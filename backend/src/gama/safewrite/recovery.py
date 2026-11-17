"""Crash recovery (plan 1.7). Runs at worker start and before every job, under the write lock.

For each journal without a terminal job record:

1. delete journaled temp files that still exist (only ``.gama-tmp-`` names);
2. hash every item's file. If a library root or directory is unavailable (not
   mounted yet) or a hash fails with anything but "file not found", recovery is
   **deferred**: nothing is decided, the job stays open ("recovery pending"),
   and new jobs that touch those files are refused until a later recovery succeeds;
3. otherwise resolve every item by its **current hash** (S3):
   * apply item:   ``== sha_after`` -> committed, ``== sha_before`` ->
     failed (or restored after an interrupted rollback), else conflict;
   * restore item: ``== sha_before`` -> restored, else restore_failed /
     restore_conflict;
4. mark the job ``interrupted``.

Finished jobs whose temp files could not be removed (``temp_leaked``) get those
temps removed now. Leftover ``*.part`` files in the backup store are swept.
A journal that cannot be read is reported ("damaged") and blocks only what it
could concern: applies (and restores) of the files its header names, or every
apply if even its header is unreadable. Recovery never continues a job.
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


def _hash(root: str, path: str) -> str | None:
    """sha256, None if the file is missing from an available directory; _Defer otherwise."""
    if not os.path.isdir(root) or not os.path.isdir(os.path.dirname(path)):
        raise _Defer(f"{os.path.dirname(path)} is not available (not mounted?)")
    try:
        return fsutil.sha256_file(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise _Defer(f"cannot read {path}: {e}") from None


def resolve_apply_item(item: dict, cur: str | None) -> tuple[str, str]:
    """Final state of an apply item from the file's current hash (S3: never trust the state)."""
    sb, sa, state = item.get("sha_before"), item.get("sha_after"), item.get("state", "planned")
    if cur is None:
        return "failed", "file missing after the interruption"
    if sa is not None and cur == sa:
        return "committed", "the edit is in place"
    if cur == sb:
        if state in ("restored", "restoring"):
            return "restored", "the original is in place (rollback completed)"
        if state in ("failed", "conflict", "skipped"):
            return state, "original untouched"
        return "failed", "interrupted before the rename; original untouched"
    return "conflict", "file matches neither the original nor the edit"


def _resolve_restore(item: dict, cur: str | None) -> tuple[str, str]:
    if cur is None:
        return "restore_failed", "file missing"
    if cur == item.get("sha_before"):
        return "restored", "restore had completed before the interruption"
    if cur in (item.get("sha_after"), item.get("sha_expected_current")):
        return "restore_failed", "interrupted before the rename; file untouched"
    return "restore_conflict", "file matches neither the backup nor the expected current version"


def _header_paths(header: dict | None) -> set[str] | None:
    if not header:
        return None
    return {os.path.join(i["root"], i["relpath"]) for i in header.get("items", [])}


def _remove_temps(paths: list[str]) -> tuple[list[str], list[str]]:
    removed, failed = [], []
    for t in paths:
        if not os.path.basename(t).startswith(fsutil.TEMP_PREFIX):
            continue
        if not os.path.isdir(os.path.dirname(t)):
            failed.append(t)  # directory not available: try again later
            continue
        if not os.path.lexists(t):
            continue
        err = fsutil.remove_temp(t)
        (failed if err else removed).append(t)
    return removed, failed


def _first_header(path: str) -> dict | None:
    try:
        with open(path, "rb") as f:
            rec = json.loads(f.readline())
        return rec if rec.get("t") == "header" else None
    except (OSError, ValueError, AttributeError):
        return None


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

    if s["status"] in TERMINAL_JOB:
        if not s["leaked"]:
            return None
        removed, failed = _remove_temps(s["leaked"])
        if removed:
            j = Journal.open_append(path)
            try:
                j.write({"t": "cleanup", "removed": removed})
            finally:
                j.close()
        # A leftover temp of a finished job blocks nothing; it is retried at every recovery.
        return {"journal": path, "job": job, "kind": kind, "status": "cleanup_pending" if failed else "cleaned",
                "temps_removed": removed, "temps_left": failed, "paths": set(), "items": {}}

    temps = [t for ts in s["temps"].values() for t in ts]
    removed, failed_temps = _remove_temps(temps)
    try:
        if failed_temps:
            raise _Defer(f"temp files could not be removed: {failed_temps}")
        hashes = {}
        for hi in header.get("items", []):
            hashes[hi["n"]] = _hash(hi["root"], os.path.join(hi["root"], hi["relpath"]))
    except _Defer as e:
        return {"journal": path, "job": job, "kind": kind, "status": "pending", "error": str(e),
                "temps_removed": removed, "paths": _header_paths(header), "items": {}}

    resolved = {}
    j = Journal.open_append(path)
    try:
        for hi in header.get("items", []):
            n = hi["n"]
            item = {**hi, **s["items"].get(n, {})}
            state = item.get("state", "planned")
            cur = hashes[n]
            if kind == "restore":
                if state in TERMINAL_ITEM:
                    continue
                new, why = _resolve_restore(item, cur)
            else:
                new, why = resolve_apply_item(item, cur)
                if new == state:
                    continue
            j.item(n, new, stage="interrupted", error=why, recovered=True, sha_found=cur,
                   sha_before=item.get("sha_before"), sha_after=item.get("sha_after"))
            resolved[n] = new
        j.job("interrupted", recovered=True, temps_removed=removed)
    finally:
        j.close()
    return {"journal": path, "job": job, "kind": kind, "items": resolved, "temps_removed": removed,
            "status": "interrupted", "paths": set()}


def recover_locked(env: WriteEnv) -> list[dict]:
    """Recover every unfinished journal. The caller must hold the write lock.

    Never raises for one bad journal: it is reported and the others are processed.
    """
    reports = []
    swept = env.store.sweep_tmp()
    if swept:
        reports.append({"journal": None, "job": None, "status": "swept", "parts_removed": swept, "paths": set()})
    for path in sorted(glob.glob(os.path.join(env.journal_dir, "*.jsonl"))):
        try:
            r = recover_journal(path)
        except Exception as e:  # noqa: BLE001 - e.g. the journal cannot be appended to
            header = _first_header(path)
            r = {"journal": path, "job": (header or {}).get("job"), "status": "damaged",
                 "error": f"{type(e).__name__}: {e}", "paths": _header_paths(header)}
        if r is not None:
            reports.append(r)
    return reports


def blocked_paths(reports: list[dict]) -> tuple[set[str], bool]:
    """(paths that new jobs must not touch, whether every apply must be refused)."""
    paths: set[str] = set()
    unknown = False
    for r in reports:
        if r["status"] in ("pending", "damaged"):
            if r.get("paths") is None:
                unknown = True
            else:
                paths |= {os.path.normpath(p) for p in r["paths"]}
    return paths, unknown


def recover(env: WriteEnv) -> list[dict]:
    with env.write_lock():
        return recover_locked(env)

