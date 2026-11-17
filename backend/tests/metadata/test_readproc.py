import shutil
import time
from pathlib import Path

import pytest

from gama.metadata.reader import read_file
from gama.metadata.readproc import read_files

pytestmark = pytest.mark.slow

BACKEND = str(Path(__file__).resolve().parents[2])
SRC = "Format Zoo/flac_single_genre.flac"


def test_matches_in_process_read(corpus_master):
    paths = [corpus_master.master / rel for rel in sorted(corpus_master.manifest)]
    got = read_files(paths, chunk_size=7)
    assert [r.path for r in got] == [str(p) for p in paths]
    assert [r.to_dict() for r in got] == [read_file(p).to_dict() for p in paths]


def test_empty_input():
    assert read_files([]) == []


def test_bad_reader_spec():
    with pytest.raises(ValueError):
        read_files(["x"], reader="nocolon")


def _files(corpus, names):
    out = []
    for n in names:
        p = corpus.root / n
        shutil.copyfile(corpus.path(SRC), p)
        out.append(p)
    return out


def test_hanging_reader_is_killed_and_isolated(corpus):
    paths = _files(corpus, ["a.flac", "b_HANG.flac", "c.flac", "d.flac"])
    t0 = time.monotonic()
    got = read_files(paths, timeout=1.5, chunk_size=50, reader="tests.metadata.readproc_hooks:sleepy",
                     extra_sys_path=[BACKEND])
    elapsed = time.monotonic() - t0
    assert [r.path for r in got] == [str(p) for p in paths]
    assert [r.ok for r in got] == [True, False, True, True]
    assert got[1].reason == "timeout" and got[1].skip_reason == "unreadable"
    assert elapsed < 15  # chunk timeout + isolated retry, not the 600 s sleep


def test_crashing_reader_is_isolated(corpus):
    paths = _files(corpus, ["a.flac", "b.flac", "c_CRASH.flac", "d.flac"])
    got = read_files(paths, chunk_size=3, reader="tests.metadata.readproc_hooks:crashy", extra_sys_path=[BACKEND])
    assert [r.ok for r in got] == [True, True, False, True]
    assert got[2].reason == "reader-crashed" and "9" in got[2].error


def test_stdout_chatter_does_not_corrupt_protocol(corpus):
    paths = _files(corpus, ["a.flac", "b.flac"])
    got = read_files(paths, reader="tests.metadata.readproc_hooks:noisy", extra_sys_path=[BACKEND])
    assert [r.ok for r in got] == [True, True]


def test_unicode_and_odd_paths(corpus):
    paths = _files(corpus, ["ünï cødé ✓.flac", "new\nline.flac", "-dash.flac"])
    got = read_files(paths)
    assert [r.path for r in got] == [str(p) for p in paths]
    assert all(r.ok for r in got)
