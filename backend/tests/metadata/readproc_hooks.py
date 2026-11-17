"""Test-only reader hooks for readproc (imported inside the child process)."""
import os
import time

from gama.metadata.reader import read_file


def sleepy(path: str):
    if "HANG" in path:
        time.sleep(600)
    return read_file(path)


def crashy(path: str):
    if "CRASH" in path:
        os._exit(9)
    return read_file(path)


def noisy(path: str):
    print("library chatter on stdout")  # must not corrupt the protocol
    return read_file(path)


class _Broken(type(read_file("/nonexistent"))):
    def to_dict(self):
        return {"path": self.path}  # missing every other key


def malformed(path: str):
    r = read_file(path)
    return _Broken(**{k: getattr(r, k) for k in ("path", "ok")})
