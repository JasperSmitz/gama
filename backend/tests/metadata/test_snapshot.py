"""Snapshot: stable across reads, blind to genre edits, sensitive to everything else.

Edits use raw writers (fixture builder helpers, metaflac), not mutagen.
"""
import subprocess

import pytest

from fixtures.build import (id3_apic_body, id3_text_body, id3v2_tag, mp4_atoms, mp4_set_ilst, mp4_text_item,
                            ogg_set_comments)
from gama.metadata.reader import read_file
from verify.verify import id3v2_parse, ogg_parse

pytestmark = pytest.mark.slow

Z = "Format Zoo/"
MP3 = Z + "mp3_v24_full.mp3"
FLAC = Z + "flac_two_pictures_no_padding.flac"
OGG = Z + "ogg_vorbis_multi_genre.ogg"
M4A = Z + "m4a_aac_moov_after_mdat.m4a"


def snap(p):
    r = read_file(p)
    assert r.ok, r.error
    return r.tags.snapshot


def rewrite_id3(path, fn):
    data = path.read_bytes()
    p = id3v2_parse(data)
    path.write_bytes(id3v2_tag(fn(list(p["frames"])), 4) + data[p["end"]:])


def mp4_items(path):
    data = path.read_bytes()

    def find(start, end, chain):
        for t, s, ps, e in mp4_atoms(data, start, end):
            if t == chain[0]:
                return (ps, e) if len(chain) == 1 else find(ps + (4 if t == b"meta" else 0), e, chain[1:])
        raise AssertionError("ilst not found")
    ps, e = find(0, len(data), [b"moov", b"udta", b"meta", b"ilst"])
    return [data[s:end] for _, s, _, end in mp4_atoms(data, ps, e)]


def set_ogg(path, fn):
    ogg_set_comments(path, fn(ogg_parse(path.read_bytes())["comments"]))


def test_snapshot_stable(corpus_master):
    for rel in corpus_master.manifest:
        a, b = read_file(corpus_master.master / rel), read_file(corpus_master.master / rel)
        assert a.to_dict() == b.to_dict(), rel


# --------------------------------------------------------------------------- genre edits leave it unchanged

def test_genre_edits_do_not_change_snapshot(corpus):
    before = {rel: snap(corpus.path(rel)) for rel in (MP3, FLAC, OGG, M4A)}
    rewrite_id3(corpus.path(MP3), lambda fr: [(f, b) for f, b in fr if f != "TCON"]
                + [("TCON", id3_text_body(["A", "B"], 3))])
    subprocess.run(["metaflac", "--remove-tag=GENRE", "--set-tag=GENRE=A", "--set-tag=GENRE=B",
                    str(corpus.path(FLAC))], check=True)
    set_ogg(corpus.path(OGG), lambda c: [x for x in c if not x.upper().startswith("GENRE=")] + ["GENRE=A"])
    mp4_set_ilst(corpus.path(M4A), [i for i in mp4_items(corpus.path(M4A)) if i[4:8] != b"\xa9gen"]
                 + [mp4_text_item(b"\xa9gen", ["A", "B"])])
    for rel, s in before.items():
        r = read_file(corpus.path(rel))
        assert r.genre.values != () and r.genre.values[0] == "A", rel
        assert r.tags.snapshot == s, rel


# --------------------------------------------------------------------------- defects change it

def test_dropped_ufid_changes_snapshot(corpus):
    s = snap(corpus.path(MP3))
    rewrite_id3(corpus.path(MP3), lambda fr: [(f, b) for f, b in fr if f != "UFID"])
    assert snap(corpus.path(MP3))["tags"] != s["tags"]


def test_apic_type_change_changes_snapshot(corpus):
    s = snap(corpus.path(MP3))

    def fn(frames):
        out = []
        for f, b in frames:
            if f == "APIC" and b"image/png" in b[:12]:
                mime_end = b.index(b"\0", 1)
                desc_end = b.index(b"\0", mime_end + 2)
                b = id3_apic_body(b[desc_end + 1:], "image/png", 5, "back", 3)
            out.append((f, b))
        return out
    rewrite_id3(corpus.path(MP3), fn)
    s2 = snap(corpus.path(MP3))
    assert s2["pictures"] != s["pictures"] and s2["tags"] != s["tags"]


def test_id3v1_and_ape_raw_bytes_in_snapshot(corpus):
    p = corpus.path(MP3)
    s = snap(p)
    data = bytearray(p.read_bytes())
    data[-125:-120] = b"XXXXX"
    p.write_bytes(bytes(data))
    s1 = snap(p)
    assert s1["raw"]["id3v1"] != s["raw"]["id3v1"] and s1["raw"]["apev2"] == s["raw"]["apev2"]
    p.write_bytes(p.read_bytes().replace(b"120,210", b"120,211"))
    assert snap(p)["raw"]["apev2"] != s["raw"]["apev2"]


def test_flac_picture_removal_changes_snapshot(corpus):
    s = snap(corpus.path(FLAC))
    subprocess.run(["metaflac", "--remove", "--block-type=PICTURE", str(corpus.path(FLAC))], check=True)
    s2 = snap(corpus.path(FLAC))
    assert s2["pictures"] != s["pictures"] and s2["tags"]["blocks"] != s["tags"]["blocks"]


def test_vorbis_comment_case_change_changes_snapshot(corpus):
    s = snap(corpus.path(OGG))
    set_ogg(corpus.path(OGG), lambda c: [x.replace("CUSTOM_UNRELATED=", "custom_unrelated=") for x in c])
    assert snap(corpus.path(OGG))["tags"]["comments"] != s["tags"]["comments"]


def test_vorbis_comment_order_change_changes_snapshot(corpus):
    s = snap(corpus.path(OGG))
    set_ogg(corpus.path(OGG), lambda c: list(reversed(c)))
    assert snap(corpus.path(OGG))["tags"]["comments"] != s["tags"]["comments"]


def test_mp4_freeform_drop_changes_snapshot(corpus):
    s = snap(corpus.path(M4A))
    mp4_set_ilst(corpus.path(M4A), [i for i in mp4_items(corpus.path(M4A)) if i[4:8] != b"----"])
    assert snap(corpus.path(M4A))["tags"]["items"] != s["tags"]["items"]


def test_mp4_cover_change_changes_snapshot(corpus):
    s = snap(corpus.path(M4A))
    mp4_set_ilst(corpus.path(M4A), [i for i in mp4_items(corpus.path(M4A)) if i[4:8] != b"covr"])
    s2 = snap(corpus.path(M4A))
    assert s2["pictures"] == [] and s2["tags"] != s["tags"]
