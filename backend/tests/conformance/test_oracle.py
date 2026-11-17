"""Oracle self-tests.

The oracle must accept an unmodified copy and legitimate genre edits, and must
reject every planted defect. Edits here are made with raw writers (builder
helpers, metaflac), never mutagen, so the oracle is tested independently of
the library Gama will use.
"""
import errno
import os
import shutil
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


# --------------------------------------------------------------------------- review of M1: planted defects
# Each test plants one defect from .review-m1.md and proves the oracle now
# reports it. Where a check could over-reject, a positive control comes first.

FLAC_LATIN1 = "Format Zoo/flac_latin1_comment.flac"
M4A_FAST = "Format Zoo/m4a_aac_faststart.m4a"
M4A_FRAG = "Format Zoo/m4a_fragmented.m4a"
OPUS = "Format Zoo/opus_multi_genre.opus"
MP3_V23 = "Format Zoo/mp3_v23_slash_genre.mp3"
FFFD = "�".encode()


def rejects(corpus, rel, expected, needle, **kw):
    probs = compare(corpus.master / rel, corpus.path(rel), "genres", expected, **kw)
    assert any(needle in p for p in probs), probs
    return probs


def raw_comments(path: Path) -> list[bytes]:
    from verify.verify import flac_parse
    return [c.encode("utf-8", "surrogateescape") for c in flac_parse(path.read_bytes())["comments"]]


def with_genre(comments: list[bytes], genre: bytes) -> list[bytes]:
    return [c for c in comments if not c.upper().startswith(b"GENRE=")] + [b"GENRE=" + genre]


# ---- 1: lossy decoding (mutagen writes U+FFFD back for invalid UTF-8)

def test_review1_accepts_genre_edit_keeping_latin1_bytes(corpus):
    from fixtures.build import flac_set_raw_comments
    flac_set_raw_comments(corpus.path(FLAC_LATIN1), with_genre(raw_comments(corpus.master / FLAC_LATIN1), b"Blues"))
    assert compare(corpus.master / FLAC_LATIN1, corpus.path(FLAC_LATIN1), "genres", ["Blues"]) == []


def test_review1_rejects_fffd_rewrite_of_latin1_flac_comment(corpus):
    from fixtures.build import flac_set_raw_comments
    master = raw_comments(corpus.master / FLAC_LATIN1)
    assert b"COMMENT=caf\xe9 au lait" in master
    damaged = [c.replace(b"\xe9", FFFD) for c in with_genre(master, b"Blues")]
    flac_set_raw_comments(corpus.path(FLAC_LATIN1), damaged)
    assert_rejects(corpus, FLAC_LATIN1, ["Blues"], "non-genre Vorbis comments")


def _raw_vorbis_packet(vendor: bytes, comments: list[bytes]) -> bytes:
    body = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(comments))
    body += b"".join(struct.pack("<I", len(c)) + c for c in comments)
    return b"\x03vorbis" + body + b"\x01"


def test_review1_rejects_fffd_rewrite_of_latin1_ogg_comment(corpus):
    from fixtures.build import ogg_set_comment_packet
    comments = [c.encode() for c in ogg_parse((corpus.master / OGG_MULTI).read_bytes())["comments"]]
    ogg_set_comment_packet(corpus.master / OGG_MULTI, _raw_vorbis_packet(b"v", comments + [b"COMMENT=caf\xe9"]))
    ogg_set_comment_packet(corpus.path(OGG_MULTI), _raw_vorbis_packet(b"v", comments + [b"COMMENT=caf" + FFFD]))
    rejects(corpus, OGG_MULTI, None, "non-genre Ogg comments", check_audio=False)


def test_review1_rejects_fffd_rewrite_of_mp4_freeform_name(corpus):
    from fixtures.build import mp4_box, mp4_data

    def ff(name: bytes) -> bytes:
        return mp4_box(b"----", mp4_box(b"mean", b"\0\0\0\0com.apple.iTunes") + mp4_box(b"name", b"\0\0\0\0" + name)
                       + mp4_data(1, b"v"))
    for root, name in ((corpus.master, b"caf\xe9"), (corpus.root, b"caf" + FFFD)):
        mp4_set_ilst(root / M4A_FREEFORM, mp4_items(root / M4A_FREEFORM) + [ff(name)])
    rejects(corpus, M4A_FREEFORM, None, "MP4 ilst items")


@pytest.mark.parametrize("fid,good,bad", [
    ("TPE1", b"\x03caf\xe9", b"\x03caf" + FFFD),                                   # invalid UTF-8
    ("TPE1", b"\x01\xff\xfe\x00\xd8", b"\x01\xff\xfe" + "�".encode("utf-16-le")),  # lone surrogate
    ("TPE1", b"\x02\x00a\x00", b"\x02\x00a"),                                       # odd UTF-16 length
    ("COMM", b"\x03eng\0caf\xe9", b"\x03eng\0caf" + FFFD),
    ("TXXX", b"\x03d\xe9sc\0v", b"\x03d" + FFFD + b"sc\0v"),
])
def test_review1_id3_views_are_lossless(fid, good, bad):
    assert id3_frame_view(fid, good) != id3_frame_view(fid, bad)


# ---- 2: preserved attributes

def test_review2_rejects_lost_xattr(corpus):
    rel = "Format Zoo/flac_single_genre.flac"
    try:
        os.setxattr(corpus.master / rel, "user.note", b"keep")
    except OSError as e:
        if e.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            pytest.skip("tmp filesystem has no user xattrs")
        raise
    assert any("xattrs changed" in p for p in compare_tree(corpus.master, corpus.root)[rel])
    os.setxattr(corpus.path(rel), "user.note", b"keep")
    assert compare_tree(corpus.master, corpus.root) == {}
    flac_set_genres(corpus.path(rel), GOOD)
    os.setxattr(corpus.path(rel), "user.note", b"changed")
    assert_rejects(corpus, rel, GOOD, "xattrs changed")


def test_review2_rejects_changed_group(corpus):
    rel = "Format Zoo/flac_single_genre.flac"
    cur = corpus.path(rel).stat().st_gid
    other = next((g for g in os.getgroups() if g != cur), None)
    if other is None:
        pytest.skip("the test user has only one group")
    flac_set_genres(corpus.path(rel), GOOD)
    os.chown(corpus.path(rel), -1, other)
    assert_rejects(corpus, rel, GOOD, "group changed")


def test_review2_rejects_symlink_replaced_by_edited_file(corpus):
    rel = "Format Zoo/link.flac"
    (corpus.master / rel).symlink_to("flac_single_genre.flac")
    (corpus.root / rel).symlink_to("flac_single_genre.flac")
    assert compare_tree(corpus.master, corpus.root) == {}
    # Genre edits must refuse symlinks, even when the link itself is intact.
    assert any("symlink" in p for p in compare_tree(corpus.master, corpus.root, {rel: GOOD})[rel])
    # What os.replace onto the link path produces: a regular file with new genres.
    (corpus.root / rel).unlink()
    shutil.copy2(corpus.path("Format Zoo/flac_single_genre.flac"), corpus.root / rel)
    flac_set_genres(corpus.root / rel, GOOD)
    assert any("file type changed" in p for p in compare_tree(corpus.master, corpus.root, {rel: GOOD})[rel])


def test_review2_rejects_changed_symlink_target(corpus):
    rel = "Format Zoo/link.flac"
    (corpus.master / rel).symlink_to("flac_single_genre.flac")
    (corpus.root / rel).symlink_to("flac_multi_genre.flac")
    assert any("symlink target changed" in p for p in compare_tree(corpus.master, corpus.root)[rel])


def test_review2_dangling_master_symlink_does_not_crash(corpus):
    rel = "Format Zoo/dangling.flac"
    (corpus.master / rel).symlink_to("does-not-exist.flac")
    shutil.copy2(corpus.path("Format Zoo/flac_single_genre.flac"), corpus.root / rel)
    assert compare(corpus.master / rel, corpus.root / rel, "identical") == ["file type changed: symlink -> regular file"]


def test_review2_rejects_split_hard_link(corpus):
    a, b = "Format Zoo/flac_single_genre.flac", "Format Zoo/hardlink.flac"
    os.link(corpus.master / a, corpus.master / b)
    os.link(corpus.path(a), corpus.path(b))
    assert compare_tree(corpus.master, corpus.root) == {}
    # Replace a by an edited copy, as a rename-based writer would: the link is split.
    tmp = corpus.root / "Format Zoo/.tmp.flac"
    shutil.copy2(corpus.path(a), tmp)
    flac_set_genres(tmp, GOOD)
    os.replace(tmp, corpus.path(a))
    tree = compare_tree(corpus.master, corpus.root, {a: GOOD})
    assert any("hard link" in p for p in tree[a]) and any("hard link" in p for p in tree[b])


def test_review2_rejects_mtime_change_of_untouched_file(corpus):
    rel = "Format Zoo/flac_single_genre.flac"
    st = corpus.path(rel).stat()
    os.utime(corpus.path(rel), ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert any("mtime changed" in p for p in compare_tree(corpus.master, corpus.root)[rel])
    assert compare_tree(corpus.master, corpus.root, check_mtime=False) == {}


def test_review2_rejects_leftover_directory_and_dir_mode(corpus):
    (corpus.root / "Format Zoo/.gama-tmp-job1").mkdir()
    (corpus.root / "Broken").chmod(0o700)
    tree = compare_tree(corpus.master, corpus.root)
    assert any("extra directory" in p for p in tree["Format Zoo/.gama-tmp-job1"])
    assert any("directory mode changed" in p for p in tree["Broken"])


# ---- 3 + 4: MP4 top-level boxes and absolute offsets

def _patch(path: Path, fn) -> None:
    data = bytearray(path.read_bytes())
    fn(data)
    path.write_bytes(bytes(data))


def test_review3_rejects_changed_ftyp(corpus):
    p = corpus.path(M4A_FAST)
    _patch(p, lambda d: d.__setitem__(slice(8, 12), b"qt  "))
    assert_rejects(corpus, M4A_FAST, None, "MP4 boxes")


def _box_payload(data: bytes, typ: bytes) -> int:
    i = data.index(typ)
    assert data.count(typ) == 1
    return i + 4


def test_review3_rejects_changed_tfra_moof_offset(corpus):
    p = corpus.path(M4A_FRAG)

    def fn(d):
        q = _box_payload(bytes(d), b"tfra")
        ver = d[q]
        w = 8 if ver == 1 else 4
        d[q + 16 + w + w - 1] ^= 0x10  # last byte of the first moof_offset
    _patch(p, fn)
    assert_rejects(corpus, M4A_FRAG, None, "absolute offsets in mfra/tfra")


def test_review3_rejects_changed_tfra_track_id(corpus):
    p = corpus.path(M4A_FRAG)
    _patch(p, lambda d: d.__setitem__(_box_payload(bytes(d), b"tfra") + 7, 9))
    rejects(corpus, M4A_FRAG, None, "MP4 boxes", check_audio=False)


def test_review3_rejects_changed_tfhd_base_offset(corpus):
    p = corpus.path(M4A_FRAG)
    _patch(p, lambda d: d.__setitem__(_box_payload(bytes(d), b"tfhd") + 15, d[_box_payload(bytes(d), b"tfhd") + 15] ^ 1))
    rejects(corpus, M4A_FRAG, None, "absolute offsets in moof/traf/tfhd", check_audio=False)


def test_review3_rejects_changed_trun(corpus):
    p = corpus.path(M4A_FRAG)
    _patch(p, lambda d: d.__setitem__(_box_payload(bytes(d), b"trun") + 11, d[_box_payload(bytes(d), b"trun") + 11] ^ 1))
    rejects(corpus, M4A_FRAG, None, "MP4 boxes", check_audio=False)


def test_review3_rejects_changed_meta_hdlr(corpus):
    p = corpus.path(M4A_FREEFORM)
    data = p.read_bytes()
    assert data.count(b"mdirappl") == 1
    p.write_bytes(data.replace(b"mdirappl", b"mdirxxxx"))
    rejects(corpus, M4A_FREEFORM, None, "hdlr", check_audio=False)


def test_review3_rejects_changed_data_locale(corpus):
    p = corpus.path(M4A_FREEFORM)

    def fn(d):
        i = bytes(d).index(b"\xa9nam") + 4  # the item's first child: the data box
        assert d[i + 4:i + 8] == b"data"
        d[i + 15] = 1  # last byte of the locale field
    _patch(p, fn)
    rejects(corpus, M4A_FREEFORM, None, "MP4 ilst items", check_audio=False)


def _insert_free_before_mdat(path: Path, n: int, fix_stco: bool) -> None:
    data = bytearray(path.read_bytes())
    mdat = next(s for t, s, _, _ in mp4_atoms(bytes(data), 0, len(data)) if t == b"mdat")
    if fix_stco:
        q = _box_payload(bytes(data), b"stco")
        for k in range(int.from_bytes(data[q + 4:q + 8], "big")):
            off = q + 8 + 4 * k
            data[off:off + 4] = (int.from_bytes(data[off:off + 4], "big") + n).to_bytes(4, "big")
    path.write_bytes(bytes(data[:mdat]) + struct.pack(">I", n) + b"free" + bytes(n - 8) + bytes(data[mdat:]))


def test_review4_accepts_mdat_shift_with_fixed_stco(corpus):
    _insert_free_before_mdat(corpus.path(M4A_FAST), 64, fix_stco=True)
    assert compare(corpus.master / M4A_FAST, corpus.path(M4A_FAST), "genres", None) == []


def test_review4_rejects_mdat_shift_without_stco_fix(corpus):
    _insert_free_before_mdat(corpus.path(M4A_FAST), 64, fix_stco=False)
    rejects(corpus, M4A_FAST, None, "absolute offsets in moov/trak/mdia/minf/stbl/stco", check_audio=False)


def test_review4_rejects_wrong_stco_without_ffmpeg(corpus):
    def fn(d):
        q = _box_payload(bytes(d), b"stco") + 8
        d[q:q + 4] = (int.from_bytes(d[q:q + 4], "big") + 64).to_bytes(4, "big")
    _patch(corpus.path(M4A_FAST), fn)
    rejects(corpus, M4A_FAST, None, "absolute offsets", check_audio=False)


# ---- 5: ID3 non-text frames hashed raw

def _replace_frame(fid, body):
    def fn(frames):
        return [(f, body if f == fid else b) for f, b in frames]
    return fn


def _add_frame(fid, body):
    return lambda frames: frames + [(fid, body)]


def test_review5_rejects_truncated_popm(corpus):
    rewrite_id3(corpus.master / MP3_FULL, _replace_frame("POPM", b"x@y\0" + b"\0" + b"\0\0\0\0"))
    rewrite_id3(corpus.path(MP3_FULL), _replace_frame("POPM", b"x@y\0"))
    rejects(corpus, MP3_FULL, None, "POPM", check_audio=False)


def test_review5_rejects_truncated_priv(corpus):
    rewrite_id3(corpus.master / MP3_FULL, _add_frame("PRIV", b"AverageLevel\0\x00\x01\x00\x00"))
    rewrite_id3(corpus.path(MP3_FULL), _add_frame("PRIV", b"AverageLevel\0\x00\x01"))
    rejects(corpus, MP3_FULL, None, "PRIV", check_audio=False)


def test_review5_url_frame_terminator_is_tolerated(corpus):
    rewrite_id3(corpus.master / MP3_FULL, _add_frame("WOAR", b"http://example.invalid/\0"))
    rewrite_id3(corpus.path(MP3_FULL), _add_frame("WOAR", b"http://example.invalid/"))
    assert compare(corpus.master / MP3_FULL, corpus.path(MP3_FULL), "genres", None, check_audio=False) == []


# ---- 6: ID3v2.3 -> 2.4 upgrade keeps TORY/TRDA

def _rewrite_id3_version(path: Path, fn, version: int) -> None:
    data = path.read_bytes()
    p = id3v2_parse(data)
    path.write_bytes(id3v2_tag(fn(list(p["frames"])), version) + data[p["end"]:])


def _upgrade(extra):
    def fn(frames):
        out = [(f, b) for f, b in frames if f not in ("TYER", "TDAT", "TORY", "TRDA")]
        return out + [("TDRC", id3_text_body(["1972-03-15"], 1))] + extra
    return fn


@pytest.fixture
def v23_with_tory(corpus):
    _rewrite_id3_version(corpus.master / MP3_V23, _add_frame("TORY", id3_text_body(["1969"], 1)), 3)
    _rewrite_id3_version(corpus.path(MP3_V23), _add_frame("TORY", id3_text_body(["1969"], 1)), 3)
    return corpus


def test_review6_accepts_upgrade_with_tdor(v23_with_tory):
    c = v23_with_tory
    _rewrite_id3_version(c.path(MP3_V23), _upgrade([("TDOR", id3_text_body(["1969"], 1))]), 4)
    assert compare(c.master / MP3_V23, c.path(MP3_V23), "genres", None) == []


def test_review6_rejects_upgrade_dropping_tory(v23_with_tory):
    c = v23_with_tory
    _rewrite_id3_version(c.path(MP3_V23), _upgrade([]), 4)
    rejects(c, MP3_V23, None, "original release date", check_audio=False)


def test_review6_rejects_upgrade_dropping_trda(corpus):
    trda = ("TRDA", id3_text_body(["March 15th"], 1))
    _rewrite_id3_version(corpus.master / MP3_V23, _add_frame(*trda), 3)
    _rewrite_id3_version(corpus.path(MP3_V23), _upgrade([]), 4)
    rejects(corpus, MP3_V23, None, "TRDA", check_audio=False)


# ---- 8: Ogg page structure and the Vorbis framing bit

def rewrite_ogg_pages(path: Path, fn) -> None:
    from fixtures.build import ogg_page_bytes, ogg_pages
    pages = fn(ogg_pages(path.read_bytes()))
    path.write_bytes(b"".join(ogg_page_bytes(p["flags"], p["granule"], p["serial"], p["seq"], p["lacing"], p["body"])
                              for p in pages))


def _set(i, **kw):
    def fn(pages):
        pages[i].update(kw)
        return pages
    return fn


def test_review8_rejects_missing_vorbis_framing_bit(corpus):
    from fixtures.build import ogg_set_comment_packet, vorbis_comment_packet
    comments = ogg_parse((corpus.master / OGG_MULTI).read_bytes())["comments"]
    ogg_set_comment_packet(corpus.path(OGG_MULTI), vorbis_comment_packet("vorbis", "x", comments)[:-1])
    ogg_set_comment_packet(corpus.master / OGG_MULTI, vorbis_comment_packet("vorbis", "x", comments))
    rejects(corpus, OGG_MULTI, None, "framing bit", check_audio=False)


def test_review8_rejects_cleared_eos(corpus):
    rewrite_ogg_pages(corpus.path(OGG_MULTI), lambda pages: _set(-1, flags=pages[-1]["flags"] & ~0x04)(pages))
    rejects(corpus, OGG_MULTI, None, "EOS", check_audio=False)


def test_review8_rejects_changed_middle_granule(corpus):
    pages = ogg_parse(corpus.path(OGG_MULTI).read_bytes())["seqs"]
    assert len(pages) >= 4
    rewrite_ogg_pages(corpus.path(OGG_MULTI), _set(2, granule=(12345).to_bytes(8, "little")))
    rejects(corpus, OGG_MULTI, None, "granule", check_audio=False)


def test_review8_rejects_sequence_gap(corpus):
    rewrite_ogg_pages(corpus.path(OGG_MULTI), lambda pages: _set(-1, seq=pages[-1]["seq"] + 1)(pages))
    rejects(corpus, OGG_MULTI, None, "sequence numbers", check_audio=False)


def test_review8_rejects_dropped_opus_trailing_data(corpus):
    from fixtures.build import ogg_set_comment_packet, vorbis_comment_packet
    comments = ogg_parse((corpus.master / OPUS).read_bytes())["comments"]
    ogg_set_comment_packet(corpus.master / OPUS, vorbis_comment_packet("opus", "x", comments) + b"\x00binary")
    ogg_set_comment_packet(corpus.path(OPUS), vorbis_comment_packet("opus", "x", comments))
    rejects(corpus, OPUS, None, "trailing bytes", check_audio=False)


# ---- minor: ID3v1 raw bytes with a permitted genre change

def test_review_minor_id3v1_raw_bytes_checked_with_genre_change_allowed(corpus):
    p = corpus.path(MP3_FULL)
    _patch(p, lambda d: d.__setitem__(len(d) - 128 + 3 + 25, 0x41))  # a byte after the title's NUL
    probs = compare(corpus.master / MP3_FULL, p, "genres", None, allow_id3v1_genre_change=True, check_audio=False)
    assert "ID3v1 raw bytes changed" in probs
