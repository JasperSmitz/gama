"""Files whose text mutagen would rewrite lossily are refused (lead decision after M2).

Each case is crafted inline from a corpus copy in tmp_path: a same-length
ASCII placeholder is written with a normal tool, then swapped for invalid
bytes (Ogg pages get fresh CRCs).
"""
import struct
import subprocess

import pytest

from fixtures.build import (id3_text_body, id3_txxx_body, id3v2_tag, mp4_atoms, mp4_box, mp4_set_ilst, ogg_page_bytes,
                            ogg_pages, ogg_set_comments)
from gama.metadata import textcheck
from gama.metadata.reader import read_file
from verify.verify import id3v2_parse, ogg_parse

pytestmark = pytest.mark.slow

Z = "Format Zoo/"


def swap(path, old: bytes, new: bytes):
    data = path.read_bytes()
    assert data.count(old) == 1 and len(old) == len(new)
    path.write_bytes(data.replace(old, new))


def recrc_ogg(path):
    pages = ogg_pages(path.read_bytes())
    path.write_bytes(b"".join(ogg_page_bytes(p["flags"], p["granule"], p["serial"], p["seq"], p["lacing"], p["body"])
                              for p in pages))


def flac_with(corpus, comment: str):
    p = corpus.path(Z + "flac_single_genre.flac")
    subprocess.run(["metaflac", f"--set-tag={comment}", str(p)], check=True)
    return p


def test_flac_invalid_utf8_value(corpus):
    p = flac_with(corpus, "CUSTOM=ZZZZ")
    swap(p, b"CUSTOM=ZZZZ", b"CUSTOM=\xff\xfeZZ")
    r = read_file(p)
    assert r.ok and r.skip_reason == "non-utf8-text"
    assert r.genre.values == ("Rock",)  # still read and shown


def test_flac_invalid_utf8_genre(corpus):
    p = flac_with(corpus, "GENRE=ZZZZ")
    swap(p, b"GENRE=ZZZZ", b"GENRE=Z\xc3ZZ")
    assert read_file(p).skip_reason == "non-utf8-text"


def test_flac_invalid_vendor(corpus):
    p = corpus.path(Z + "flac_single_genre.flac")
    body = textcheck.flac_comment_block(str(p))
    vendor = body[4:4 + int.from_bytes(body[:4], "little")]
    swap(p, body[:4] + vendor, body[:4] + b"\xff" + vendor[1:])
    assert read_file(p).skip_reason == "non-utf8-text"


@pytest.mark.parametrize("bad", [b"CUST~M=ZZZZ", b"CUSTOMxZZZZ"])
def test_flac_comment_mutagen_would_drop(corpus, bad):
    p = flac_with(corpus, "CUSTOM=ZZZZ")
    swap(p, b"CUSTOM=ZZZZ", bad)
    assert read_file(p).skip_reason == "lossy-comments"


@pytest.mark.parametrize("rel", [Z + "ogg_vorbis_multi_genre.ogg", Z + "opus_multi_genre.opus"])
def test_ogg_invalid_utf8(corpus, rel):
    p = corpus.path(rel)
    ogg_set_comments(p, ogg_parse(p.read_bytes())["comments"] + ["CUSTOM=ZZZZ"])
    swap(p, b"CUSTOM=ZZZZ", b"CUSTOM=\xffZZZ")
    recrc_ogg(p)
    r = read_file(p)
    assert r.ok and r.skip_reason == "non-utf8-text"


def rewrite_id3(path, fn):
    data = path.read_bytes()
    t = id3v2_parse(data)
    path.write_bytes(id3v2_tag(fn(list(t["frames"])), 4) + data[t["end"]:])


@pytest.mark.parametrize("body", [
    b"\x03desc\x00bad \xff\xfe utf8",            # UTF-8 frame with invalid bytes
    b"\x01" + "desc".encode("utf-16") + b"\x00\x00\xff\xfe\x00",  # odd-length UTF-16 value
])
def test_id3_invalid_text(corpus, body):
    p = corpus.path(Z + "mp3_v24_full.mp3")
    rewrite_id3(p, lambda fr: fr + [("TXXX", body)])
    r = read_file(p)
    assert r.ok and r.skip_reason == "non-utf8-text"


def test_id3_merged_duplicate_frames(corpus):
    p = corpus.path(Z + "mp3_v24_full.mp3")
    rewrite_id3(p, lambda fr: fr + [("TXXX", id3_txxx_body("CUSTOM_UNRELATED", "second copy", 3))])
    assert read_file(p).skip_reason == "id3-frames-dropped"


def test_id3_valid_unicode_is_fine(corpus):
    p = corpus.path(Z + "mp3_v24_full.mp3")
    rewrite_id3(p, lambda fr: fr + [("TIT3", id3_text_body(["Ünïcødé ✓"], 1))])
    assert read_file(p).writable


def _ilst_items(path):
    data = path.read_bytes()

    def find(start, end, chain):
        for t, s, ps, e in mp4_atoms(data, start, end):
            if t == chain[0]:
                return (ps, e) if len(chain) == 1 else find(ps + (4 if t == b"meta" else 0), e, chain[1:])
        raise AssertionError
    ps, e = find(0, len(data), [b"moov", b"udta", b"meta", b"ilst"])
    return [data[s:end] for _, s, _, end in mp4_atoms(data, ps, e)]


def _bad_text_item(key: bytes) -> bytes:
    return mp4_box(key, mp4_box(b"data", struct.pack(">II", 1, 0) + b"bad \xff\xfe"))


def test_mp4_undecodable_genre_refused(corpus):
    p = corpus.path(Z + "m4a_aac_moov_after_mdat.m4a")
    mp4_set_ilst(p, [i for i in _ilst_items(p) if i[4:8] != b"\xa9gen"] + [_bad_text_item(b"\xa9gen")])
    assert read_file(p).skip_reason == "non-utf8-text"


def test_mp4_other_undecodable_atom_is_preserved_by_mutagen(corpus):
    # mutagen keeps unparsable atoms and writes them back byte-for-byte, so this stays writable.
    p = corpus.path(Z + "m4a_aac_moov_after_mdat.m4a")
    mp4_set_ilst(p, [i for i in _ilst_items(p) if i[4:8] != b"\xa9nam"] + [_bad_text_item(b"\xa9nam")])
    assert read_file(p).writable


def test_corpus_has_no_text_problems(corpus_master):
    for rel, e in corpus_master.manifest.items():
        r = read_file(corpus_master.master / rel)
        if not r.ok:
            continue
        if "non-utf8-comment" in e["props"]:
            assert r.skip_reason == "non-utf8-text", rel
        elif r.tags.native.get("text_problem") not in ("mp4-truncated",):
            assert r.tags.native.get("text_problem") is None, rel
