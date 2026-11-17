"""Regression tests for the M2 review: files that look writable but whose write
would silently lose data. Every case is crafted inline in tmp_path.
"""
import struct

import pytest
from mutagen.mp4 import MP4

from fixtures.build import (id3_text_body, id3_txxx_body, id3v2_tag, mp4_atoms, mp4_box, mp4_data, mp4_set_ilst,
                            mp4_text_item)
from gama.metadata import policy, readproc
from gama.metadata.reader import read_file
from gama.metadata.readproc import read_files

pytestmark = pytest.mark.slow

Z = "Format Zoo/"
BACKEND = str(__import__("pathlib").Path(__file__).resolve().parents[2])


def mp3(corpus, name, frames, version=4):
    """An MP3 with exactly ``frames`` (raw bodies) in an ID3v2.<version> tag."""
    audio = corpus.path(Z + "mp3_untagged.mp3").read_bytes()
    p = corpus.root / name
    p.write_bytes(id3v2_tag(frames, version) + audio)
    return p


BASE4 = [("TIT2", id3_text_body(["Title"], 3)), ("TCON", id3_text_body(["Rock"], 3))]
BASE3 = [("TIT2", id3_text_body(["Title"], 0)), ("TCON", id3_text_body(["Rock"], 0))]


def test_crafted_baselines_are_writable(corpus):
    assert read_file(mp3(corpus, "ok4.mp3", BASE4)).writable
    r = read_file(mp3(corpus, "ok3.mp3", BASE3 + [("TYER", id3_text_body(["1972"], 0)),
                                                   ("TDAT", id3_text_body(["1503"], 0))], 3))
    assert r.writable and "id3v2.3-upgrade" in r.warnings


# --------------------------------------------------------------------------- 1: frames mutagen drops or merges

@pytest.mark.parametrize("extra", [
    [("TALB", b"\x09bad encoding byte")],          # junk frame: invalid text encoding
    [("TALB", b"")],                               # zero-size frame
    [("TAL$", b"\x00weird id")],                   # invalid frame ID
    [("RVA2", b"track\x00\x01\x00\x10\x00"), ("RVA2", b"track\x00\x01\x00\x20\x00")],  # duplicate HashKey (#754)
    [("GEOB", b"\x00app/x\x00f\x00d\x00AAAA"), ("GEOB", b"\x00app/x\x00f\x00d\x00BBBB")],
    [("TXXX", id3_txxx_body("dup", "one", 3)), ("TXXX", id3_txxx_body("dup", "two", 3))],
])
def test_dropped_or_merged_frames_refused(corpus, extra):
    r = read_file(mp3(corpus, "x.mp3", BASE4 + extra))
    # An invalid text encoding byte is reported as such; both codes refuse the write.
    assert r.ok and r.skip_reason in ("id3-frames-dropped", "non-utf8-text"), (r.skip_reason, r.warnings)
    assert r.skip_reason in policy.SKIP


def test_raw_frame_ids_in_snapshot(corpus):
    r = read_file(mp3(corpus, "x.mp3", BASE4 + [("TPE1", id3_text_body(["A"], 3))]))
    assert r.tags.snapshot["tags"]["raw_frame_ids"] == ["TIT2", "TPE1"]


# --------------------------------------------------------------------------- 2: v2.3 -> v2.4 upgrade losses

@pytest.mark.parametrize("extra", [
    [("RVAD", b"\x03\x10" + b"\x00\x10" * 4)],
    [("EQUA", b"\x10\x80\x00\x00\x01")],
    [("TSIZ", id3_text_body(["123456"], 0))],
    [("TRDA", id3_text_body(["June 1972"], 0))],
    [("TYER", id3_text_body(["c. 1972"], 0))],                                     # not convertible
    [("TYER", id3_text_body(["1972"], 0)), ("TDRC", id3_text_body(["1973"], 0))],  # TDRC already there
    [("TDAT", id3_text_body(["1503"], 0))],                                        # TDAT without TYER
    [("TORY", id3_text_body(["1970"], 0)), ("TDOR", id3_text_body(["1971"], 0))],
])
def test_v23_lossy_upgrade_refused(corpus, extra):
    r = read_file(mp3(corpus, "x.mp3", BASE3 + extra, 3))
    # mutagen does not parse EQUA at all (unknown frame), which is refused for v2.3 too.
    assert r.ok and r.skip_reason in ("id3v2.3-lossy-frames", "id3v2.3-unknown-frames"), r.tags.native
    assert r.tags.native["upgrade_lossy_frames"] or r.skip_reason == "id3v2.3-unknown-frames"


@pytest.mark.parametrize("extra", [
    [("TYER", id3_text_body(["1972"], 0))],
    [("TYER", id3_text_body(["1972"], 0)), ("TDAT", id3_text_body(["1503"], 0)), ("TIME", id3_text_body(["1230"], 0))],
    [("TORY", id3_text_body(["1970"], 0))],
])
def test_v23_lossless_upgrade_allowed(corpus, extra):
    r = read_file(mp3(corpus, "x.mp3", BASE3 + extra, 3))
    assert r.writable, (r.skip_reason, r.tags.native)


def test_v23_snapshot_is_the_tag_as_stored(corpus):
    r = read_file(mp3(corpus, "x.mp3", BASE3 + [("TYER", id3_text_body(["1972"], 0)),
                                                ("TDAT", id3_text_body(["1503"], 0))], 3))
    t = r.tags.snapshot["tags"]
    # Pinned for M4: the verifier must map these deliberately (no update_to_v24 in the snapshot).
    assert t["id3_version"] == "2.3"
    assert t["raw_frame_ids"] == ["TDAT", "TIT2", "TYER"]
    assert {k for k, _ in t["frames"]} == {"TIT2", "TYER", "TDAT"}
    assert r.tags.fields["date"] == "1972"


def test_v24_tag_with_v23_frames_keeps_them_in_snapshot(corpus):
    r = read_file(mp3(corpus, "x.mp3", BASE4 + [("TYER", id3_text_body(["1972"], 3)),
                                                ("RVAD", b"\x03\x10" + b"\x00\x10" * 4)]))
    assert r.writable  # a v2.4 save does not upgrade, so nothing is dropped
    assert {"TYER", "RVAD"} <= {k for k, _ in r.tags.snapshot["tags"]["frames"]}


# --------------------------------------------------------------------------- 5: unknown frames in v2.3

def test_v23_unknown_frame_refused(corpus):
    r = read_file(mp3(corpus, "x.mp3", BASE3 + [("XABC", b"\x00some data")], 3))
    assert r.ok and r.skip_reason == "id3v2.3-unknown-frames"


def test_v24_unknown_frame_allowed(corpus):
    r = read_file(mp3(corpus, "x.mp3", BASE4 + [("XABC", b"\x00some data")]))
    assert r.writable and r.tags.snapshot["tags"]["unknown_frames"]


# --------------------------------------------------------------------------- 3: invalid UTF-8 Vorbis comment

def test_invalid_utf8_vorbis_comment(corpus):
    import subprocess
    p = corpus.path(Z + "flac_single_genre.flac")
    subprocess.run(["metaflac", "--set-tag=ARTIST2=ZZZZ", str(p)], check=True)
    data = p.read_bytes()
    p.write_bytes(data.replace(b"ARTIST2=ZZZZ", b"ARTIST2=\xffZZZ"))
    r = read_file(p)
    assert r.ok and r.skip_reason == "non-utf8-text"


# --------------------------------------------------------------------------- 4: MP4 genre atoms mutagen cannot parse

def _items(path):
    data = path.read_bytes()

    def find(start, end, chain):
        for t, s, ps, e in mp4_atoms(data, start, end):
            if t == chain[0]:
                return (ps, e) if len(chain) == 1 else find(ps + (4 if t == b"meta" else 0), e, chain[1:])
        raise AssertionError
    ps, e = find(0, len(data), [b"moov", b"udta", b"meta", b"ilst"])
    return [data[s:end] for _, s, _, end in mp4_atoms(data, ps, e)]


@pytest.mark.parametrize("value", [0, 999])
def test_out_of_range_gnre_refused(corpus, value):
    p = corpus.path(Z + "m4a_gnre.m4a")
    mp4_set_ilst(p, [i for i in _items(p) if i[4:8] != b"gnre"]
                 + [mp4_box(b"gnre", mp4_data(0, struct.pack(">H", value)))])
    r = read_file(p)
    assert r.ok and r.skip_reason == "mp4-genre-unparsed"
    assert "mp4-gnre" not in r.warnings  # the "will be replaced by name" text would be false


def test_gen_with_non_text_flags_refused(corpus):
    p = corpus.path(Z + "m4a_aac_moov_after_mdat.m4a")
    mp4_set_ilst(p, [i for i in _items(p) if i[4:8] != b"\xa9gen"]
                 + [mp4_box(b"\xa9gen", mp4_data(21, struct.pack(">I", 7)))])
    r = read_file(p)
    assert r.ok and r.skip_reason == "mp4-genre-unparsed" and r.genre.style == "empty"


def test_valid_gnre_still_writable(corpus):
    r = read_file(corpus.path(Z + "m4a_gnre.m4a"))
    assert r.writable and "mp4-gnre" in r.warnings


# --------------------------------------------------------------------------- 7: raw MP4 structure in snapshot

def test_mp4_udta_atom_outside_ilst_changes_snapshot(corpus):
    p = corpus.path(Z + "m4a_aac_moov_after_mdat.m4a")
    before = read_file(p).tags.snapshot
    data = p.read_bytes()
    top = list(mp4_atoms(data, 0, len(data)))
    _, ms, mps, me = next(a for a in top if a[0] == b"moov")
    out = b""
    for t, s, ps, e in mp4_atoms(data, mps, me):
        if t == b"udta":
            out += mp4_box(b"udta", data[ps:e] + mp4_box(b"\xa9xyz", b"\x00\x0c\x15\xc7+52.3+004.9/"))
        else:
            out += data[s:e]
    p.write_bytes(data[:ms] + mp4_box(b"moov", out) + data[me:])
    after = read_file(p).tags.snapshot
    assert after["tags"]["boxes"] != before["tags"]["boxes"]
    assert after["tags"]["items"] == before["tags"]["items"]


def test_mp4_mdat_byte_changes_snapshot(corpus):
    p = corpus.path(Z + "m4a_aac_moov_after_mdat.m4a")
    before = read_file(p).tags.snapshot["tags"]["mdat"]
    data = bytearray(p.read_bytes())
    data[200] ^= 0xFF  # inside mdat (moov is at the end)
    p.write_bytes(bytes(data))
    assert read_file(p).tags.snapshot["tags"]["mdat"] != before


@pytest.mark.parametrize("rel", [Z + "m4a_aac_faststart.m4a", Z + "m4a_aac_moov_after_mdat.m4a"])
def test_mp4_genre_write_keeps_snapshot(corpus, rel):
    """Chunk offsets move when a faststart tag grows; the relative view must not."""
    p = corpus.path(rel)
    before = read_file(p).tags.snapshot
    m = MP4(p)
    m.tags["\xa9gen"] = ["A much longer genre value " * 20, "Second"]
    m.save()
    after = read_file(p)
    assert after.genre.values[1] == "Second"
    assert after.tags.snapshot == before


# --------------------------------------------------------------------------- 6: readproc never raises

def test_readproc_fork_failure_is_contained(corpus, monkeypatch):
    def boom(*a, **kw):
        raise OSError(11, "Resource temporarily unavailable")
    monkeypatch.setattr(readproc.subprocess, "Popen", boom)
    got = read_files([corpus.path(Z + "flac_single_genre.flac"), corpus.path(Z + "flac_no_genre.flac")])
    assert [r.reason for r in got] == ["reader-crashed", "reader-crashed"]
    assert "cannot start reader" in got[0].error


def test_readproc_malformed_result_is_contained(corpus):
    got = read_files([corpus.path(Z + "flac_single_genre.flac")], reader="tests.metadata.readproc_hooks:malformed",
                     extra_sys_path=[BACKEND])
    assert len(got) == 1 and not got[0].ok and got[0].reason == "reader-crashed"
