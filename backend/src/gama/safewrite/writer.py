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
from gama.safewrite.journal import Journal, JournalError
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


def _phase_b_one(item: WriteItem, res: ItemResult, env: WriteEnv, j: Journal, job_id: str) -> None:
    """Write one file. Raises _ItemFailed; the original is untouched unless the rename happened."""
    store = env.store
    backup = store.path_for(item.sha_before)
    fsutil.failpoint("item_start", n=item.n, path=item.path)
    try:
        preflight.check_item(item.target, env.euid())
        preflight.check_library_space(item.path, os.path.getsize(backup), env.margin_bytes, env.free_space)
    except preflight.PreflightError as e:
        raise _ItemFailed("conflict" if e.code == "conflict" else "failed", "preflight", f"{e.code}: {e}") from None

    temp = _temp_path(item, job_id)
    j.item(item.n, "temp_created", temp=temp)  # journaled before it exists
    renamed = False
    try:
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                got = fsutil.copy_stream(backup, fd)  # S7: from the verified backup, not the live file
            finally:
                os.close(fd)
        except OSError as e:
            raise _ItemFailed("failed", "temp", f"{type(e).__name__}: {e}") from None
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
        except OSError as e:
            raise _ItemFailed("failed", "attrs", f"{type(e).__name__}: {e}") from None
        fsutil.failpoint("before_rename_check", n=item.n, path=item.path, temp=temp)

        if fsutil.sha256_file(item.path) != item.sha_before:  # S7: last look before replacing
            raise _ItemFailed("conflict", "recheck", "file changed during the job; not overwritten")
        sha_after = fsutil.sha256_file(temp)
        res.sha_after = sha_after
        j.item(item.n, "renaming", temp=temp, sha_after=sha_after)
        fsutil.failpoint("after_renaming_journaled", n=item.n, path=item.path)
        os.replace(temp, item.path)
        renamed = True
        fsutil.fsync_dir(os.path.dirname(item.path))
        fsutil.failpoint("after_rename", n=item.n, path=item.path)
        if fsutil.sha256_file(item.path) != sha_after:
            raise _ItemFailed("failed", "post-rename", "file does not match the verified temp after the rename")
        j.item(item.n, "committed", sha_after=sha_after)
        res.state = "committed"
        fsutil.failpoint("after_commit", n=item.n, path=item.path)
    except _ItemFailed:
        if not renamed:
            _unlink(temp)
        raise
    except Exception as e:  # noqa: BLE001 - every unexpected error fails the item, decided by hash later
        if not renamed:
            _unlink(temp)
        raise _ItemFailed("failed", "after-rename" if renamed else "unexpected",
                          f"{type(e).__name__}: {e}") from None


def _preflight_all(items: list[WriteItem], env: WriteEnv) -> dict[int, tuple[str, str]]:
    problems: dict[int, tuple[str, str]] = {}
    euid = env.euid()
    for it in items:
        try:
            preflight.check_item(it.target, euid)
        except preflight.PreflightError as e:
            problems[it.n] = (e.code, str(e))
    if problems:
        return problems
    needed: dict[str, int] = {}
    for it in items:
        if not env.store.has(it.sha_before):
            needed[it.sha_before] = os.path.getsize(it.path)
    try:
        preflight.check_backup_space(needed.values(), env.store.root, env.margin_bytes, env.free_space)
        by_dir: dict[str, int] = {}
        for it in items:
            d = os.path.dirname(it.path)
            by_dir[d] = max(by_dir.get(d, 0), os.path.getsize(it.path))
        for d, size in by_dir.items():
            preflight.check_library_space(os.path.join(d, "x"), size, env.margin_bytes, env.free_space)
    except preflight.PreflightError as e:
        problems[-1] = (e.code, str(e))
    return problems


def run_apply(items: list[WriteItem], env: WriteEnv, *, job_id: str | None = None,
              policy: str = "stop_rollback") -> ApplyResult:
    """Run one apply job. Never raises for file problems."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    if not items or len({i.n for i in items}) != len(items):
        raise ValueError("items must be non-empty with unique n")
    job_id = job_id or uuid.uuid4().hex
    results = {i.n: ItemResult(i.n, i.relpath) for i in items}
    try:
        with env.write_lock():
            from gama.safewrite.recovery import recover_locked
            recovered = recover_locked(env)
            try:
                j = Journal.create(env.journal_dir, job_id)
            except JournalError as e:  # S4: never reuse a journal
                return ApplyResult(job_id, "failed", list(results.values()), None, str(e), recovered)
            try:
                status = _run_locked(items, env, j, job_id, policy, results)
                error = None
            except Exception as e:  # noqa: BLE001 - journal what happened, then report
                status, error = "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
                j.job("failed", error=error[:2000])
            finally:
                j.close()
            return ApplyResult(job_id, status, list(results.values()), j.path, error, recovered)
    except LockBusy as e:
        return ApplyResult(job_id, "failed", list(results.values()), None, str(e))


def _run_locked(items: list[WriteItem], env: WriteEnv, j: Journal, job_id: str, policy: str,
                results: dict[int, ItemResult]) -> str:
    j.write({"t": "header", "job": job_id, "kind": "apply", "policy": policy,
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
    except (BackupError, OSError) as e:
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
            j.item(it.n, e.state, stage=e.stage, error=e.error[:2000], sha_after=r.sha_after)
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
        out = restore_one(RestoreItem(it.n, it.root, it.relpath, it.sha_before, r.sha_after), env, j, job_id)
        r.restore = asdict(out)
        if out.state == "restored":
            r.state = "restored"
        else:
            all_ok = False
    status = "rolled_back" if all_ok else "failed_partial"
    j.job(status)
    return status
