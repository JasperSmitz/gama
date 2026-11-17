"""Crash recovery (plan 1.7). Runs at worker start and before every job.

For each journal without a terminal job record:

1. delete journaled temp files that still exist (only ``.gama-tmp-`` names);
2. resolve every non-terminal item by the file's **current hash**, not by its
   journaled state (S3):
   * apply item:   ``== sha_after`` -> committed, ``== sha_before`` ->
     failed(interrupted), anything else -> conflict; an interrupted rollback
     (``restoring``) resolves to restored / committed / conflict the same way;
   * restore item: ``== sha_before`` -> restored, else restore_failed /
     restore_conflict;
3. mark the job ``interrupted``.

Recovery never continues a job. The user reviews the result and can restore.
"""
from __future__ import annotations

import glob
import os

from gama.safewrite import fsutil
from gama.safewrite.env import WriteEnv
from gama.safewrite.journal import TERMINAL_ITEM, TERMINAL_JOB, Journal, load, summarize


def _hash(path: str) -> str | None:
    try:
        return fsutil.sha256_file(path)
    except FileNotFoundError:
        return None


def _resolve_apply(item: dict, cur: str | None) -> tuple[str, str]:
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


def recover_journal(path: str) -> dict | None:
    """Recover one journal; returns a report, or None if the job had finished."""
    s = summarize(load(path))
    if s["status"] in TERMINAL_JOB or s["header"] is None:
        return None
    header = s["header"]
    kind = header.get("kind", "apply")
    j = Journal.open_append(path)
    removed, resolved = [], {}
    try:
        for n, temps in s["temps"].items():
            for t in temps:
                if os.path.basename(t).startswith(fsutil.TEMP_PREFIX) and os.path.lexists(t):
                    os.unlink(t)
                    fsutil.fsync_dir(os.path.dirname(t))
                    removed.append(t)
        for hi in header.get("items", []):
            n = hi["n"]
            item = {**hi, **s["items"].get(n, {})}
            state = item.get("state", "planned")
            file_path = os.path.join(hi["root"], hi["relpath"])
            cur = _hash(file_path)
            if kind == "restore":
                if state in TERMINAL_ITEM:
                    continue
                new, why = _resolve_restore(item, cur)
            else:
                new, why = _resolve_apply(item, cur)
                if new == state:
                    continue
            j.item(n, new, stage="interrupted", error=why, recovered=True, sha_found=cur)
            resolved[n] = new
        j.job("interrupted", recovered=True, temps_removed=removed)
    finally:
        j.close()
    return {"job": header.get("job"), "kind": kind, "journal": path, "items": resolved,
            "temps_removed": removed, "status": "interrupted"}


def recover_locked(env: WriteEnv) -> list[dict]:
    """Recover every unfinished journal. The caller must hold the write lock."""
    reports = []
    for path in sorted(glob.glob(os.path.join(env.journal_dir, "*.jsonl"))):
        r = recover_journal(path)
        if r is not None:
            reports.append(r)
    return reports


def recover(env: WriteEnv) -> list[dict]:
    with env.write_lock():
        return recover_locked(env)
