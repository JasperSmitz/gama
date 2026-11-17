"""Filesystem helpers for the safe write engine: hashing, durability, attributes,
free space, and failpoints for crash tests."""
from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable

TEMP_PREFIX = ".gama-tmp-"
CHUNK = 1 << 20


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def fsync_file(path: str | os.PathLike) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: str | os.PathLike) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def free_space(path: str | os.PathLike) -> int:
    """Bytes available to an unprivileged writer on the filesystem holding ``path``."""
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def copy_stream(src: str | os.PathLike, dst_fd: int) -> str:
    """Copy ``src`` into the open ``dst_fd``; return sha256 of the bytes written. fsyncs."""
    h = hashlib.sha256()
    with open(src, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
            view = memoryview(chunk)
            while view:
                n = os.write(dst_fd, view)
                view = view[n:]
    os.fsync(dst_fd)
    return h.hexdigest()


def copy_attrs(src: str | os.PathLike, dst: str | os.PathLike) -> list[str]:
    """Give ``dst`` the group, xattrs (incl. POSIX ACLs) and mode of ``src``.

    Group change failure raises. xattrs: ENOTSUP on either side is recorded in
    the returned notes; any other error raises. mtime is deliberately not copied,
    so scanners notice the change. Order: group, xattrs, then mode (setting an
    ACL rewrites the group mode bits).
    """
    notes: list[str] = []
    st = os.stat(src, follow_symlinks=False)
    if os.stat(dst).st_gid != st.st_gid:
        os.chown(dst, -1, st.st_gid)
    try:
        names = os.listxattr(src, follow_symlinks=False)
        have = os.listxattr(dst, follow_symlinks=False)
    except OSError as e:
        if e.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
        notes.append("xattrs: not supported by the filesystem")
    else:
        for name in have:
            if name not in names:
                os.removexattr(dst, name, follow_symlinks=False)
        for name in names:
            os.setxattr(dst, name, os.getxattr(src, name, follow_symlinks=False), follow_symlinks=False)
    os.chmod(dst, stat.S_IMODE(st.st_mode))
    return notes


# --------------------------------------------------------------------------- failpoints

class FailpointError(RuntimeError):
    """Raised by a failpoint set to 'raise'."""


_FAILPOINTS: dict[str, Callable[[dict], None] | str] = {}
EXIT_CODE = 77


def set_failpoint(name: str, action: Callable[[dict], None] | str) -> None:
    """Test hook. ``action`` is "raise", "exit" (os._exit), or a callable(ctx)."""
    _FAILPOINTS[name] = action


def clear_failpoints() -> None:
    _FAILPOINTS.clear()


def _env_failpoints() -> dict[str, str]:
    """GAMA_FAILPOINTS="name[:n],..." with GAMA_ENABLE_FAILPOINTS=1: os._exit at that point
    (for item n only, if given). Used by crash tests that run the engine in a subprocess."""
    if os.environ.get("GAMA_ENABLE_FAILPOINTS") != "1":
        return {}
    out = {}
    for spec in filter(None, os.environ.get("GAMA_FAILPOINTS", "").split(",")):
        name, _, n = spec.partition(":")
        out[name] = n
    return out


def failpoint(name: str, **ctx) -> None:
    """Called by the engine at each step. A no-op unless a test armed it."""
    env = _env_failpoints()
    if name in env and (env[name] == "" or str(ctx.get("n")) == env[name]):
        os._exit(EXIT_CODE)
    action = _FAILPOINTS.get(name)
    if action is None:
        return
    if action == "raise":
        raise FailpointError(name)
    if action == "exit":
        os._exit(EXIT_CODE)
    action(dict(ctx, name=name))
