"""Regression tests for the M3 review (planner, iTunes ID3 sizes). Crafted inline in tmp_path."""
import os
import struct
import subprocess

import pytest

from fixtures.build import id3_text_body
from gama.metadata.reader import read_file
from gama.planning.planner import FileRef, PlanError, plan_files

pytestmark = pytest.mark.slow

KC = "King Crimson/1969 - In the Court of the Crimson King"
Z = "Format Zoo/"


def inproc(paths):
    return [read_file(p) for p in paths]


def kc(corpus):
    return sorted(r for r in corpus.manifest if r.startswith(KC + "/"))


# --------------------------------------------------------------------------- #2 same file twice

def test_symlinked_directory_is_refused_before_reading(corpus):
    (corpus.root / "alias").symlink_to(corpus.root / KC)
    first = kc(corpus)[0]
    name = first.rsplit("/", 1)[1]
    seen = []

    def spy(paths):
        seen.extend(paths)
        return inproc(paths)
    p = plan_files([FileRef(str(corpus.root), first), FileRef(str(corpus.root), f"alias/{name}")], "add", ["Jazz"],
                   reader=spy)
    assert [i.action for i in p.items] == ["change", "skip"]
    assert p.items[1].reason == "symlinked-path" and p.n_change == 1
    assert seen == [os.path.join(str(corpus.root), first)]


def test_nested_roots_same_file_planned_once(corpus):
    first = kc(corpus)[0]
    inner = corpus.root / "King Crimson"
    rel_inner = first.split("/", 1)[1]
    p = plan_files([FileRef(str(corpus.root), first), FileRef(str(inner), rel_inner)], "add", ["Jazz"],
                   reader=inproc)
    assert [i.action for i in p.items] == ["change", "skip"] and p.items[1].reason == "duplicate-file"


@pytest.mark.parametrize("link_first", [True, False])
def test_symlinked_file_never_blocks_the_real_file(corpus, link_first):
    first = kc(corpus)[0]
    link = f"{KC}/link.flac"
    (corpus.root / link).symlink_to(corpus.root / first)
    refs = [FileRef(str(corpus.root), first), FileRef(str(corpus.root), link)]
    if link_first:
        refs.reverse()
    p = plan_files(refs, "add", ["Jazz"], reader=inproc)
    by = {i.relpath: i for i in p.items}
    assert by[first].action == "change"
    assert by[link].action == "skip" and by[link].reason == "symlink"


# --------------------------------------------------------------------------- #4 bare string

def test_bare_string_genres_refused(corpus):
    with pytest.raises(PlanError, match="list"):
        plan_files([FileRef(str(corpus.root), kc(corpus)[0])], "replace", "Rock", reader=inproc)


# --------------------------------------------------------------------------- #6 containment before any I/O

def test_outside_root_carries_nothing_and_is_never_read(corpus, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "x.flac").write_bytes(corpus.path(kc(corpus)[0]).read_bytes())
    seen = []

    def spy(paths):
        seen.extend(paths)
        return inproc(paths)
    lib = corpus.root / "King Crimson"
    p = plan_files([FileRef(str(lib), "../../elsewhere/x.flac")], "add", ["Jazz"], reader=spy)
    it = p.items[0]
    assert it.reason == "outside-root" and seen == []
    assert (it.format, it.size, it.inode, it.genres_before, it.sha_before, it.warnings) == (None, None, None, None,
                                                                                           None, [])


# --------------------------------------------------------------------------- #7 hash before and after the read

def _same_size_same_mtime_edit(path):
    st = os.stat(path)
    data = bytearray(path.read_bytes())
    i = data.index(b"Progressive Rock")
    data[i:i + 4] = b"PROG"  # same length: the genre text itself changes
    path.write_bytes(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(path).st_size == st.st_size


@pytest.mark.parametrize("when", ["before-read", "after-read"])
def test_change_with_same_size_and_mtime_is_detected(corpus, when):
    target = corpus.path(kc(corpus)[0])

    def reader(paths):
        if when == "before-read":
            _same_size_same_mtime_edit(target)
        res = inproc(paths)
        if when == "after-read":
            _same_size_same_mtime_edit(target)
        return res
    p = plan_files([FileRef(str(corpus.root), r) for r in kc(corpus)], "add", ["Jazz"], reader=reader)
    assert p.items[0].action == "skip" and p.items[0].reason == "changed-while-planning"
    assert [i.action for i in p.items[1:]] == ["change", "change"]


# --------------------------------------------------------------------------- #1 in the planner

def test_delimited_result_not_round_tripping_becomes_skip(corpus):
    rel = Z + "flac_single_genre.flac"
    subprocess.run(["metaflac", "--remove-tag=GENRE", "--set-tag=GENRE=Rock ; ", str(corpus.path(rel))], check=True)
    r = read_file(corpus.path(rel))
    assert r.writable and r.genre.style == "delimited"
    p = plan_files([FileRef(str(corpus.root), rel)], "add", ["Jazz"], reader=inproc)
    assert p.items[0].action == "skip" and p.items[0].reason == "would-not-round-trip"
    assert p.items[0].genres_after is None


# --------------------------------------------------------------------------- #5 iTunes-style v2.4 sizes

def _plain_frame(fid, body):
    return fid.encode() + struct.pack(">IH", len(body), 0) + body


def _syncsafe(n):
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def test_itunes_plain_frame_sizes_are_not_refused(corpus):
    frames = [("TIT2", id3_text_body(["T" * 300], 3)), ("TALB", id3_text_body(["A" * 200], 3)),
              ("TPE1", id3_text_body(["Artist"], 3)), ("TCON", id3_text_body(["Rock"], 3))]
    body = b"".join(_plain_frame(f, b) for f, b in frames) + b"\0" * 64
    tag = b"ID3\x04\x00\x00" + _syncsafe(len(body)) + body
    p = corpus.root / "itunes.mp3"
    p.write_bytes(tag + corpus.path(Z + "mp3_untagged.mp3").read_bytes())
    r = read_file(p)
    assert r.writable, (r.skip_reason, r.error)
    assert r.tags.fields["title"] == "T" * 300 and r.genre.values == ("Rock",)
    assert r.tags.snapshot["tags"]["raw_frame_ids"] == ["TALB", "TIT2", "TPE1"]
