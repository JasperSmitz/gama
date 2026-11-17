"""Read files in short-lived child processes with timeouts (JSON over stdout).

mutagen runs only in children (plan 1.8): a file that makes the parser hang or
crash costs one child, never the caller. The parent:

* starts ``python -I`` children for chunks of ``chunk_size`` files;
* the child writes one JSON line per file as soon as it is read, so the
  parent always knows which file was in progress;
* if no line arrives within ``timeout`` seconds the child is killed; a child
  that exits early is a crash. Either way the file in progress is retried
  once on its own, isolating it; if that also fails it gets
  ``ok=False, reason="timeout"`` or ``"reader-crashed"``. Reading then
  resumes with the next file.

Results come back in input order, one per path. ``read_files`` never raises
for a bad file.
"""
from __future__ import annotations

import importlib
import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from gama.metadata.model import ReadResult

DEFAULT_READER = "gama.metadata.reader:read_file"
DEFAULT_TIMEOUT = 30.0
DEFAULT_CHUNK = 50

_SRC_DIR = str(Path(__file__).resolve().parents[2])
_BOOTSTRAP = ("import sys,json;r=json.loads(sys.stdin.readline());sys.path[:0]=r['sys_path'];"
              "from gama.metadata.readproc import child_main;child_main(r)")


def read_files(paths: Iterable[str | Path], *, timeout: float = DEFAULT_TIMEOUT, chunk_size: int = DEFAULT_CHUNK,
               reader: str = DEFAULT_READER, extra_sys_path: Sequence[str] = ()) -> list[ReadResult]:
    """Read every path in child processes. ``reader`` ("module:function") is a test hook."""
    if ":" not in reader:
        raise ValueError("reader must be 'module:function'")
    paths = [str(p) for p in paths]
    sys_path = [_SRC_DIR, *map(str, extra_sys_path)]
    results: list[ReadResult | None] = [None] * len(paths)
    i = 0
    while i < len(paths):
        chunk = paths[i:i + chunk_size]
        done, failure = _run_child(chunk, timeout, reader, sys_path)
        for d in done:
            results[i] = ReadResult.from_dict(d)
            i += 1
        if failure is None:
            continue
        # Isolate the file that was in progress: retry it alone.
        suspect = paths[i]
        done1, failure1 = _run_child([suspect], timeout, reader, sys_path)
        if done1:
            results[i] = ReadResult.from_dict(done1[0])
        else:
            kind, detail = failure1 or failure
            results[i] = ReadResult(suspect, False, reason=kind, error=detail, skip_reason="unreadable")
        i += 1
    return [r for r in results if r is not None]


def _run_child(paths: list[str], timeout: float, reader: str, sys_path: list[str]
               ) -> tuple[list[dict], tuple[str, str] | None]:
    """Run one child over ``paths``. Returns (results in order, failure or None)."""
    req = json.dumps({"sys_path": sys_path, "reader": reader, "paths": paths}) + "\n"
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen([sys.executable, "-I", "-c", _BOOTSTRAP], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=err)
        done: list[dict] = []
        failure: tuple[str, str] | None = None
        try:
            proc.stdin.write(req.encode())
            proc.stdin.close()
            fd = proc.stdout.fileno()
            sel = selectors.DefaultSelector()
            sel.register(fd, selectors.EVENT_READ)
            buf = b""
            deadline = time.monotonic() + timeout
            eof = False
            while not eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = ("timeout", f"no result within {timeout:g}s")
                    break
                if not sel.select(remaining):
                    continue
                data = os.read(fd, 1 << 16)
                if not data:
                    eof = True
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    d = json.loads(line)
                    if len(done) >= len(paths) or d.get("path") != paths[len(done)]:
                        failure = ("reader-crashed", "child returned a result for the wrong file")
                        eof = True
                        break
                    done.append(d)
                    deadline = time.monotonic() + timeout
            sel.close()
        except (OSError, ValueError) as e:  # broken pipe, bad JSON from a dying child
            failure = ("reader-crashed", f"{type(e).__name__}: {e}")
        finally:
            if proc.poll() is None:
                if failure is None and len(done) == len(paths):
                    try:
                        proc.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                else:
                    proc.kill()
            proc.wait()
            proc.stdout.close()
        if failure is None and len(done) < len(paths):
            err.seek(0)
            tail = err.read()[-400:].decode("utf-8", "replace").strip()
            failure = ("reader-crashed", f"reader exited with {proc.returncode}: {tail}")
    return done, failure


def child_main(req: dict) -> None:
    """Child entry point. Results go to the original stdout; anything the
    libraries print goes to stderr, so it cannot corrupt the protocol."""
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    mod_name, func_name = req["reader"].split(":", 1)
    fn = getattr(importlib.import_module(mod_name), func_name)
    for p in req["paths"]:
        try:
            r = fn(p)
        except Exception as e:  # noqa: BLE001 - read_file never raises, but a hook might
            r = ReadResult(p, False, reason="unreadable", error=f"{type(e).__name__}: {e}"[:500],
                           skip_reason="unreadable")
        out.write(json.dumps(r.to_dict()) + "\n")
        out.flush()
    out.close()
