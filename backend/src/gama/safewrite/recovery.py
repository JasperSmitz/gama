"""Crash recovery (plan 1.7). Runs at worker start and before every job, under the write lock.

For each journal without a terminal job record:

1. check every library root the job touched: it must exist and have the same
   identity (st_dev, st_ino) as when the job started. A root that is absent, or
   an empty mountpoint where the disk should be, **defers** recovery;
2. delete the job's temp files (only those the engine created itself);
3. hash every item's file. A hash error other than "not found" defers too;
4. resolve every item by its **current hash**, never by the journaled state
   alone (S3):
   * apply ``state``:   ``== sha_after`` -> committed; ``== sha_before`` -> the
     journaled outcome (or failed when interrupted before the rename); not found
     -> ``missing`` when the edit may have been written (sha_after is kept), else
     failed; anything else -> conflict;
   * ``restore_state`` (rollback inside an apply job): ``restored`` when the file
     is the original again, ``restore_failed`` otherwise;
   * restore job ``state``: ``== sha_before`` -> restored, else restore_failed /
     restore_conflict;
5. mark the job ``interrupted``.

A deferred job stays open ("pending"), and new jobs that touch its files are
refused until a later recovery succeeds or an operator resolves it explicitly
(``resolve_pending``, e.g. after a root was retired or an album folder renamed).
Finished jobs whose temp files could not be removed get them removed now.
Leftover ``*.part`` files in the backup store are swept. A journal that cannot be
read is reported ("damaged") and blocks only what it could concern.
Recovery never continues a job.
"""
from __future__ import annotations

import glob
import json
import os

from gama.safewrite import fsutil
from gama.safewrite.env import WriteEnv
from gama.safewrite.journal import TERMINAL_ITEM, TERMINAL_JOB, Journal, JournalError, load, summarize


class _Defer(Exception):
    pass


def _check_roots(header: dict) -> None:
    """_Defer unless every root of the job is present with its recorded identity."""
    recorded = header.get("roots") or {}
    roots = {i.get("root") for i in header.get("items", [])}
    for root in sorted(r for r in roots if r):
        if not os.path.isdir(root):
            raise _Defer(f"library root {root} is not available (not mounted?)")
        want = recorded.get(root)
        if want is not None and fsutil.root_identity(root) != list(want):
            raise _Defer(f"library root {root} is not the filesystem the job wrote to (empty mountpoint? "
                         "disk not mounted?)")


def _hash(path: str) -> str | None:
    """sha256, or None if the file is not there (its root was confirmed present)."""
    try:
        return fsutil.sha256_file(path)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None
    except OSError as e:
        raise _Defer(f"cannot read {path}: {e}") from None


def resolve_apply_item(item: dict, cur: str | None) -> tuple[str, str | None, str]:
    """(state, restore_state, why) for an apply item from the file's current hash."""
    sb, sa = item.get("sha_before"), item.get("sha_after")
    state, rstate = item.get("state", "planned"), item.get("restore_state")
    if cur is None:
        if sa is not None or state in ("committed", "renaming", "missing"):
            return "missing", rstate, "file not found; the edit may have been written (sha_after kept)"
        return "failed", rstate, "file not found; the job never wrote it"
    if sa is not None and cur == sa:
        new_r = "restore_failed" if rstate == "restoring" else rstate
        return "committed", new_r, "the edit is in place"
    if cur == sb:
        if rstate in ("restoring", "restored"):
            return (state if state in ("committed", "failed") else "committed"), "restored", \
                "the original is in place (rolled back)"
        if state in ("failed", "conflict", "skipped"):
            return state, rstate, "original untouched"
        return "failed", rstate, "interrupted before the rename; original untouched"
    return "conflict", rstate, "file matches neither the original nor the edit"


def _resolve_restore(item: dict, cur: str | None) -> tuple[str, str]:
    if cur is None:
        return "restore_failed", "file missing"
    if cur == item.get("sha_before"):
        return "restored", "restore had completed before the interruption"
    if cur in (item.get("sha_after"), item.get("sha_expected_current")):
        return "restore_failed", "interrupted before the rename; file untouched"
    return "restore_conflict", "file matches neither the backup nor the expected current version"


def _header_paths(header: dict | None) -> set[str] | None:
    """Canonical paths a journal concerns; None when unknown (malformed header)."""
    if not header:
        return None
    try:
        return {fsutil.real_target(i["root"], i["relpath"]) for i in header.get("items", [])}
    except (KeyError, TypeError, AttributeError):
        return None


def _remove_temps(paths: list[str], *, roots_ok: bool) -> tuple[list[str], list[str]]:
    """(removed or already gone, could not remove). With the roots confirmed present, a
    temp whose directory no longer exists (folder renamed or deleted) is gone."""
    removed, failed = [], []
    for t in paths:
        if not os.path.basename(t).startswith(fsutil.TEMP_PREFIX):
            continue
        if not os.path.isdir(os.path.dirname(t)):
            (removed if roots_ok else failed).append(t)
            continue
        err = fsutil.remove_temp(t)
        (failed if err else removed).append(t)
    return removed, failed


def _first_header(path: str) -> dict | None:
    try:
        with open(path, "rb") as f:
            rec = json.loads(f.readline())
        return rec if isinstance(rec, dict) and rec.get("t") == "header" else None
    except (OSError, ValueError):
        return None


def _resolve_items(j: Journal, s: dict, header: dict, hashes: dict, *, forced: bool) -> dict:
    kind = header.get("kind", "apply")
    resolved = {}
    for hi in header.get("items", []):
        n = hi["n"]
        item = {**hi, **s["items"].get(n, {})}
        state = item.get("state", "planned")
        cur = hashes[n]
        extra = {"forced": True} if forced else {}
        if kind == "restore":
            if state in TERMINAL_ITEM:
                continue
            new, why = _resolve_restore(item, cur)
            j.item(n, new, stage="interrupted", error=why, recovered=True, sha_found=cur,
                   sha_before=item.get("sha_before"), sha_after=item.get("sha_after"), **extra)
            resolved[n] = new
            continue
        new, new_r, why = resolve_apply_item(item, cur)
        if new == state and new_r == item.get("restore_state"):
            continue
        fields = {"stage": "interrupted", "error": why, "recovered": True, "sha_found": cur,
                  "sha_before": item.get("sha_before"), "sha_after": item.get("sha_after"), **extra}
        if new_r is not None:
            fields["restore_state"] = new_r
        j.item(n, new, **fields)
        resolved[n] = new
    return resolved


def recover_journal(path: str) -> dict | None:
    """Recover one journal. Returns a report, or None when there was nothing to do.

    Report status: "interrupted" (resolved), "pending" (deferred), "damaged"
    (journal unreadable), "cleaned" / "cleanup_pending" (leaked temps of a finished job).
    """
    try:
        s = summarize(load(path))
    except JournalError as e:
        header = _first_header(path)
        return {"journal": path, "job": (header or {}).get("job"), "status": "damaged", "error": str(e),
                "paths": _header_paths(header)}
    header = s["header"]
    if header is None:
        return None
    job, kind = header.get("job"), header.get("kind", "apply")
    paths = _header_paths(header)
    if paths is None:
        return {"journal": path, "job": job, "status": "damaged", "error": "malformed journal header",
                "paths": None}
    try:
        _check_roots(header)
        roots_ok = True
    except _Defer as e:
        roots_ok, root_error = False, str(e)

    if s["status"] in TERMINAL_JOB:
        if not s["leaked"]:
            return None
        removed, failed = _remove_temps(s["leaked"], roots_ok=roots_ok)
        if removed:
            j = Journal.open_append(path)
            try:
                j.write({"t": "cleanup", "removed": removed})
            finally:
                j.close()
        # A leftover temp of a finished job blocks nothing; it is retried at every recovery.
        return {"journal": path, "job": job, "kind": kind, "status": "cleanup_pending" if failed else "cleaned",
                "temps_removed": removed, "temps_left": failed, "paths": set(), "items": {}}

    if not roots_ok:
        return {"journal": path, "job": job, "kind": kind, "status": "pending", "error": root_error,
                "temps_removed": [], "paths": paths, "items": {}}
    temps = [t for ts in s["temps"].values() for t in ts]
    removed, failed_temps = _remove_temps(temps, roots_ok=True)
    try:
        if failed_temps:
            raise _Defer(f"temp files could not be removed: {failed_temps}")
        hashes = {hi["n"]: _hash(os.path.join(hi["root"], hi["relpath"])) for hi in header.get("items", [])}
    except _Defer as e:
        return {"journal": path, "job": job, "kind": kind, "status": "pending", "error": str(e),
                "temps_removed": removed, "paths": paths, "items": {}}

    j = Journal.open_append(path)
    try:
        resolved = _resolve_items(j, s, header, hashes, forced=False)
        j.job("interrupted", recovered=True, temps_removed=removed)
    finally:
        j.close()
    return {"journal": path, "job": job, "kind": kind, "items": resolved, "temps_removed": removed,
            "status": "interrupted", "paths": set()}


def resolve_pending(env: WriteEnv, job_id: str, *, confirm_missing: bool = False) -> dict:
    """Operator action for a job whose recovery stays pending (root retired, album folder
    renamed, temp in a directory that stays read-only).

    It re-checks everything: files that can be hashed are resolved by hash as usual.
    Files (or whole roots) that are not there are resolved as ``missing`` / failed
    only with ``confirm_missing=True``; otherwise nothing is written and the report
    says what is still unavailable. Unremovable temps are recorded as leaked.
    """
    with env.write_lock():
        path = os.path.join(env.journal_dir, f"{job_id}.jsonl")
        s = summarize(load(path))
        header = s["header"]
        if s["status"] in TERMINAL_JOB:
            return {"job": job_id, "status": "already_finished", "journal": path}
        unavailable, hashes = [], {}
        for hi in header.get("items", []):
            p = os.path.join(hi["root"], hi["relpath"])
            try:
                hashes[hi["n"]] = fsutil.sha256_file(p)
            except FileNotFoundError:
                hashes[hi["n"]] = None
                unavailable.append(p)
            except OSError as e:
                hashes[hi["n"]] = None
                unavailable.append(f"{p} ({e})")
        if unavailable and not confirm_missing:
            return {"job": job_id, "status": "pending", "journal": path, "unavailable": unavailable}
        temps = [t for ts in s["temps"].values() for t in ts]
        removed, left = _remove_temps(temps, roots_ok=True)
        j = Journal.open_append(path)
        try:
            for t in left:
                j.write({"t": "item", "n": -1, "temp_leaked": t})
            resolved = _resolve_items(j, s, header, hashes, forced=True)
            j.job("interrupted", recovered=True, forced=True, temps_removed=removed,
                  unavailable=unavailable)
        finally:
            j.close()
        return {"job": job_id, "status": "interrupted", "journal": path, "items": resolved,
                "unavailable": unavailable, "temps_removed": removed, "temps_left": left}


def recover_locked(env: WriteEnv) -> list[dict]:
    """Recover every unfinished journal. The caller must hold the write lock.

    Never raises: a bad journal or an unremovable store entry is reported, and the
    rest is processed.
    """
    reports = []
    try:
        swept = env.store.sweep_tmp()
        if swept:
            reports.append({"journal": None, "job": None, "status": "swept", "parts_removed": swept,
                            "paths": set()})
    except Exception as e:  # noqa: BLE001
        reports.append({"journal": None, "job": None, "status": "sweep_failed", "error": str(e), "paths": set()})
    try:
        journals = sorted(glob.glob(os.path.join(env.journal_dir, "*.jsonl")))
    except Exception as e:  # noqa: BLE001
        return reports + [{"journal": None, "job": None, "status": "damaged", "error": str(e), "paths": None}]
    for path in journals:
        try:
            r = recover_journal(path)
        except Exception as e:  # noqa: BLE001 - e.g. the journal cannot be appended to
            try:
                header = _first_header(path)
                paths = _header_paths(header)
                job = (header or {}).get("job")
            except Exception:  # noqa: BLE001
                paths, job = None, None
            r = {"journal": path, "job": job, "status": "damaged", "error": f"{type(e).__name__}: {e}",
                 "paths": paths}
        if r is not None:
            reports.append(r)
    return reports


def blocked_paths(reports: list[dict]) -> tuple[set[str], bool]:
    """(canonical paths new jobs must not touch, whether every apply must be refused)."""
    paths: set[str] = set()
    unknown = False
    for r in reports:
        if r["status"] in ("pending", "damaged"):
            if r.get("paths") is None:
                unknown = True
            else:
                paths |= set(r["paths"])
    return paths, unknown


def recover(env: WriteEnv) -> list[dict]:
    with env.write_lock():
        return recover_locked(env)
