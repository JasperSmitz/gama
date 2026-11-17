"""Regression tests for the M4 review: verify false passes and files the preview
approved but a write would damage. Adversarial files are crafted inline in tmp_path.
"""
import struct
import subprocess

import pytest

from fixtures.build import (id3_text_body, id3v2_tag, mp4_atoms, mp4_box, mp4_data, mp4_freeform, mp4_set_ilst,
                            mp4_text_item, ogg_header_packets, ogg_set_comment_packet, vorbis_comment_packet)
from gama.metadata.formats import id3 as id3_fmt
from gama.metadata.formats import mp4 as mp4_fmt
from gama.metadata.genres import apply
from gama.metadata.reader import read_file
from gama.safewrite.mutate import run_mutate
from gama.safewrite.verify import verify_temp
from verify.verify import compare

pytestmark = pytest.mark.slow

Z = "Format Zoo/"
M4A = Z + "m4a_aac_moov_after_mdat.m4a"


def _syncsafe(n):
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _ilst(path):
    data = path.read_bytes()

    def find(start, end, chain):
        for t, s, ps, e in mp4_atoms(data, start, end):
            if t == chain[0]:
                return (ps, e) if len(chain) == 1 else find(ps + (4 if t == b"meta" else 0), e, chain[1:])
        raise AssertionError
    ps, e = find(0, len(data), [b"moov", b"udta", b"meta", b"ilst"])
    return [data[s:end] for _, s, _, end in mp4_atoms(data, ps, e)]


# --------------------------------------------------------------------------- 1: MP4 items mutagen re-renders

def _locale_item():
    return mp4_box(b"\xa9cmt", mp4_box(b"data", struct.pack(">II", 1, 0x1234) + b"comment"))


MP4_NORMALISED = {
    "covr-bmp": [mp4_box(b"covr", mp4_data(27, b"BM" + b"\0" * 60))],
    "cpil-2": [mp4_box(b"cpil", mp4_data(21, b"\x02"))],
    "locale": [_locale_item()],
    "two-tmpo": [mp4_box(b"tmpo", mp4_data(21, b"\x00\x78")), mp4_box(b"tmpo", mp4_data(21, b"\x00\x79"))],
    "two-freeform": [mp4_freeform("com.apple.iTunes", "X", ["a"]), mp4_freeform("com.apple.iTunes", "X", ["b"])],
    "two-nam": [mp4_text_item(b"\xa9nam", ["First"]), mp4_text_item(b"\xa9nam", ["Second"])],
}


def _crafted_m4a(corpus, extra):
    p = corpus.path(M4A)
    keep = [i for i in _ilst(p) if not (i[4:8] == b"\xa9nam" and any(x[4:8] == b"\xa9nam" for x in extra))]
    mp4_set_ilst(p, keep + extra)
    return p


@pytest.mark.parametrize("case", sorted(MP4_NORMALISED))
def test_mp4_items_mutagen_would_rewrite_are_refused(corpus, case):
    p = _crafted_m4a(corpus, MP4_NORMALISED[case])
    r = read_file(p)
    assert r.ok and r.skip_reason == "mp4-items-normalised", (r.skip_reason, r.error)


@pytest.mark.parametrize("case", sorted(MP4_NORMALISED))
def test_verify_catches_mp4_rerender_if_written_anyway(corpus, case):
    """The snapshot's raw ilst multiset sees the re-render (second line of defence)."""
    p = _crafted_m4a(corpus, MP4_NORMALISED[case])
    backup = corpus.root / "backup.m4a"
    backup.write_bytes(p.read_bytes())
    before = read_file(p)
    mp4_fmt.apply_genres(str(p), list(before.genre.values) + ["Blues"])  # bypass the policy on purpose
    v = verify_temp(backup, p, list(before.genre.values) + ["Blues"], audio="off")
    assert not v.ok and any("ilst_items" in x for x in v.problems), v.problems
    assert compare(backup, p, "genres", list(before.genre.values) + ["Blues"], check_audio=False)


def test_clean_m4a_items_still_writable(corpus):
    assert read_file(corpus.path(M4A)).writable


# --------------------------------------------------------------------------- 2: ID3v2.4 footer

def _footer_mp3(corpus):
    audio = corpus.path(Z + "mp3_untagged.mp3").read_bytes()
    body = b"".join(f.encode() + _syncsafe(len(b)) + b"\0\0" + b
                    for f, b in [("TIT2", id3_text_body(["T"], 3)), ("TCON", id3_text_body(["Rock"], 3))])
    hdr = b"ID3\x04\x00\x10" + _syncsafe(len(body))
    p = corpus.root / "footer.mp3"
    p.write_bytes(hdr + body + b"3DI" + hdr[3:] + audio)
    return p


def test_id3_footer_refused(corpus):
    r = read_file(_footer_mp3(corpus))
    assert r.ok and r.skip_reason == "id3-footer"


def test_verify_catches_stale_footer_if_written_anyway(corpus):
    p = _footer_mp3(corpus)
    backup = corpus.root / "footer.backup.mp3"
    backup.write_bytes(p.read_bytes())
    id3_fmt.apply_genres(str(p), ["Rock", "Jazz"])
    v = verify_temp(backup, p, ["Rock", "Jazz"])
    assert not v.ok and any("audio_region" in x for x in v.problems), v.problems


def test_mp3_audio_region_in_snapshot(corpus):
    p = corpus.path(Z + "mp3_v24_full.mp3")
    before = read_file(p).tags.snapshot["tags"]["audio_region"]
    data = bytearray(p.read_bytes())
    end = len(data) - 128 - 200
    data[end] ^= 0x01  # a byte inside the MPEG stream, just before APEv2
    p.write_bytes(bytes(data))
    assert read_file(p).tags.snapshot["tags"]["audio_region"] != before


# --------------------------------------------------------------------------- 3: chunk offsets relative to mdat

def _stco_offsets(data):
    out = []

    def walk(start, end):
        for t, s, ps, e in mp4_atoms(data, start, end):
            if t in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                walk(ps, e)
            elif t == b"stco":
                out.append(ps)
    walk(0, len(data))
    return out


def test_stale_chunk_offsets_caught_by_verify(corpus):
    rel = Z + "m4a_aac_faststart.m4a"
    p = corpus.path(rel)
    orig = p.read_bytes()
    r = read_file(p)
    res = apply(r.genre, "add", ["A genre long enough to move the media data " * 5])
    assert run_mutate(p, r.format, res.values, r.genre.values).ok
    data = bytearray(p.read_bytes())
    # Put the stco table back to its pre-write values: offsets now point before the moved mdat.
    old_body_at = _stco_offsets(orig)[0]
    new_body_at = _stco_offsets(bytes(data))[0]
    count = int.from_bytes(orig[old_body_at + 4:old_body_at + 8], "big")
    n = 8 + 4 * count
    data[new_body_at:new_body_at + n] = orig[old_body_at:old_body_at + n]
    p.write_bytes(bytes(data))
    v = verify_temp(corpus.master / rel, p, list(res.values), audio="off")
    assert not v.ok and any("boxes" in x for x in v.problems), v.problems


# --------------------------------------------------------------------------- 4: FLAC without comment block

def test_flac_without_comment_block_refused(corpus):
    r = read_file(corpus.path(Z + "flac_no_comment_block.flac"))
    assert r.ok and r.skip_reason == "flac-no-comment-block"


# --------------------------------------------------------------------------- 5: "TAG" near the end

def test_tag_near_end_refused(corpus):
    p = corpus.root / "tagnear.mp3"
    p.write_bytes(corpus.path(Z + "mp3_untagged.mp3").read_bytes() + b"TAG" + b"\0" * 124)
    r = read_file(p)
    assert r.ok and r.skip_reason == "id3v1-misplaced"


# --------------------------------------------------------------------------- 6: ext header, frame flags

def _frames4(extra_flags=0):
    return b"".join(f.encode() + _syncsafe(len(b)) + struct.pack(">H", fl) + b for f, b, fl in [
        ("TIT2", id3_text_body(["T"], 3), 0), ("TPE3", id3_text_body(["Conductor"], 3), extra_flags),
        ("TCON", id3_text_body(["Rock"], 3), 0)])


def _mp3_with(corpus, tag: bytes, name: str):
    p = corpus.root / name
    p.write_bytes(tag + corpus.path(Z + "mp3_untagged.mp3").read_bytes())
    return p


def test_v24_extended_header_with_restrictions_refused(corpus):
    ext = _syncsafe(8) + b"\x01\x10" + b"\x01\x00"
    body = ext + _frames4()
    p = _mp3_with(corpus, b"ID3\x04\x00\x40" + _syncsafe(len(body)) + body, "exthdr.mp3")
    assert read_file(p).skip_reason == "id3-extended-header"


def test_v23_extended_header_with_crc_refused(corpus):
    frames = b"".join(f.encode() + struct.pack(">IH", len(b), 0) + b
                      for f, b in [("TIT2", id3_text_body(["T"], 0)), ("TCON", id3_text_body(["Rock"], 0))])
    ext = struct.pack(">IHI", 10, 0x8000, 0) + b"\0\0\0\0"
    body = ext + frames
    p = _mp3_with(corpus, b"ID3\x03\x00\x40" + _syncsafe(len(body)) + body, "exthdr3.mp3")
    assert read_file(p).skip_reason == "id3-extended-header"


@pytest.mark.parametrize("flags", [0x4000, 0x2000, 0x1000])
def test_v24_frame_status_flags_refused(corpus, flags):
    body = _frames4(flags)
    p = _mp3_with(corpus, b"ID3\x04\x00\x00" + _syncsafe(len(body)) + body, "flags.mp3")
    assert read_file(p).skip_reason == "id3-frame-flags"


# --------------------------------------------------------------------------- 7: genre key spelling verified

def test_verify_checks_genre_key_spelling(corpus):
    rel = Z + "flac_multi_genre.flac"  # uses lowercase "genre"
    p = corpus.path(rel)
    subprocess.run(["metaflac", "--remove-tag=genre", "--set-tag=GENRE=Rock", "--set-tag=GENRE=Blues",
                    "--set-tag=GENRE=Rock", "--set-tag=GENRE=Jazz", str(p)], check=True)
    v = verify_temp(corpus.master / rel, p, ["Rock", "Blues", "Rock", "Jazz"], audio="off")
    assert not v.ok and any("genre field keys" in x for x in v.problems)


# --------------------------------------------------------------------------- 8: padded Vorbis in the oracle

def test_oracle_accepts_padding_after_framing_byte_and_rejects_junk(corpus):
    rel = Z + "ogg_vorbis_padded.ogg"
    assert compare(corpus.master / rel, corpus.path(rel), "genres") == []
    p = corpus.path(rel)
    _, packets, _ = ogg_header_packets(p.read_bytes())
    pkt = packets[1]
    ogg_set_comment_packet(p, pkt[:-3000] + b"\x05" + b"\0" * 2999)  # non-zero byte after the framing byte
    assert any("framing" in x for x in compare(corpus.master / rel, p, "genres"))


def test_padded_vorbis_write_keeps_padding(corpus):
    rel = Z + "ogg_vorbis_big_comment.ogg"
    p = corpus.path(rel)
    r = read_file(p)
    res = apply(r.genre, "add", ["Jazz"])
    assert run_mutate(p, r.format, res.values, r.genre.values).ok
    assert compare(corpus.master / rel, p, "genres", list(res.values)) == []
    assert verify_temp(corpus.master / rel, p, res.values).ok
    assert vorbis_comment_packet  # builder helper used by the fixture


# --------------------------------------------------------------------------- 9: grouping identity

def test_grouping_identity_frame_not_falsely_refused(corpus):
    frames = (b"TIT2" + _syncsafe(3) + b"\x00\x40" + b"\x07\x03T"      # group byte 7, then UTF-8 text
              + b"TCON" + _syncsafe(5) + b"\x00\x00" + b"\x03Rock")
    p = _mp3_with(corpus, b"ID3\x04\x00\x00" + _syncsafe(len(frames)) + frames, "grp.mp3")
    assert read_file(p).skip_reason != "non-utf8-text"
