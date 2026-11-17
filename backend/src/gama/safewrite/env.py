"""The environment a write or restore job runs in: data dir layout, injectable
hooks (free space, euid, mutate, verify) and the global write lock."""
from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from gama.safewrite import fsutil
from gama.safewrite.backups import BackupStore

MIB = 1 << 20


class LockBusy(RuntimeError):
    """Another apply or restore job holds the write lock."""


def _default_mutate(*a, **kw):
    from gama.safewrite.mutate import run_mutate
    return run_mutate(*a, **kw)


def _default_verify(*a, **kw):
    from gama.safewrite.verify import verify_temp
    return verify_temp(*a, **kw)


@dataclass
class WriteEnv:
    data_dir: str
    margin_bytes: int = 64 * MIB
    audio: str = "auto"                    # verify_audio: auto | off
    mutate_timeout: float = 60.0
    free_space: Callable[[str], int] = fsutil.free_space
    euid: Callable[[], int] = os.geteuid
    mutate: Callable = field(default=_default_mutate)
    verify: Callable = field(default=_default_verify)

    def __post_init__(self):
        self.data_dir = os.fspath(self.data_dir)
        for d in (self.journal_dir, self.lock_dir):
            os.makedirs(d, mode=0o700, exist_ok=True)
        self.store = BackupStore(os.path.join(self.data_dir, "backups"))

    @property
    def journal_dir(self) -> str:
        return os.path.join(self.data_dir, "journal")

    @property
    def lock_dir(self) -> str:
        return os.path.join(self.data_dir, "locks")

    @contextlib.contextmanager
    def write_lock(self):
        """Only one apply or restore job at a time (flock; released if the process dies)."""
        fd = os.open(os.path.join(self.lock_dir, "write.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LockBusy("another write or restore job is running") from None
            yield
        finally:
            os.close(fd)
