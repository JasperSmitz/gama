"""Content-addressed backup store (plan 1.6 Phase A).

Layout: ``<store>/sha256/ab/<sha256>`` (read-only, 0400) and ``<store>/tmp/`` for
partial writes. A backup is written to a temp file opened ``O_CREAT|O_EXCL``,
fsynced, re-hashed, then hard-linked to its final name (``os.link`` never
replaces an existing name) and the directories fsynced.

A backup is **never overwritten or deleted** (S1). If the final name exists it
is re-hashed and reused only when it matches; otherwise the operation fails.
Partial temp files are removed on every failure (S9).
"""
from __future__ import annotations

import os
import re
import secrets

from gama.safewrite import fsutil

_SHA_RE = re.compile(r"[0-9a-f]{64}")


class BackupError(RuntimeError):
    """A backup could not be created or does not match its hash."""


class BackupStore:
    def __init__(self, root: str | os.PathLike):
        self.root = os.fspath(root)
        self.tmp = os.path.join(self.root, "tmp")
        fsutil.makedirs_durable(os.path.join(self.root, "sha256"))
        fsutil.makedirs_durable(self.tmp)

    def path_for(self, sha: str) -> str:
        if not _SHA_RE.fullmatch(sha):
            raise BackupError(f"not a sha256: {sha!r}")
        return os.path.join(self.root, "sha256", sha[:2], sha)

    def relpath_for(self, sha: str) -> str:
        return os.path.relpath(self.path_for(sha), self.root)

    def sweep_tmp(self) -> list[str]:
        """Delete leftover ``*.part`` files (a crash during a copy). Call under the write lock."""
        removed = []
        for name in sorted(os.listdir(self.tmp)):
            if name.endswith(".part"):
                os.unlink(os.path.join(self.tmp, name))
                removed.append(name)
        if removed:
            fsutil.fsync_dir(self.tmp)
        return removed

    def has(self, sha: str) -> bool:
        return os.path.exists(self.path_for(sha))

    def verify(self, sha: str) -> bool:
        """True if the backup exists and its content still hashes to ``sha``."""
        try:
            return fsutil.sha256_file(self.path_for(sha)) == sha
        except OSError:
            return False

    def ensure(self, src: str | os.PathLike, sha: str) -> tuple[str, bool]:
        """Make sure a verified backup of ``src`` (which must hash to ``sha``) exists.

        Returns (backup path, created). Raises BackupError; never overwrites.
        """
        final = self.path_for(sha)
        if os.path.exists(final):
            if not self.verify(sha):
                raise BackupError(f"existing backup {final} does not match its hash; refusing to touch it")
            return final, False
        fsutil.makedirs_durable(os.path.dirname(final))
        tmp = os.path.join(self.tmp, f"{sha}.{secrets.token_hex(6)}.part")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                fsutil.failpoint("backup_copy", path=os.fspath(src), sha=sha)
                got = fsutil.copy_stream(src, fd)
            finally:
                os.close(fd)
            if got != sha:
                raise BackupError(f"{src} changed: expected sha256 {sha}, copied {got}")
            if fsutil.sha256_file(tmp) != sha:
                raise BackupError(f"backup temp {tmp} does not read back correctly")
            try:
                os.link(tmp, final)
            except FileExistsError:
                # Another writer got there first: reuse only if it matches.
                if not self.verify(sha):
                    raise BackupError(f"existing backup {final} does not match its hash") from None
                return final, False
            os.chmod(final, 0o400)
            fsutil.fsync_dir(os.path.dirname(final))
            return final, True
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            fsutil.fsync_dir(self.tmp)
