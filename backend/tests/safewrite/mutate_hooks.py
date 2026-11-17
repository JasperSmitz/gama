"""Test-only writer hooks for safewrite.mutate (imported inside the child)."""
import os
import time


def sleepy(path, fmt, values, expect_before):
    time.sleep(600)


def crashy(path, fmt, values, expect_before):
    with open(path, "r+b") as f:  # half-written temp file, then die
        f.write(b"\0" * 64)
    os._exit(7)
