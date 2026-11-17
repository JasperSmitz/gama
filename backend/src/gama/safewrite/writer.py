"""The apply job (plan 1.6): preflight, Phase A (backups), Phase B (per file), batch policy.

Job-level preflight writes nothing if any check fails:
write lock, recovery of earlier jobs, per-file checks (regular file, no
symlink, one hard link, owned by us, writable file and directory, sha equals
the plan), free space for backups and temp copies.

Phase A backs up every file into the content-addressed store and re-hashes all
backups before any edit. Phase B, per file and journaled at each step:

 1. re-check the preflight conditions (incl. library free space);
 2. journal the temp path, create it ``O_EXCL`` next to the original, fill it
    **from the backup** (S7), fsync, check its hash;
 3. mutate it in a child process (timeout, killed on hang; S6);
 4. verify it in a read child (genres, snapshot, raw trailers, audio);
 5. copy group, xattrs and mode from the original (not mtime);
 6. re-hash the original: still ``sha_before``, else ``conflict`` (S7);
 7. journal ``renaming`` with ``sha_after``;
 8. ``os.replace`` + directory fsync;
 9. re-hash: ``== sha_after`` -> journal ``committed``.

Any failure before step 8 removes the temp file; the original stays
byte-identical. After that, outcomes are decided by hash (S3).

Batch policy ``stop_rollback`` (default): on the first failed item, stop and
restore every attempted item whose current hash is not ``sha_before`` through
the restore path -> ``rolled_back`` (or ``failed_partial`` if a restore did not
succeed). ``continue``: keep going -> ``completed_with_errors``.

A multi-file job is NOT atomic; each file is replaced atomically by rename(2).
"""
from __future__ import annotations

import os
import secrets
import traceback
import uuid
from dataclasses import asdict, dataclass, field

from gama.safewrite import fsutil, preflight
from gama.safewrite.backups import BackupError
from gama.safewrite.env import LockBusy, WriteEnv
from gama.safewrite.journal import Journal, JournalError, JournalWriteError, load, summarize
from gama.safewrite.restore import RestoreItem, restore_one

POLICIES = ("stop_rollback", "continue")


@dataclass(frozen=True)
class WriteItem:
    n: int
    root: str
    relpath: str
    format: str
    sha_before: str
    genres_before: list[str]
    genres_after: list[str]

    @property
    def path(self) -> str:
        return os.path.join(self.root, self.relpath)

    @property
    def target(self) -> preflight.Target:
        return preflight.Target(self.n, self.root, self.relpath, self.sha_before)


@dataclass
class ItemResult:
    n: int
    relpath: str
    state: str = "planned"
    stage: str | None = None
    error: str | None = None
    sha_after: str | None = None
    backup_sha: str | None = None
    backup_relpath: str | None = None
    verification: dict | None = None
    attr_notes: list[str] = field(default_factory=list)
    restore: dict | None = None
    restore_state: str | None = None  # outcome of the rollback; ``state`` stays the apply outcome


@dataclass
class ApplyResult:
    job_id: str
    status: str
    items: list[ItemResult]
    journal_path: str | None = None
    error: str | None = None
    recovered: list[dict] = field(default_factory=list)

    @property
    def written(self) -> list[ItemResult]:
        return [i for i in self.items if i.state == "committed"]


def _temp_path(item: WriteItem, job_id: str) -> str:
    d, name = os.path.split(item.path)
    return os.path.join(d, f"{fsutil.TEMP_PREFIX}{job_id}-{item.n}-{secrets.token_hex(4)}"
                           f"{os.path.splitext(name)[1]}")


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


class _ItemFailed(Exception):
    def __init__(self, state: str, stage: str, error: str):
        super().__init__(error)
        self.state, self.stage, self.error = state, stage, error
        self.leaked: str | None = None
        self.sha_found: str | None = None


def _phase_b_one(item: WriteItem, res: ItemResult, env: WriteEnv, j: Journal, job_id: str) -> None:
    """Write one file. Every exception becomes _ItemFailed; the original is untouched
    unless the rename happened, and after the rename the outcome is decided by hash."""
    state = {"temp": None, "renamed": False}
    try:
        _phase_b_steps(item, res, env, j, job_id, state)
    except _ItemFailed as e:
        failure = e
    except Exception as e:  # noqa: BLE001 - OSError from hashing/stat, journal write errors, ...
        if state["renamed"] and res.state != "committed":
            # Never report "failed" for a file that holds the edit: decide by hash.
            _commit_by_hash(item, res, j, f"{type(e).__name__}: {e}")
            return
        if state["renamed"]:
            raise  # committed, but the journal or a later step failed: run_apply aborts and resolves by hash
        failure = _ItemFailed("failed", "unexpected", f"{type(e).__name__}: {e}")
    else:
        return
    if state["temp"] and not state["renamed"]:
        leak = fsutil.remove_temp(state["temp"])
        if leak:
            failure.error += f"; temp file could not be removed ({leak})"
            failure.leaked = state["temp"]
    raise failure


def _phase_b_steps(item: WriteItem, res: ItemResult, env: WriteEnv, j: Journal, job_id: str, state: dict) -> None:
    store = env.store
    backup = store.path_for(item.sha_before)
    fsutil.failpoint("item_start", n=item.n, path=item.path)
    try:
        preflight.check_item(item.target, env.euid())
        preflight.check_library_space(item.path, os.path.getsize(backup), env.margin_bytes, env.free_space)
    except preflight.PreflightError as e:
        raise _ItemFailed("conflict" if e.code == "conflict" else "failed", "preflight", f"{e.code}: {e}") from None
    except OSError as e:  # e.g. the file became unreadable: still a clean, pre-temp failure
        raise _ItemFailed("failed", "preflight", f"{type(e).__name__}: {e}") from None

    temp = _temp_path(item, job_id)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as e:  # incl. FileExistsError: that file is not ours and is never touched
        raise _ItemFailed("failed", "temp", f"{type(e).__name__}: {e}") from None
    # Ours from here on: only now is it recorded (in the journal too), so cleanup and
    # recovery only ever delete files the engine created itself.
    state["temp"] = temp
    try:
        j.item(item.n, "temp_created", temp=temp)
        got = fsutil.copy_stream(backup, fd)  # S7: from the verified backup, not the live file
    finally:
        os.close(fd)
    if got != item.sha_before or fsutil.sha256_file(temp) != item.sha_before:
        raise _ItemFailed("failed", "temp", "temp copy does not match the backup")
    j.item(item.n, "temp_written", temp=temp)
    fsutil.failpoint("after_temp_written", n=item.n, path=item.path, temp=temp)

    m = env.mutate(temp, item.format, item.genres_after, item.genres_before, timeout=env.mutate_timeout)
    if not m.ok:
        raise _ItemFailed("failed", "mutate", m.error or "writer failed")
    fsutil.fsync_file(temp)

    v = env.verify(backup, temp, item.genres_after, audio=env.audio)
    res.verification = v.to_dict()
    if not v.ok:
        raise _ItemFailed("failed", "verify", "; ".join(v.problems)[:2000])
    j.item(item.n, "verified", verification=res.verification)

    try:
        res.attr_notes = fsutil.copy_attrs(item.path, temp)
        fsutil.fsync_file(temp)  # make mode/group/xattrs durable before the rename
    except OSError as e:
        raise _ItemFailed("failed", "attrs", f"{type(e).__name__}: {e}") from None
    fsutil.failpoint("before_rename_check", n=item.n, path=item.path, temp=temp)

    if fsutil.sha256_file(item.path) != item.sha_before:  # S7: last look before replacing
        raise _ItemFailed("conflict", "recheck", "file changed during the job; not overwritten")
    sha_after = fsutil.sha256_file(temp)
    res.sha_after = sha_after
    j.item(item.n, "renaming", temp=temp, sha_after=sha_after)
    fsutil.failpoint("after_renaming_journaled", n=item.n, path=item.path)
    try:
        os.replace(temp, item.path)
    except OSError as e:
        raise _ItemFailed("failed", "rename", f"{type(e).__name__}: {e}") from None
    state["renamed"] = True
    note = None
    try:
        fsutil.fsync_dir(os.path.dirname(item.path))
        fsutil.failpoint("after_rename", n=item.n, path=item.path)
    except Exception as e:  # noqa: BLE001 - the hash decides below
        note = f"after the rename: {type(e).__name__}: {e} (the edit may not be durable yet)"
    _commit_by_hash(item, res, j, note)
    fsutil.failpoint("after_commit", n=item.n, path=item.path)


def _commit_by_hash(item: WriteItem, res: ItemResult, j: Journal, note: str | None) -> None:
    """After the rename: the file's hash decides. == sha_after -> committed (any error is
    only a note), else failed with the hash found."""
    try:
        cur = fsutil.sha256_file(item.path)
    except OSError as e:
        cur, note = None, f"{note + '; ' if note else ''}cannot re-read after the rename: {e}"
    if cur != res.sha_after:
        f = _ItemFailed("failed", "post-rename", f"file does not match the verified temp after the rename "
                                                 f"(found {cur}){'; ' + note if note else ''}")
        f.sha_found = cur
        raise f
    res.state, res.error = "committed", note
    j.item(item.n, "committed", sha_after=res.sha_after, error=note)


def _preflight_all(items: list[WriteItem], env: WriteEnv) -> dict[int, tuple[str, str]]:
    problems: dict[int, tuple[str, str]] = {}
    euid = env.euid()
    for it in items:
        try:
            preflight.check_item(it.target, euid)
        except preflight.PreflightError as e:
            problems[it.n] = (e.code, str(e))
        except OSError as e:
            problems[it.n] = ("unreadable", f"{type(e).__name__}: {e}")
    if problems:
        return problems
    try:
        needed: dict[str, int] = {}
        for it in items:
            if not env.store.has(it.sha_before):
                needed[it.sha_before] = os.path.getsize(it.path)
        preflight.check_backup_space(needed.values(), env.store.root, env.margin_bytes, env.free_space)
        by_dir: dict[str, int] = {}
        for it in items:
            d = os.path.dirname(it.path)
            by_dir[d] = max(by_dir.get(d, 0), os.path.getsize(it.path))
        for d, size in by_dir.items():
            preflight.check_library_space(os.path.join(d, "x"), size, env.margin_bytes, env.free_space)
    except preflight.PreflightError as e:
        problems[-1] = (e.code, str(e))
    except OSError as e:
        problems[-1] = ("unreadable", f"{type(e).__name__}: {e}")
    return problems


def _resolve_results(items: list[WriteItem], journal_path: str, results: dict[int, ItemResult]) -> None:
    """After an abort, report what is really on disk: journal + current hashes (read-only)."""
    from gama.safewrite.recovery import resolve_apply_item
    try:
        s = summarize(load(journal_path))
    except Exception:  # noqa: BLE001
        s = {"items": {}}
    for it in items:
        r = results[it.n]
        merged = {"sha_before": it.sha_before, **s["items"].get(it.n, {})}
        r.sha_after = merged.get("sha_after", r.sha_after)
        try:
            cur = fsutil.sha256_file(it.path)
        except FileNotFoundError:
            cur = None
        except OSError as e:
            r.state, r.error = "unknown", f"cannot hash after the abort: {e}"
            continue
        r.state, r.restore_state, why = resolve_apply_item(merged, cur)
        r.error = r.error or why


def run_apply(items: list[WriteItem], env: WriteEnv, *, job_id: str | None = None,
              policy: str = "stop_rollback", observer=None) -> ApplyResult:
    """Run one apply job. Never raises for file problems."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    if not items or len({i.n for i in items}) != len(items):
        raise ValueError("items must be non-empty with unique n")
    job_id = job_id or uuid.uuid4().hex
    results = {i.n: ItemResult(i.n, i.relpath) for i in items}
    try:
        with env.write_lock():
            from gama.safewrite.recovery import blocked_paths, recover_locked
            recovered = recover_locked(env)
            blocked, unknown = blocked_paths(recovered)
            touched = [it.relpath for it in items if fsutil.real_target(it.root, it.relpath) in blocked]
            if unknown or touched:
                why = ("an unreadable journal of an earlier job needs attention" if unknown else
                       f"recovery of an earlier job is pending for: {touched[:5]}")
                for r in results.values():
                    r.state, r.stage, r.error = "skipped", "recovery", why
                return ApplyResult(job_id, "failed", list(results.values()), None,
                                   f"refused: {why}; nothing was written", recovered)
            try:
                j = Journal.create(env.journal_dir, job_id)
            except JournalError as e:  # S4: never reuse a journal
                return ApplyResult(job_id, "failed", list(results.values()), None, str(e), recovered)
            j.observer = observer
            try:
                status = _run_locked(items, env, j, job_id, policy, results)
                error = None
            except Exception as e:  # noqa: BLE001
                # Typically the journal itself cannot be written (disk full). No terminal
                # status is written: the job stays open, so the next recovery resolves every
                # item by hash and removes the temps. The result shows the states the
                # files are really in (journal + current hashes).
                status, error = "aborted", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
                _resolve_results(items, j.path, results)
            finally:
                j.close()
            return ApplyResult(job_id, status, list(results.values()), j.path, error, recovered)
    except LockBusy as e:
        return ApplyResult(job_id, "failed", list(results.values()), None, str(e))


def _run_locked(items: list[WriteItem], env: WriteEnv, j: Journal, job_id: str, policy: str,
                results: dict[int, ItemResult]) -> str:
    j.write({"t": "header", "job": job_id, "kind": "apply", "policy": policy,
             "roots": {r: fsutil.root_identity(r) for r in sorted({i.root for i in items})},
             "items": [{**asdict(i), "backup_sha": i.sha_before} for i in items]})

    # ---- job-level preflight: nothing is written if anything fails
    problems = _preflight_all(items, env)
    if problems:
        conflict = any(code == "conflict" for code, _ in problems.values())
        for it in items:
            r = results[it.n]
            code, msg = problems.get(it.n, (None, None))
            r.state = ("conflict" if code == "conflict" else "failed") if code else "skipped"
            r.stage, r.error = "preflight", (f"{code}: {msg}" if code else None)
            j.item(it.n, r.state, stage="preflight", error=r.error)
        if -1 in problems:
            code, msg = problems[-1]
            for r in results.values():
                r.state, r.stage, r.error = "skipped", "preflight", f"{code}: {msg}"
        status = "conflict" if conflict else "failed"
        j.job(status, error=None if -1 not in problems else f"{problems[-1][0]}: {problems[-1][1]}")
        return status

    # ---- Phase A: back up everything, then verify every backup, before any edit
    try:
        for it in items:
            path, created = env.store.ensure(it.path, it.sha_before)
            r = results[it.n]
            r.backup_sha, r.backup_relpath = it.sha_before, env.store.relpath_for(it.sha_before)
            j.item(it.n, "backed_up", backup=r.backup_relpath, created=created)
        fsutil.failpoint("after_backup")
        for it in items:
            if not env.store.verify(it.sha_before):
                raise BackupError(f"backup of {it.relpath} failed verification")
    except Exception as e:  # noqa: BLE001 - BackupError, OSError, ...: no file has been edited yet
        if isinstance(e, JournalWriteError):
            raise
        for it in items:
            results[it.n].state, results[it.n].stage = "skipped", "backup"
            j.item(it.n, "skipped", stage="backup", error=str(e))
        j.job("failed", error=f"backup phase: {e}")
        return "failed"

    # ---- Phase B: one file at a time
    failed = False
    attempted: list[WriteItem] = []
    for it in items:
        r = results[it.n]
        if failed and policy == "stop_rollback":
            r.state, r.stage = "skipped", "stopped"
            j.item(it.n, "skipped", stage="stopped")
            continue
        attempted.append(it)
        try:
            _phase_b_one(it, r, env, j, job_id)
        except _ItemFailed as e:
            r.state, r.stage, r.error = e.state, e.stage, e.error
            extra = {"temp_leaked": e.leaked} if e.leaked else {}
            if e.sha_found:
                extra["sha_found"] = e.sha_found
            j.item(it.n, e.state, stage=e.stage, error=e.error[:2000], sha_after=r.sha_after, **extra)
            failed = True

    if not failed:
        j.job("succeeded")
        return "succeeded"
    if policy == "continue":
        j.job("completed_with_errors")
        return "completed_with_errors"

    # ---- stop_rollback: restore every item that reached "renaming" (committed, or failed
    # after the rename) and whose bytes are not the original, decided by hash (S3).
    # Items that never got that far were not written by us; a conflict item keeps the
    # other program's bytes.
    all_ok = True
    for it in attempted:
        r = results[it.n]
        if r.sha_after is None:
            continue
        try:
            cur = fsutil.sha256_file(it.path)
        except OSError:
            cur = None
        if cur == it.sha_before:
            continue
        out = restore_one(RestoreItem(it.n, it.root, it.relpath, it.sha_before, r.sha_after), env, j, job_id,
                          field="restore_state")
        r.restore = asdict(out)
        r.restore_state = out.state
        if out.state != "restored":
            all_ok = False
    status = "rolled_back" if all_ok else "failed_partial"
    j.job(status)
    return status
