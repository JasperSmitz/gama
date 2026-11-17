"""Append-only JSONL journal, one file per job (plan 1.7).

The file is created with ``O_EXCL`` (S4: an existing journal is never reused or
overwritten). Every record is written and fsynced before the step it describes
proceeds. A torn last line (crash mid-write) is ignored on load; a damaged line
anywhere else is an error.

Records:
  {"t": "header", "job": id, "kind": "apply"|"restore", "items": [...], ...}
  {"t": "item", "n": n, "state": ..., ...}     item state transitions
  {"t": "job", "status": ...}                  job status; terminal ones end the job
"""
from __future__ import annotations

import json
import os
import re
import time

from gama.safewrite import fsutil

APPLY_STATES = ("planned", "backed_up", "temp_created", "temp_written", "verified", "renaming", "committed",
                "failed", "conflict", "skipped")
RESTORE_STATES = ("restoring", "restored", "restore_conflict", "restore_failed", "nothing_to_do")
TERMINAL_ITEM = {"committed", "failed", "conflict", "skipped", "restored", "restore_conflict", "restore_failed",
                 "nothing_to_do"}
TERMINAL_JOB = {"succeeded", "completed_with_errors", "rolled_back", "failed_partial", "conflict", "failed",
                "interrupted"}
_JOB_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


class JournalError(RuntimeError):
    pass


class Journal:
    def __init__(self, path: str, fd: int):
        self.path = path
        self._fd = fd

    @classmethod
    def create(cls, journal_dir: str, job_id: str) -> Journal:
        if not _JOB_RE.fullmatch(job_id):
            raise JournalError(f"bad job id {job_id!r}")
        os.makedirs(journal_dir, mode=0o700, exist_ok=True)
        path = os.path.join(journal_dir, f"{job_id}.jsonl")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        except FileExistsError:
            raise JournalError(f"journal {path} already exists; refusing to reuse it") from None
        fsutil.fsync_dir(journal_dir)
        return cls(path, fd)

    @classmethod
    def open_append(cls, path: str) -> Journal:
        """Reopen an existing journal (recovery only). A torn last line is cut off first."""
        with open(path, "rb") as f:
            data = f.read()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        if data and not data.endswith(b"\n"):
            os.ftruncate(fd, data.rfind(b"\n") + 1)
            os.fsync(fd)
        return cls(path, fd)

    def write(self, record: dict) -> None:
        record = dict(record, ts=time.time())
        line = (json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")
        view = memoryview(line)
        while view:
            n = os.write(self._fd, view)
            view = view[n:]
        os.fsync(self._fd)

    def item(self, n: int, state: str, **fields) -> None:
        self.write({"t": "item", "n": n, "state": state, **fields})

    def job(self, status: str, **fields) -> None:
        self.write({"t": "job", "status": status, **fields})

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def load(path: str) -> list[dict]:
    """All records; a torn (unparseable, unterminated) last line is ignored."""
    with open(path, "rb") as f:
        data = f.read()
    lines = data.split(b"\n")
    out = []
    for i, line in enumerate(lines):
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            if i == len(lines) - 1:  # torn final write: no trailing newline
                break
            raise JournalError(f"{path}: damaged record on line {i + 1}") from None
    return out


def summarize(records: list[dict]) -> dict:
    """{"header", "items": {n: item records merged}, "status", "temps": {n: [temp paths]}}."""
    header = next((r for r in records if r.get("t") == "header"), None)
    items: dict[int, dict] = {}
    temps: dict[int, list[str]] = {}
    status = None
    for r in records:
        if r.get("t") == "item":
            cur = items.setdefault(r["n"], {})
            cur.update({k: v for k, v in r.items() if k not in ("t", "ts")})
            if r.get("temp"):
                temps.setdefault(r["n"], []).append(r["temp"])
        elif r.get("t") == "job":
            status = r["status"]
    return {"header": header, "items": items, "status": status, "temps": temps}
