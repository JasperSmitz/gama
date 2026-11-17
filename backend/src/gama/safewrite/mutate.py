"""Apply a genre change to a temp copy, in a child process (plan 1.6 step 3).

The parent (``run_mutate``) starts ``python -I`` with a JSON request on stdin
and kills it after ``timeout`` seconds. The child (``child_main``):

1. re-reads the temp file and refuses unless it is writable by policy, has the
   planned format, and its stored genres still equal ``expect_before``;
2. writes exactly ``values`` with the format's ``apply_genres``;
3. prints one JSON result line.

The parent owns every rename; killing the child can only leave a half-written
*temp* file, which verification rejects and the caller removes. mutagen is
imported only in the child.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 60.0
DEFAULT_HOOK = "gama.safewrite.mutate:mutate_file"
_SRC_DIR = str(Path(__file__).resolve().parents[2])
_BOOTSTRAP = ("import sys,json;r=json.loads(sys.stdin.readline());sys.path[:0]=r['sys_path'];"
              "from gama.safewrite.mutate import child_main;child_main(r)")


@dataclass(frozen=True)
class MutateResult:
    ok: bool
    error: str | None = None


class MutateRefused(RuntimeError):
    """The child refused to write (file not in the planned state)."""


def mutate_file(path: str, fmt: str, values: Sequence[str], expect_before: Sequence[str]) -> None:
    """Child side: check the temp file, then write ``values``. Raises on any problem."""
    from gama.metadata.formats import id3, mp4, vorbis
    from gama.metadata.reader import read_file

    r = read_file(path)
    if not r.ok:
        raise MutateRefused(f"temp file unreadable: {r.reason}: {r.error}")
    if r.skip_reason is not None:
        raise MutateRefused(f"temp file refused by policy: {r.skip_reason}")
    if r.format != fmt:
        raise MutateRefused(f"format is {r.format}, planned {fmt}")
    if list(r.genre.values) != list(expect_before):
        raise MutateRefused(f"genres are {list(r.genre.values)}, planned from {list(expect_before)}")
    values = list(values)
    if fmt in vorbis.CLASSES:
        vorbis.apply_genres(path, fmt, values)
    elif fmt == "mp3":
        id3.apply_genres(path, values)
    elif fmt == "mp4":
        mp4.apply_genres(path, values)
    else:
        raise MutateRefused(f"no writer for {fmt}")


def child_main(req: dict) -> None:
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        import importlib

        mod, func = req["hook"].split(":", 1)
        getattr(importlib.import_module(mod), func)(req["path"], req["format"], req["values"], req["expect_before"])
        res = {"ok": True, "error": None}
    except Exception as e:  # noqa: BLE001 - every failure is reported, never raised
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:1000]}
    out.write(json.dumps(res) + "\n")
    out.flush()
    out.close()


def run_mutate(path: str | Path, fmt: str, values: Sequence[str], expect_before: Sequence[str], *,
               timeout: float = DEFAULT_TIMEOUT, hook: str = DEFAULT_HOOK,
               extra_sys_path: Sequence[str] = ()) -> MutateResult:
    """Run the mutation in a child. Never raises; a timeout kills the child."""
    req = {"sys_path": [_SRC_DIR, *map(str, extra_sys_path)], "hook": hook, "path": str(path), "format": fmt,
           "values": list(values), "expect_before": list(expect_before)}
    try:
        proc = subprocess.Popen([sys.executable, "-I", "-c", _BOOTSTRAP], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        return MutateResult(False, f"cannot start writer: {type(e).__name__}: {e}")
    try:
        out, err = proc.communicate((json.dumps(req) + "\n").encode(), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return MutateResult(False, f"timeout: writer killed after {timeout:g}s")
    lines = out.decode("utf-8", "replace").splitlines()
    try:
        res = json.loads(lines[-1]) if lines else None
    except ValueError:
        res = None
    if proc.returncode != 0 or not isinstance(res, dict) or "ok" not in res:
        tail = err.decode("utf-8", "replace").strip()[-400:]
        return MutateResult(False, f"writer crashed (exit {proc.returncode}): {tail}")
    return MutateResult(bool(res["ok"]), res.get("error"))
