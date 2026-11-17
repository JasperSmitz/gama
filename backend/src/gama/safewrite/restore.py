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
from dataclasses import asdict, dataclass

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


def restore_one(it: RestoreItem, env: WriteEnv, journal: Journal, job_id: str) -> RestoreOutcome:
    """Restore one file. Never raises for file problems; journals every step."""
    out = RestoreOutcome(it.n, it.relpath, "restore_failed")

    def done(state: str, error: str | None = None) -> RestoreOutcome:
        out.state, out.error = state, error
        journal.item(it.n, state, error=error, sha_found=out.sha_found, forced_backup=out.forced_backup_sha)
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

    temp = _temp_path(it.path, job_id, it.n, "r")
    journal.item(it.n, "restoring", temp=temp, sha_expected_current=cur)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            got = fsutil.copy_stream(store.path_for(it.sha_before), fd)
        finally:
            os.close(fd)
        if got != it.sha_before or fsutil.sha256_file(temp) != it.sha_before:
            raise RuntimeError("temp copy of the backup does not match sha_before")
        fsutil.copy_attrs(it.path, temp)
        fsutil.failpoint("restore_before_rename", n=it.n, path=it.path)
        if fsutil.sha256_file(it.path) != cur:
            _unlink(temp)
            return done("restore_conflict", "file changed during the restore; not overwritten")
        os.replace(temp, it.path)
    except Exception as e:  # noqa: BLE001
        _unlink(temp)
        return done("restore_failed", f"{type(e).__name__}: {e}")
    fsutil.fsync_dir(os.path.dirname(it.path))
    fsutil.failpoint("restore_after_rename", n=it.n, path=it.path)
    if fsutil.sha256_file(it.path) != it.sha_before:
        return done("restore_failed", "file does not match the backup after the restore")
    return done("restored")


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


@dataclass
class RestoreResult:
    job_id: str
    status: str                     # succeeded | completed_with_errors | failed
    items: list[RestoreOutcome]
    journal_path: str | None = None
    error: str | None = None


def run_restore(items: list[RestoreItem], env: WriteEnv, *, job_id: str | None = None) -> RestoreResult:
    """A restore job: locked, recovered, journaled like an apply."""
    from gama.safewrite.recovery import recover_locked

    job_id = job_id or uuid.uuid4().hex
    try:
        with env.write_lock():
            recover_locked(env)
            try:
                j = Journal.create(env.journal_dir, job_id)
            except JournalError as e:
                return RestoreResult(job_id, "failed", [], None, str(e))
            try:
                j.write({"t": "header", "job": job_id, "kind": "restore",
                         "items": [asdict(i) for i in items]})
                outs = [restore_one(i, env, j, job_id) for i in items]
                status = "succeeded" if all(o.state in RESTORED_OK for o in outs) else "completed_with_errors"
                j.job(status)
                return RestoreResult(job_id, status, outs, j.path)
            finally:
                j.close()
    except LockBusy as e:
        return RestoreResult(job_id, "failed", [], None, str(e))
