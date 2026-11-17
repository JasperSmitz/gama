"""Oracle self-tests.

The oracle must accept an unmodified copy and legitimate genre edits, and must
reject every planted defect. Edits here are made with raw writers (builder
helpers, metaflac), never mutagen, so the oracle is tested independently of
the library Gama will use.
"""
import struct
import subprocess
from pathlib import Path

import pytest

from fixtures.build import (id3_apic_body, id3_text_body, id3v2_tag, mp4_atoms, mp4_set_ilst, mp4_text_item,
                            ogg_set_comments)
from verify.verify import (compare, compare_tree, id3_frame_view, id3_text_values, id3v2_parse, ogg_parse,
                           raw_genres)

pytestmark = pytest.mark.slow

MP3_FULL = "Format Zoo/mp3_v24_full.mp3"
OGG_MULTI = "Format Zoo/ogg_vorbis_multi_genre.ogg"
FLAC_TWO_PICS = "Format Zoo/flac_two_pictures_no_padding.flac"
M4A_FREEFORM = "Format Zoo/m4a_aac_moov_after_mdat.m4a"


# --------------------------------------------------------------------------- raw edit helpers

def rewrite_id3(path: Path, fn) -> None:
    """Rebuild the ID3v2.4 tag from fn(frames); audio, APEv2 and ID3v1 untouched."""
    data = path.read_bytes()
    p = id3v2_parse(data)
    frames = fn(list(p["frames"]))
    path.write_bytes(id3v2_tag(frames, 4) + data[p["end"]:])


def set_tcon(values):
    def fn(frames):
        out = [(f, b) for f, b in frames if f != "TCON"]
        return out + [("TCON", id3_text_body(values, 3))]
    return fn


def set_ogg_genres(path: Path, genres: list[str], single_field: bool = False) -> None:
    comments = [c for c in ogg_parse(path.read_bytes())["comments"] if not c.upper().startswith("GENRE=")]
    comments += [f"GENRE={';'.join(genres)}"] if single_field else [f"GENRE={g}" for g in genres]
    ogg_set_comments(path, comments)


def flac_set_genres(path: Path, genres: list[str]) -> None:
    subprocess.run(["metaflac", "--remove-tag=GENRE", "--remove-tag=genre", *[f"--set-tag=GENRE={g}" for g in genres],
                    str(path)], check=True)


def mp4_items(path: Path) -> list[bytes]:
    data = path.read_bytes()

    def find(start, end, chain):
        for t, s, ps, e in mp4_atoms(data, start, end):
            if t == chain[0]:
                if len(chain) == 1:
                    return ps, e
                return find(ps + (4 if t == b"meta" else 0), e, chain[1:])
        raise AssertionError("ilst not found")
    ps, e = find(0, len(data), [b"moov", b"udta", b"meta", b"ilst"])
    return [data[s:end] for _, s, _, end in mp4_atoms(data, ps, e)]


def mp4_replace_items(path: Path, keep, extra: list[bytes]) -> None:
    mp4_set_ilst(path, [it for it in mp4_items(path) if keep(it[4:8])] + extra)


# --------------------------------------------------------------------------- acceptance

def test_accepts_unmodified_copy(corpus):
    assert compare_tree(corpus.master, corpus.root) == {}


def test_accepts_unmodified_copy_in_genres_mode(corpus):
    for rel in corpus.manifest:
        assert compare(corpus.master / rel, corpus.path(rel), "genres") == [], rel


GOOD = ["Progressive Rock", "Ünïcødé Genre", "Jazz"]


def test_accepts_legitimate_edits(corpus):
    rels = {
        "flac": "Format Zoo/flac_multi_genre.flac",
        "mp3": MP3_FULL,
        "ogg": OGG_MULTI,
        "opus": "Format Zoo/opus_multi_genre.opus",
        "m4a": M4A_FREEFORM,
        "flac2": FLAC_TWO_PICS,
    }
    flac_set_genres(corpus.path(rels["flac"]), GOOD)
    flac_set_genres(corpus.path(rels["flac2"]), GOOD)
    rewrite_id3(corpus.path(rels["mp3"]), set_tcon(GOOD))
    set_ogg_genres(corpus.path(rels["ogg"]), GOOD)
    set_ogg_genres(corpus.path(rels["opus"]), GOOD)
    mp4_replace_items(corpus.path(rels["m4a"]), lambda k: k != b"\xa9gen", [mp4_text_item(b"\xa9gen", GOOD)])
    for rel in rels.values():
        assert raw_genres(corpus.path(rel)) == GOOD, rel
    assert compare_tree(corpus.master, corpus.root, {r: GOOD for r in rels.values()}) == {}


def test_accepts_removing_all_genres(corpus):
    flac_set_genres(corpus.path(FLAC_TWO_PICS), [])
    assert compare(corpus.master / FLAC_TWO_PICS, corpus.path(FLAC_TWO_PICS), "genres", []) == []


# --------------------------------------------------------------------------- planted defects

def assert_rejects(corpus, rel, expected, needle):
    probs = compare(corpus.master / rel, corpus.path(rel), "genres", expected)
    assert probs, f"oracle accepted a planted defect in {rel}"
    assert any(needle in p for p in probs), probs
    tree = compare_tree(corpus.master, corpus.root, {rel: expected})
    assert rel in tree


def test_rejects_dropped_ufid(corpus):
    rewrite_id3(corpus.path(MP3_FULL), lambda fr: [(f, b) for f, b in fr if f != "UFID"])
    assert_rejects(corpus, MP3_FULL, None, "UFID")


def test_rejects_dropped_ufid_alongside_legit_genre_change(corpus):
    def fn(frames):
        return set_tcon(GOOD)([(f, b) for f, b in frames if f != "UFID"])
    rewrite_id3(corpus.path(MP3_FULL), fn)
    assert_rejects(corpus, MP3_FULL, GOOD, "UFID")


def test_rejects_changed_apic_type(corpus):
    def fn(frames):
        out = []
        for f, b in frames:
            if f == "APIC" and b"image/jpeg" in b[:12]:
                mime_end = b.index(b"\0", 1)
                b = b[:mime_end + 1] + bytes([4]) + b[mime_end + 2:]
            out.append((f, b))
        return out
    rewrite_id3(corpus.path(MP3_FULL), fn)
    assert_rejects(corpus, MP3_FULL, None, "APIC")


def test_rejects_changed_apic_description(corpus):
    def fn(frames):
        out = []
        for f, b in frames:
            if f == "APIC" and b"image/jpeg" in b[:12]:
                b = id3_apic_body(_apic_payload(b), "image/jpeg", 3, "FRONT", 3)
            out.append((f, b))
        return out
    rewrite_id3(corpus.path(MP3_FULL), fn)
    assert_rejects(corpus, MP3_FULL, None, "APIC")


def _apic_payload(b: bytes) -> bytes:
    """Image bytes of a UTF-8/latin-1 APIC body."""
    mime_end = b.index(b"\0", 1)
    desc_end = b.index(b"\0", mime_end + 2)
    return b[desc_end + 1:]


def test_rejects_garbled_id3v1_title(corpus):
    p = corpus.path(MP3_FULL)
    data = bytearray(p.read_bytes())
    data[-125:-120] = b"XXXXX"
    p.write_bytes(bytes(data))
    assert_rejects(corpus, MP3_FULL, None, "ID3v1 title")


def test_rejects_id3v1_genre_byte_unless_allowed(corpus):
    p = corpus.path(MP3_FULL)
    data = bytearray(p.read_bytes())
    data[-1] = 17
    p.write_bytes(bytes(data))
    assert_rejects(corpus, MP3_FULL, None, "ID3v1 genre")
    assert compare(corpus.master / MP3_FULL, p, "genres", None, allow_id3v1_genre_change=True) == []


def test_rejects_edited_ape_item(corpus):
    p = corpus.path(MP3_FULL)
    data = p.read_bytes()
    assert data.count(b"120,210") == 1
    p.write_bytes(data.replace(b"120,210", b"120,211"))
    assert_rejects(corpus, MP3_FULL, None, "APEv2 item 'mp3gain_minmax'")


def test_rejects_removed_ape_tag(corpus):
    p = corpus.path(MP3_FULL)
    data = p.read_bytes()
    start = data.index(b"APETAGEX")
    end = len(data) - 128
    p.write_bytes(data[:start] + data[end:])
    assert_rejects(corpus, MP3_FULL, None, "APEv2 tag removed")


def test_rejects_multi_field_ogg_genre_joined_into_one(corpus):
    master_genres = raw_genres(corpus.master / OGG_MULTI)
    assert master_genres == ["Electronic", "Ambient"]
    set_ogg_genres(corpus.path(OGG_MULTI), master_genres, single_field=True)
    assert_rejects(corpus, OGG_MULTI, master_genres, "stored genres")


def test_rejects_mode_change(corpus):
    rel = "Format Zoo/flac_single_genre.flac"
    corpus.path(rel).chmod(0o600)
    tree = compare_tree(corpus.master, corpus.root)
    assert any("file mode changed" in p for p in tree[rel])
    flac_set_genres(corpus.path(rel), GOOD)
    assert_rejects(corpus, rel, GOOD, "file mode changed")


def test_rejects_leftover_temp_file(corpus):
    (corpus.root / "Format Zoo/.gama-tmp-job1-0-abc.flac").write_bytes(b"partial")
    tree = compare_tree(corpus.master, corpus.root)
    assert "Format Zoo/.gama-tmp-job1-0-abc.flac" in tree


def test_rejects_missing_file(corpus):
    rel = "Format Zoo/flac_single_genre.flac"
    corpus.path(rel).unlink()
    assert compare_tree(corpus.master, corpus.root)[rel] == ["file missing"]


def test_rejects_wrong_genres(corpus):
    rewrite_id3(corpus.path(MP3_FULL), set_tcon(["Post-Punk", "New Wave"]))
    assert_rejects(corpus, MP3_FULL, ["Post-Punk", "New Wave", "Gothic Rock", "Extra"], "stored genres")


def test_rejects_genre_order_change(corpus):
    rewrite_id3(corpus.path(MP3_FULL), set_tcon(["New Wave", "Post-Punk", "Gothic Rock"]))
    assert_rejects(corpus, MP3_FULL, None, "stored genres")


def test_rejects_skipped_write(corpus):
    """V2: a file expected to change but left byte-identical fails."""
    assert_rejects(corpus, OGG_MULTI, GOOD, "byte-identical")


def test_rejects_unexpected_change_of_other_file(corpus):
    rel = "Format Zoo/flac_single_genre.flac"
    flac_set_genres(corpus.path(rel), GOOD)
    assert rel in compare_tree(corpus.master, corpus.root, {})


def test_rejects_dropped_flac_picture(corpus):
    subprocess.run(["metaflac", "--remove", "--block-type=PICTURE", "--dont-use-padding",
                    str(corpus.path(FLAC_TWO_PICS))], check=True)
    assert_rejects(corpus, FLAC_TWO_PICS, None, "PICTURE")


def test_rejects_vorbis_comment_case_or_order_change(corpus):
    rel = "Format Zoo/flac_multi_genre.flac"
    p = corpus.path(rel)
    subprocess.run(["metaflac", "--remove-tag=CUSTOM_UNRELATED", "--set-tag=custom_unrelated=keep me ✓ ünïcødé",
                    str(p)], check=True)
    assert_rejects(corpus, rel, None, "non-genre Vorbis comments")


def test_rejects_dropped_mp4_freeform(corpus):
    mp4_replace_items(corpus.path(M4A_FREEFORM), lambda k: k != b"----", [])
    assert_rejects(corpus, M4A_FREEFORM, None, "MP4 ilst items")


def test_rejects_leftover_gnre(corpus):
    rel = "Format Zoo/m4a_gnre.m4a"
    mp4_replace_items(corpus.path(rel), lambda k: True, [mp4_text_item(b"\xa9gen", ["Rock"])])
    assert_rejects(corpus, rel, ["Rock"], "stored genres")


def test_rejects_corrupted_audio_byte(corpus):
    rel = "Format Zoo/flac_single_genre.flac"
    p = corpus.path(rel)
    data = bytearray(p.read_bytes())
    data[-500] ^= 0xFF
    p.write_bytes(bytes(data))
    assert_rejects(corpus, rel, None, "audio")


def test_rejects_corrupted_mp3_audio(corpus):
    p = corpus.path(MP3_FULL)
    data = bytearray(p.read_bytes())
    end = id3v2_parse(bytes(data))["end"]
    data[end + 2000] ^= 0x55
    p.write_bytes(bytes(data))
    assert_rejects(corpus, MP3_FULL, None, "MPEG audio region")


def test_rejects_bad_ogg_crc(corpus):
    p = corpus.path(OGG_MULTI)
    set_ogg_genres(p, GOOD)
    data = bytearray(p.read_bytes())
    data[-10] ^= 0x01
    p.write_bytes(bytes(data))
    assert_rejects(corpus, OGG_MULTI, GOOD, "bad CRC")


def test_rejects_unsupported_format_modified(corpus):
    rel = "Format Zoo/unsupported.wav"
    p = corpus.path(rel)
    p.write_bytes(p.read_bytes() + b"LIST\x04\0\0\0INFO")
    assert_rejects(corpus, rel, None, "unsupported format")


def test_identical_mode_rejects_any_byte_change(corpus):
    rel = "Broken/garbage.flac"
    p = corpus.path(rel)
    data = bytearray(p.read_bytes())
    data[0] ^= 1
    p.write_bytes(bytes(data))
    assert compare(corpus.master / rel, p, "identical")


# --------------------------------------------------------------------------- parser details (V8)

def _syncsafe(n):
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def test_id3_utf16_values_decoded_separately():
    body = b"\x01" + "Rock".encode("utf-16") + b"\0\0" + "Ünï".encode("utf-16") + b"\0\0"
    assert id3_text_values(body) == ["Rock", "Ünï"]


def test_id3_frame_unsync_and_data_length_indicator():
    raw_body = b"\x03Rock\0Ja\xff\x00zz"  # unsynchronised: \xff\x00 -> \xff
    dli = struct.pack(">I", len(raw_body) - 1)
    frame_body = dli + raw_body
    frame = b"TCON" + _syncsafe(len(frame_body)) + struct.pack(">H", 0x0003) + frame_body
    tag = b"ID3\x04\x00\x00" + _syncsafe(len(frame)) + frame
    p = id3v2_parse(tag)
    assert p["frames"][0][0] == "TCON"
    assert p["frames"][0][1] == b"\x03Rock\0Ja\xffzz"


def test_id3v23_tag_level_unsync():
    body = b"\x00Rock\xff\x00"
    frame = b"TCON" + struct.pack(">I", len(body) - 1) + b"\0\0" + body
    tag = b"ID3\x03\x00\x80" + _syncsafe(len(frame)) + frame
    p = id3v2_parse(tag)
    assert p["frames"][0][1] == b"\x00Rock\xff"


def test_id3_frame_view_is_encoding_independent():
    a = id3_frame_view("TPE1", id3_text_body(["Ünïcødé"], 1))
    b = id3_frame_view("TPE1", id3_text_body(["Ünïcødé"], 3))
    assert a == b
