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
