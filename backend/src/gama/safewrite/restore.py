"""Restore files from backups (plan 1.7). Used by restore jobs and by the
batch rollback of an apply job.

For each item the backup is re-hashed against ``sha_before`` **before** the
file is touched (S2). Then the current file decides:

* ``== sha_after``  -> restore via temp-from-backup, attribute copy, re-hash of
  the original just before the rename, rename, verify ``== sha_before``;
* ``== sha_before`` -> nothing to do;
* anything else     -> ``restore_conflict``; never overwrites newer edits (S5)
  unless ``force``, which first backs up the current bytes into the store;
* missing           -> reported, never recreated.
"""
from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import asdict, dataclass, field

from gama.safewrite import fsutil, preflight
from gama.safewrite.env import LockBusy, WriteEnv
from gama.safewrite.journal import Journal, JournalError

RESTORED_OK = {"restored", "nothing_to_do"}


@dataclass(frozen=True)
class RestoreItem:
    n: int
    root: str
    relpath: str
    sha_before: str
    sha_after: str | None
    force: bool = False

    @property
    def path(self) -> str:
        return os.path.join(self.root, self.relpath)


@dataclass
class RestoreOutcome:
    n: int
    relpath: str
    state: str                      # restored | nothing_to_do | restore_conflict | restore_failed
    error: str | None = None
    sha_found: str | None = None
    forced_backup_sha: str | None = None


def _temp_path(path: str, job_id: str, n: int, tag: str) -> str:
    d, name = os.path.split(path)
    ext = os.path.splitext(name)[1]
    return os.path.join(d, f"{fsutil.TEMP_PREFIX}{job_id}-{n}-{tag}{secrets.token_hex(4)}{ext}")


def restore_one(it: RestoreItem, env: WriteEnv, journal: Journal, job_id: str, *,
                field: str = "state") -> RestoreOutcome:
    """Restore one file. Never raises: every exception becomes ``restore_failed``,
    and once the rename has happened the outcome is decided by hash.

    ``field``: the journal field for the restore states. A rollback inside an apply
    job uses "restore_state", so the item's apply ``state`` keeps saying what the
    edit did (committed, failed, ...) and the rollback outcome sits next to it.
    """
    out = RestoreOutcome(it.n, it.relpath, "restore_failed")
    try:
        return _restore_one(it, env, journal, job_id, out, field)
    except Exception as e:  # noqa: BLE001 - e.g. a journal write error; the file state is decided later by hash
        out.state, out.error = "restore_failed", f"{type(e).__name__}: {e}"
        return out


def _restore_one(it: RestoreItem, env: WriteEnv, journal: Journal, job_id: str, out: RestoreOutcome,
                 field: str) -> RestoreOutcome:
    def record(state: str, **fields) -> None:
        journal.write({"t": "item", "n": it.n, field: state, **fields})

    def done(state: str, error: str | None = None, **extra) -> RestoreOutcome:
        out.state, out.error = state, error
        record(state, error=error, sha_found=out.sha_found, forced_backup=out.forced_backup_sha,
               sha_before=it.sha_before, sha_after=it.sha_after, **extra)
        return out

    store = env.store
    if not store.verify(it.sha_before):  # S2: check the backup before touching anything
        return done("restore_failed", "backup missing or does not match its hash; file not touched")
    if not os.path.lexists(it.path):
        return done("restore_failed", "file is missing; it is never recreated")
    try:
        cur = fsutil.sha256_file(it.path)
    except OSError as e:
        return done("restore_failed", f"cannot read current file: {e}")
    out.sha_found = cur
    if cur == it.sha_before:
        return done("nothing_to_do")
    if cur != it.sha_after:
        if not it.force:
            return done("restore_conflict", "file changed after the edit; not overwritten (use force)")
        try:
            store.ensure(it.path, cur)  # keep the newer bytes before replacing them
        except Exception as e:  # noqa: BLE001
            return done("restore_failed", f"could not back up the current file before forcing: {e}")
        out.forced_backup_sha = cur
    try:
        preflight.check_item(preflight.Target(it.n, it.root, it.relpath, cur), env.euid(), check_hash=False)
        preflight.check_library_space(it.path, os.path.getsize(store.path_for(it.sha_before)), env.margin_bytes,
                                      env.free_space)
    except preflight.PreflightError as e:
        return done("restore_failed", f"{e.code}: {e}")
    except OSError as e:
        return done("restore_failed", f"{type(e).__name__}: {e}")

    temp = _temp_path(it.path, job_id, it.n, "r")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as e:  # incl. FileExistsError: not ours, never touched
        return done("restore_failed", f"cannot create a temp file: {type(e).__name__}: {e}")
    try:
        # Recorded once it is ours, and before the rename. If this write fails, the temp
        # (ours) is removed and the file is never touched.
        try:
            record("restoring", temp=temp, sha_expected_current=cur, sha_before=it.sha_before,
                   sha_after=it.sha_after)
        except Exception:
            os.close(fd)
            fsutil.remove_temp(temp)
            raise
        try:
            got = fsutil.copy_stream(store.path_for(it.sha_before), fd)
        finally:
            os.close(fd)
        if got != it.sha_before or fsutil.sha256_file(temp) != it.sha_before:
            raise RuntimeError("temp copy of the backup does not match sha_before")
        fsutil.copy_attrs(it.path, temp)
        fsutil.fsync_file(temp)  # make the attribute changes durable before the rename
        fsutil.failpoint("restore_before_rename", n=it.n, path=it.path)
        if fsutil.sha256_file(it.path) != cur:
            return _fail_before_rename(done, temp, "restore_conflict", "file changed during the restore; not overwritten")
        os.replace(temp, it.path)
    except Exception as e:  # noqa: BLE001
        return _fail_before_rename(done, temp, "restore_failed", f"{type(e).__name__}: {e}")

    # After the rename the hash decides, whatever else fails.
    note = None
    try:
        fsutil.fsync_dir(os.path.dirname(it.path))
        fsutil.failpoint("restore_after_rename", n=it.n, path=it.path)
    except Exception as e:  # noqa: BLE001
        note = f"after the rename: {type(e).__name__}: {e}"
    try:
        now = fsutil.sha256_file(it.path)
    except OSError as e:
        return done("restore_failed", f"cannot re-read the file after the restore: {e}")
    if now != it.sha_before:
        return done("restore_failed", "file does not match the backup after the restore")
    return done("restored", note)


def _fail_before_rename(done, temp: str, state: str, error: str) -> RestoreOutcome:
    leak = fsutil.remove_temp(temp)
    if leak:  # recovery removes it once it can (journaled as temp_leaked)
        return done(state, f"{error}; temp file could not be removed ({leak})", temp_leaked=temp)
    return done(state, error)


@dataclass
class RestoreResult:
    job_id: str
    status: str                     # succeeded | completed_with_errors | failed | aborted (recovery pending)
    items: list[RestoreOutcome]
    journal_path: str | None = None
    error: str | None = None
    recovered: list = field(default_factory=list)
    lock_busy: bool = False


def run_restore(items: list[RestoreItem], env: WriteEnv, *, job_id: str | None = None,
                observer=None) -> RestoreResult:
    """A restore job: locked, recovered, journaled like an apply."""
    from gama.safewrite.recovery import blocked_paths, recover_locked

    job_id = job_id or uuid.uuid4().hex
    try:
        with env.write_lock():
            recovered = recover_locked(env)
            # Files of a job whose recovery is pending are not touched. A journal whose
            # header is unreadable does not block restores of other jobs.
            blocked, _ = blocked_paths(recovered)
            try:
                j = Journal.create(env.journal_dir, job_id)
            except JournalError as e:
                return RestoreResult(job_id, "failed", [], None, str(e))
            j.observer = observer
            outs: list[RestoreOutcome] = []
            try:
                j.write({"t": "header", "job": job_id, "kind": "restore",
                         "roots": {r: fsutil.root_identity(r) for r in sorted({i.root for i in items})},
                         "items": [asdict(i) for i in items]})
                for i in items:
                    if fsutil.real_target(i.root, i.relpath) in blocked:
                        outs.append(RestoreOutcome(i.n, i.relpath, "restore_failed",
                                                   "recovery of an earlier job is pending for this file"))
                        j.item(i.n, "restore_failed", error=outs[-1].error)
                    else:
                        outs.append(restore_one(i, env, j, job_id))
                status = "succeeded" if all(o.state in RESTORED_OK for o in outs) else "completed_with_errors"
                j.job(status)
                return RestoreResult(job_id, status, outs, j.path, recovered=recovered)
            except Exception as e:  # noqa: BLE001 - e.g. the journal cannot be written
                # No terminal status: the next recovery resolves the open items by hash.
                return RestoreResult(job_id, "aborted", outs, j.path, f"{type(e).__name__}: {e}",
                                     recovered=recovered)
            finally:
                j.close()
    except LockBusy as e:
        return RestoreResult(job_id, "failed", [], None, str(e), lock_busy=True)
