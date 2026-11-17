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


class JournalWriteError(JournalError):
    """A record could not be written durably. The journal was cut back to the last
    complete record and refuses further writes (poisoned)."""


class Journal:
    def __init__(self, path: str, fd: int):
        self.path = path
        self._fd = fd
        self.poisoned: str | None = None

    @classmethod
    def create(cls, journal_dir: str, job_id: str) -> Journal:
        if not _JOB_RE.fullmatch(job_id):
            raise JournalError(f"bad job id {job_id!r}")
        fsutil.makedirs_durable(journal_dir)
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
        """Append one record durably, or raise JournalWriteError.

        On any failure (short write + ENOSPC, EIO on fsync) the file is cut back to
        where this record started, so no torn line can be followed by later records,
        and the journal refuses all further writes.
        """
        if self.poisoned:
            raise JournalWriteError(f"journal {self.path} is unusable after an earlier write error: {self.poisoned}")
        record = dict(record, ts=time.time())
        line = (json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")
        start = os.lseek(self._fd, 0, os.SEEK_END)
        try:
            fsutil.failpoint("journal_write", fd=self._fd, line=line, record=record)
            view = memoryview(line)
            while view:
                n = os.write(self._fd, view)
                view = view[n:]
            os.fsync(self._fd)
        except Exception as e:  # noqa: BLE001
            self.poisoned = f"{type(e).__name__}: {e}"
            try:
                os.ftruncate(self._fd, start)
                os.fsync(self._fd)
            except OSError:
                pass
            raise JournalWriteError(f"cannot write journal {self.path}: {self.poisoned}") from e

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
        if i == len(lines) - 1:
            # Unterminated last line: a torn write, even if it happens to parse.
            # open_append() cuts it off, so it is never acted on.
            break
        try:
            out.append(json.loads(line))
        except ValueError:
            raise JournalError(f"{path}: damaged record on line {i + 1}") from None
    return out


def summarize(records: list[dict]) -> dict:
    """{"header", "items": {n: item records merged}, "status", "temps": {n: [temp paths]},
    "leaked": [temp paths that could not be removed and were not cleaned up since]}."""
    header = next((r for r in records if r.get("t") == "header"), None)
    items: dict[int, dict] = {}
    temps: dict[int, list[str]] = {}
    leaked: list[str] = []
    cleaned: set[str] = set()
    status = None
    for r in records:
        if r.get("t") == "item":
            cur = items.setdefault(r["n"], {})
            cur.update({k: v for k, v in r.items() if k not in ("t", "ts")})
            if r.get("temp"):
                temps.setdefault(r["n"], []).append(r["temp"])
            if r.get("temp_leaked"):
                leaked.append(r["temp_leaked"])
        elif r.get("t") == "job":
            status = r["status"]
        elif r.get("t") == "cleanup":
            cleaned.update(r.get("removed", []))
    return {"header": header, "items": items, "status": status, "temps": temps,
            "leaked": [t for t in leaked if t not in cleaned]}
