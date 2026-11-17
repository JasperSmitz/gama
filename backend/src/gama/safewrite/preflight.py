"""Preflight checks (plan 1.6): run before any byte is written, and again per file."""
from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from gama.safewrite import fsutil

MIB = 1 << 20


@dataclass(frozen=True)
class Target:
    """What preflight needs to know about one file."""
    n: int
    root: str
    relpath: str
    sha_before: str

    @property
    def path(self) -> str:
        return os.path.join(self.root, self.relpath)


class PreflightError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def check_location(t: Target) -> None:
    """Regular file, not a symlink, no symlinked directory below the root, inside the root."""
    root_real = os.path.realpath(t.root)
    lexical = os.path.normpath(os.path.join(root_real, t.relpath))
    if not (lexical.startswith(root_real.rstrip(os.sep) + os.sep)):
        raise PreflightError("outside-root", f"{t.relpath} is outside {t.root}")
    if os.path.realpath(os.path.dirname(lexical)) != os.path.dirname(lexical):
        raise PreflightError("symlinked-path", f"a directory on the path of {t.relpath} is a symlink")
    try:
        st = os.lstat(lexical)
    except FileNotFoundError:
        raise PreflightError("missing", f"{t.relpath} does not exist") from None
    if stat.S_ISLNK(st.st_mode):
        raise PreflightError("symlink", f"{t.relpath} is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise PreflightError("not-regular-file", f"{t.relpath} is not a regular file")


def check_item(t: Target, euid: int, *, check_hash: bool = True) -> os.stat_result:
    """Every per-file condition. Raises PreflightError; returns the lstat result."""
    check_location(t)
    st = os.lstat(t.path)
    if st.st_nlink != 1:
        raise PreflightError("hardlink", f"{t.relpath} has {st.st_nlink} hard links")
    if st.st_uid != euid:
        raise PreflightError("not-owner", f"{t.relpath} is owned by uid {st.st_uid}, not {euid}")
    # rename() over a read-only file succeeds, so the file's own mode is checked explicitly.
    if not os.access(t.path, os.W_OK):
        raise PreflightError("not-writable", f"{t.relpath} is not writable")
    d = os.path.dirname(t.path)
    if not os.access(d, os.W_OK | os.X_OK):
        raise PreflightError("dir-not-writable", f"directory of {t.relpath} is not writable")
    if check_hash and fsutil.sha256_file(t.path) != t.sha_before:
        raise PreflightError("conflict", f"{t.relpath} changed since the preview")
    return st


def check_backup_space(sizes_needed: Iterable[int], store_root: str, margin: int,
                       free_space: Callable[[str], int]) -> None:
    need = sum(sizes_needed) + margin
    have = free_space(store_root)
    if have < need:
        raise PreflightError("no-space-backups",
                             f"backup store needs {need} bytes free, has {have}")


def temp_space_needed(size: int, margin: int) -> int:
    return int(size * 1.1) + MIB + margin


def check_library_space(path: str, size: int, margin: int, free_space: Callable[[str], int]) -> None:
    need = temp_space_needed(size, margin)
    have = free_space(os.path.dirname(path))
    if have < need:
        raise PreflightError("no-space-library", f"{os.path.dirname(path)} needs {need} bytes free, has {have}")
