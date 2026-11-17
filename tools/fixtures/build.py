#!/usr/bin/env python3
"""Build Gama's fully synthetic audio fixture corpus.

Nothing is copied from a real collection. Audio comes from ffmpeg's ``lavfi``
generators (sine + noise), cover art from ``lavfi color``. Tags are written by
``metaflac`` (FLAC) or by the raw-byte writers in this module (ID3v2.2/2.3/2.4,
ID3v1, APEv2, Vorbis/Opus comment packets, MP4 ``ilst`` atoms). mutagen is not
used, so the library under test never shapes its own fixtures.

Usage::

    python tools/fixtures/build.py <out_dir>

Writes ``<out_dir>/library/...`` and ``<out_dir>/manifest.json``. Refuses an
``out_dir`` at or under the production collection or a ``GAMA_LIBRARY_ROOTS`` root. Importable:
``build(out_dir) -> dict`` returns the same manifest.

Manifest entries (keyed by path relative to ``library/``)::

    {"format": "flac|mp3|mp4|ogg-vorbis|ogg-opus|wav|broken",
     "group": "album|format|broken",
     "genres": [...stored genre values in order...] | None,   # None: no genre stored
                                                              # (broken group: not asserted)
     "props": [...short property labels the inventory test checks...]}
"""
from __future__ import annotations

import base64
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

AUDIO_SECONDS = 1.5

# The real collection. The builder refuses to write at or under these, and
# under any root named in GAMA_LIBRARY_ROOTS.
PRODUCTION_ROOTS = (Path("/storage/music"),)


def unsafe_output_dir(out_dir: Path) -> Path | None:
    """The production/library root that ``out_dir`` resolves into, or None if it is safe."""
    out = Path(out_dir).expanduser().resolve(strict=False)
    roots = list(PRODUCTION_ROOTS)
    roots += [Path(r) for r in os.environ.get("GAMA_LIBRARY_ROOTS", "").split(os.pathsep) if r.strip()]
    for root in roots:
        for r in {root.expanduser(), root.expanduser().resolve(strict=False)}:
            if out == r or out.is_relative_to(r):
                return root
    return None


# --------------------------------------------------------------------------- helpers

def run(*args: str) -> None:
    r = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"{args[0]} failed ({r.returncode}): {r.stderr.decode(errors='replace')[-800:]}")


def ffmpeg(*args: str) -> None:
    run("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args)


def tools_available() -> list[str]:
    """Names of required external tools that are missing."""
    return [t for t in ("ffmpeg", "ffprobe", "metaflac") if shutil.which(t) is None]


def lavfi_audio(freq: int = 440) -> list[str]:
    return ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={AUDIO_SECONDS}",
            "-f", "lavfi", "-i", f"anoisesrc=d={AUDIO_SECONDS}:a=0.02:seed=7",
            "-filter_complex", "[0:a][1:a]amix=inputs=2,aformat=channel_layouts=stereo"]


# --------------------------------------------------------------------------- ID3

def syncsafe(n: int) -> bytes:
    if n >= 1 << 28:
        raise ValueError("too large for syncsafe int")
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _enc_text(s: str, enc: int) -> bytes:
    if enc == 0:
        return s.encode("latin-1")
    if enc == 1:
        return s.encode("utf-16")  # with BOM
    if enc == 2:
        return s.encode("utf-16-be")
    return s.encode("utf-8")


def _term(enc: int) -> bytes:
    return b"\0\0" if enc in (1, 2) else b"\0"


def id3_text_body(values: list[str], enc: int) -> bytes:
    """Text frame body. Several values are NUL-separated (ID3v2.4 multi-value)."""
    return bytes([enc]) + _term(enc).join(_enc_text(v, enc) for v in values)


def id3_txxx_body(desc: str, value: str, enc: int) -> bytes:
    return bytes([enc]) + _enc_text(desc, enc) + _term(enc) + _enc_text(value, enc)


def id3_uslt_body(text: str, enc: int, lang: str = "eng", desc: str = "") -> bytes:
    return bytes([enc]) + lang.encode() + _enc_text(desc, enc) + _term(enc) + _enc_text(text, enc)


def id3_apic_body(img: bytes, mime: str, ptype: int, desc: str, enc: int) -> bytes:
    return bytes([enc]) + mime.encode() + b"\0" + bytes([ptype]) + _enc_text(desc, enc) + _term(enc) + img


def id3_ufid_body(owner: str, ident: bytes) -> bytes:
    return owner.encode("latin-1") + b"\0" + ident


def id3_popm_body(email: str, rating: int, count: int) -> bytes:
    return email.encode("latin-1") + b"\0" + bytes([rating]) + struct.pack(">I", count)


def id3_frame(fid: str, body: bytes, version: int) -> bytes:
    if version == 2:
        return fid.encode() + len(body).to_bytes(3, "big") + body
    size = syncsafe(len(body)) if version == 4 else struct.pack(">I", len(body))
    return fid.encode() + size + b"\0\0" + body


def id3v2_tag(frames: list[tuple[str, bytes]], version: int, padding: int = 128) -> bytes:
    """A complete ID3v2.<version> tag (no unsync, no extended header)."""
    body = b"".join(id3_frame(fid, b, version) for fid, b in frames) + b"\0" * padding
    return b"ID3" + bytes([version, 0, 0]) + syncsafe(len(body)) + body


def id3v1(title: str, artist: str, album: str, year: str, comment: str = "",
          track: int = 1, genre_byte: int = 17) -> bytes:
    def f(s: str, n: int) -> bytes:
        return s.encode("latin-1")[:n].ljust(n, b"\0")
    return b"TAG" + f(title, 30) + f(artist, 30) + f(album, 30) + f(year, 4) + f(comment, 28) + b"\0" + bytes([track, genre_byte])


def ape_v2_tag(items: dict[str, str]) -> bytes:
    """APEv2 tag with header and footer, as appended by foobar2000/mp3gain."""
    body = b""
    for k, v in items.items():
        vb = v.encode()
        body += struct.pack("<II", len(vb), 0) + k.encode() + b"\0" + vb
    size = len(body) + 32
    hdr = b"APETAGEX" + struct.pack("<IIII", 2000, size, len(items), 0xA0000000) + b"\0" * 8
    ftr = b"APETAGEX" + struct.pack("<IIII", 2000, size, len(items), 0x80000000) + b"\0" * 8
    return hdr + body + ftr


def id3_common_frames(t: dict, version: int, enc: int) -> list[tuple[str, bytes]]:
    """Frames for the common track fields of an album track description ``t``."""
    fr = [("TIT2", id3_text_body([t["title"]], enc)),
          ("TPE1", id3_text_body([t["artist"]], enc)),
          ("TALB", id3_text_body([t["album"]], enc)),
          ("TRCK", id3_text_body([t["track"]], enc))]
    if t.get("albumartist"):
        fr.append(("TPE2", id3_text_body([t["albumartist"]], enc)))
    if t.get("disc"):
        fr.append(("TPOS", id3_text_body([t["disc"]], enc)))
    if version == 4:
        fr.append(("TDRC", id3_text_body([t.get("date", "1972")], enc)))
    else:
        fr.append(("TYER", id3_text_body([t.get("date", "1972")[:4]], enc)))
        fr.append(("TDAT", id3_text_body(["1503"], enc)))
    if t.get("mb_albumid"):
        fr.append(("TXXX", id3_txxx_body("MusicBrainz Album Id", t["mb_albumid"], enc)))
    if t.get("genres"):
        vals = t["genres"] if version == 4 else ["/".join(t["genres"])] if len(t["genres"]) > 1 else t["genres"]
        fr.append(("TCON", id3_text_body(vals, enc)))
    return fr


# --------------------------------------------------------------------------- Ogg

def _ogg_crc_table() -> list[int]:
    tbl = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        tbl.append(r & 0xFFFFFFFF)
    return tbl


_CRC = _ogg_crc_table()


def ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) & 0xFF) ^ b]
    return crc


def ogg_pages(data: bytes) -> list[dict]:
    pages, pos = [], 0
    while pos < len(data):
        if data[pos:pos + 4] != b"OggS":
            raise ValueError(f"no Ogg page at offset {pos}")
        nseg = data[pos + 26]
        lacing = list(data[pos + 27:pos + 27 + nseg])
        start = pos + 27 + nseg
        body = data[start:start + sum(lacing)]
        pages.append({"flags": data[pos + 5], "granule": data[pos + 6:pos + 14],
                      "serial": data[pos + 14:pos + 18], "seq": int.from_bytes(data[pos + 18:pos + 22], "little"),
                      "lacing": lacing, "body": body})
        pos = start + sum(lacing)
    return pages


def ogg_page_bytes(flags: int, granule: bytes, serial: bytes, seq: int, lacing: list[int], body: bytes) -> bytes:
    hdr = b"OggS\0" + bytes([flags]) + granule + serial + seq.to_bytes(4, "little") + b"\0\0\0\0" + bytes([len(lacing)]) + bytes(lacing)
    crc = ogg_crc(hdr + body)
    return hdr[:22] + crc.to_bytes(4, "little") + hdr[26:] + body


def _paginate(packets: list[bytes], serial: bytes, seq0: int, first_flags: int) -> list[bytes]:
    """Lay packets out on pages (granule 0; -1 on pages where no packet ends)."""
    segs: list[tuple[int, bytes, bool]] = []  # (lace value, data, ends a packet)
    for p in packets:
        n = len(p)
        i = 0
        while True:
            chunk = p[i:i + 255]
            segs.append((len(chunk), chunk, len(chunk) < 255))
            i += 255
            if len(chunk) < 255:
                break
            if i == n:  # exact multiple of 255: terminating zero-length segment
                segs.append((0, b"", True))
                break
    out, seq, cont = [], seq0, False
    for start in range(0, len(segs), 255):
        page = segs[start:start + 255]
        flags = (first_flags if not out else 0) | (0x01 if cont else 0)
        ends = any(e for _, _, e in page)
        granule = (0).to_bytes(8, "little") if ends else (0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
        out.append(ogg_page_bytes(flags, granule, serial, seq, [lv for lv, _, _ in page], b"".join(d for _, d, _ in page)))
        cont = not page[-1][2]
        seq += 1
    return out


def ogg_header_packets(data: bytes) -> tuple[str, list[bytes], int]:
    """(codec, header packets, number of pages they occupy)."""
    pages = ogg_pages(data)
    first = pages[0]["body"]
    codec = "opus" if first.startswith(b"OpusHead") else "vorbis" if first.startswith(b"\x01vorbis") else None
    if codec is None:
        raise ValueError("not an Ogg Vorbis/Opus stream")
    want = 2 if codec == "opus" else 3
    packets, cur = [], b""
    for i, pg in enumerate(pages):
        off = 0
        for lv in pg["lacing"]:
            cur += pg["body"][off:off + lv]
            off += lv
            if lv < 255:
                packets.append(cur)
                cur = b""
        if len(packets) >= want:
            if len(packets) != want or cur:
                raise ValueError("header packets do not end on a page boundary")
            return codec, packets, i + 1
    raise ValueError("truncated Ogg headers")


def vorbis_comment_packet(codec: str, vendor: str, comments: list[str], padding: int = 0) -> bytes:
    """A comment packet; ``padding`` zero bytes follow (after the framing byte for Vorbis),
    as mutagen, Picard and others write them."""
    body = struct.pack("<I", len(vendor.encode())) + vendor.encode() + struct.pack("<I", len(comments))
    for c in comments:
        cb = c.encode("utf-8")
        body += struct.pack("<I", len(cb)) + cb
    pad = b"\0" * padding
    return (b"OpusTags" + body + pad) if codec == "opus" else (b"\x03vorbis" + body + b"\x01" + pad)


def ogg_vendor(packet: bytes) -> str:
    i = 8 if packet.startswith(b"OpusTags") else 7
    n = int.from_bytes(packet[i:i + 4], "little")
    return packet[i + 4:i + 4 + n].decode("utf-8")


def ogg_set_comments(path: Path, comments: list[str], vendor: str | None = None, padding: int = 0) -> None:
    """Replace the whole comment list of an Ogg Vorbis/Opus file, raw.

    Audio pages are copied byte-for-byte except for their sequence numbers
    (renumbered when the header grows to more pages) and CRCs.
    """
    codec, packets, _ = ogg_header_packets(path.read_bytes())
    vendor = ogg_vendor(packets[1]) if vendor is None else vendor
    ogg_set_comment_packet(path, vorbis_comment_packet(codec, vendor, comments, padding))


def ogg_set_comment_packet(path: Path, new_comment: bytes) -> None:
    """Replace the comment packet (packet 1) of an Ogg Vorbis/Opus file with raw bytes."""
    data = path.read_bytes()
    _codec, packets, nhdr = ogg_header_packets(data)
    pages = ogg_pages(data)
    serial = pages[0]["serial"]
    out = [ogg_page_bytes(pages[0]["flags"], pages[0]["granule"], serial, 0, pages[0]["lacing"], pages[0]["body"])]
    rest_headers = [new_comment] + packets[2:]
    out += _paginate(rest_headers, serial, 1, 0)
    seq = len(out)
    for pg in pages[nhdr:]:
        out.append(ogg_page_bytes(pg["flags"], pg["granule"], pg["serial"], seq, pg["lacing"], pg["body"]))
        seq += 1
    path.write_bytes(b"".join(out))


def flac_set_raw_comments(path: Path, comments: list[bytes]) -> None:
    """Replace the comment list of a FLAC's VORBIS_COMMENT block with raw byte strings.

    Keeps the vendor string and the block's position; the file grows or
    shrinks (audio frames are not touched). For fixtures with non-UTF-8 bytes,
    which metaflac will not write.
    """
    data = path.read_bytes()
    if data[:4] != b"fLaC":
        raise ValueError("flac_set_raw_comments needs a plain FLAC (no ID3 prefix)")
    pos = 4
    while True:
        hdr = data[pos]
        blen = int.from_bytes(data[pos + 1:pos + 4], "big")
        if hdr & 0x7F == 4:
            old = data[pos + 4:pos + 4 + blen]
            vlen = int.from_bytes(old[:4], "little")
            body = old[:4 + vlen] + struct.pack("<I", len(comments))
            body += b"".join(struct.pack("<I", len(c)) + c for c in comments)
            path.write_bytes(data[:pos] + bytes([hdr]) + len(body).to_bytes(3, "big") + body
                             + data[pos + 4 + blen:])
            return
        if hdr & 0x80:
            raise ValueError("no VORBIS_COMMENT block")
        pos += 4 + blen


def flac_picture_block(img: bytes, mime: str, ptype: int, desc: str, w: int = 64, h: int = 64) -> bytes:
    m, d = mime.encode(), desc.encode()
    return (struct.pack(">II", ptype, len(m)) + m + struct.pack(">I", len(d)) + d
            + struct.pack(">IIIII", w, h, 24, 0, len(img)) + img)


# --------------------------------------------------------------------------- MP4

def mp4_atoms(buf: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        size = int.from_bytes(buf[i:i + 4], "big")
        typ = buf[i + 4:i + 8]
        hdr = 8
        if size == 1:
            size, hdr = int.from_bytes(buf[i + 8:i + 16], "big"), 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            raise ValueError(f"bad atom {typ!r} at {i}")
        yield typ, i, i + hdr, i + size
        i += size


def mp4_box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def mp4_data(type_code: int, payload: bytes) -> bytes:
    return mp4_box(b"data", struct.pack(">II", type_code, 0) + payload)


def mp4_text_item(key: bytes, values: list[str]) -> bytes:
    return mp4_box(key, b"".join(mp4_data(1, v.encode("utf-8")) for v in values))


def mp4_freeform(mean: str, name: str, values: list[str]) -> bytes:
    return mp4_box(b"----", mp4_box(b"mean", b"\0\0\0\0" + mean.encode())
                   + mp4_box(b"name", b"\0\0\0\0" + name.encode())
                   + b"".join(mp4_data(1, v.encode()) for v in values))


def mp4_trkn(n: int, total: int) -> bytes:
    return mp4_box(b"trkn", mp4_data(0, struct.pack(">HHHH", 0, n, total, 0)))


def mp4_disk(n: int, total: int) -> bytes:
    return mp4_box(b"disk", mp4_data(0, struct.pack(">HHH", 0, n, total)))


def mp4_gnre(id3_index: int) -> bytes:
    return mp4_box(b"gnre", mp4_data(0, struct.pack(">H", id3_index + 1)))


def mp4_covr(img: bytes, jpeg: bool = True) -> bytes:
    return mp4_box(b"covr", mp4_data(13 if jpeg else 14, img))


def mp4_set_ilst(path: Path, items: list[bytes]) -> None:
    """Replace moov/udta/meta/ilst with ``items``, raw.

    Only valid when moov follows mdat (no chunk offsets move), which is
    asserted. Creates udta/meta(hdlr)/ilst when missing.
    """
    data = path.read_bytes()
    top = list(mp4_atoms(data, 0, len(data)))
    types = [t for t, *_ in top]
    if b"moov" not in types or b"mdat" not in types or types.index(b"moov") < types.index(b"mdat"):
        raise ValueError("mp4_set_ilst needs moov after mdat")
    _, ms, mps, me = next(a for a in top if a[0] == b"moov")
    moov_children = [(t, s, ps, e) for t, s, ps, e in mp4_atoms(data, mps, me)]
    new_ilst = mp4_box(b"ilst", b"".join(items))
    hdlr = mp4_box(b"hdlr", b"\0" * 8 + b"mdirappl" + b"\0" * 9)
    new_children = b""
    had_udta = False
    for t, s, ps, e in moov_children:
        if t != b"udta":
            new_children += data[s:e]
            continue
        had_udta = True
        udta_kids = b""
        had_meta = False
        for t2, s2, ps2, e2 in mp4_atoms(data, ps, e):
            if t2 != b"meta":
                udta_kids += data[s2:e2]
                continue
            had_meta = True
            meta_payload = data[ps2:ps2 + 4]  # version/flags
            had_ilst = False
            for t3, s3, ps3, e3 in mp4_atoms(data, ps2 + 4, e2):
                if t3 == b"ilst":
                    meta_payload += new_ilst
                    had_ilst = True
                else:
                    meta_payload += data[s3:e3]
            if not had_ilst:
                meta_payload += new_ilst
            udta_kids += mp4_box(b"meta", meta_payload)
        if not had_meta:
            udta_kids += mp4_box(b"meta", b"\0\0\0\0" + hdlr + new_ilst)
        new_children += mp4_box(b"udta", udta_kids)
    if not had_udta:
        new_children += mp4_box(b"udta", mp4_box(b"meta", b"\0\0\0\0" + hdlr + new_ilst))
    path.write_bytes(data[:ms] + mp4_box(b"moov", new_children) + data[me:])


def mp4_common_items(t: dict) -> list[bytes]:
    items = [mp4_text_item(b"\xa9nam", [t["title"]]), mp4_text_item(b"\xa9ART", [t["artist"]]),
             mp4_text_item(b"\xa9alb", [t["album"]]), mp4_text_item(b"\xa9day", [t.get("date", "1972")]),
             mp4_trkn(int(t["track"]), int(t.get("tracktotal", 0)))]
    if t.get("albumartist"):
        items.append(mp4_text_item(b"aART", [t["albumartist"]]))
    if t.get("disc"):
        items.append(mp4_disk(int(t["disc"]), int(t.get("disctotal", 0))))
    if t.get("mb_albumid"):
        items.append(mp4_freeform("com.apple.iTunes", "MusicBrainz Album Id", [t["mb_albumid"]]))
    if t.get("genres"):
        items.append(mp4_text_item(b"\xa9gen", t["genres"]))
    items.append(mp4_text_item(b"\xa9too", ["Gama fixture builder"]))
    return items


# --------------------------------------------------------------------------- builder

class Builder:
    def __init__(self, out_dir: Path):
        self.out = Path(out_dir)
        self.lib = self.out / "library"
        self.manifest: dict[str, dict] = {}
        self.tmp = Path(tempfile.mkdtemp(prefix="gama-fixture-base-"))
        self.base: dict[str, Path] = {}

    # ---- shared inputs, made once ------------------------------------------------
    def make_bases(self) -> None:
        t = self.tmp
        ffmpeg("-f", "lavfi", "-i", "color=c=0x8a2be2:s=64x64", "-frames:v", "1", str(t / "front.jpg"))
        ffmpeg("-f", "lavfi", "-i", "color=c=0x2e8b57:s=48x48", "-frames:v", "1", str(t / "back.png"))
        self.front = (t / "front.jpg").read_bytes()
        self.back = (t / "back.png").read_bytes()
        a = lavfi_audio() + ["-ar", "44100"]
        ffmpeg(*a, "-c:a", "flac", "-map_metadata", "-1", str(t / "base.flac"))
        ffmpeg(*a, "-c:a", "libmp3lame", "-b:a", "64k", "-map_metadata", "-1",
               "-id3v2_version", "0", "-write_id3v1", "0", str(t / "base.mp3"))
        ffmpeg(*a, "-c:a", "libvorbis", "-q:a", "1", "-map_metadata", "-1", str(t / "base.ogg"))
        ffmpeg(*lavfi_audio(), "-ar", "48000", "-c:a", "libopus", "-b:a", "48k", "-map_metadata", "-1", str(t / "base.opus"))
        ffmpeg(*a, "-c:a", "aac", "-b:a", "64k", "-map_metadata", "-1", str(t / "base.m4a"))
        ffmpeg(*a, "-c:a", "alac", "-map_metadata", "-1", str(t / "base_alac.m4a"))
        ffmpeg(*a, "-c:a", "pcm_s16le", "-map_metadata", "-1", str(t / "base.wav"))
        for k in ("flac", "mp3", "ogg", "opus", "m4a", "wav"):
            self.base[k] = t / f"base.{k}"
        self.base["alac"] = t / "base_alac.m4a"
        if self.base["mp3"].read_bytes()[:3] == b"ID3":
            raise RuntimeError("ffmpeg wrote an ID3 tag despite -id3v2_version 0")

    def _dest(self, rel: str) -> Path:
        p = self.lib / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def record(self, rel: str, fmt: str, group: str, genres: list[str] | None, *props: str) -> None:
        self.manifest[rel] = {"format": fmt, "group": group, "genres": genres, "props": list(props)}

    # ---- per-format track writers -----------------------------------------------------
    def vorbis_comments(self, t: dict) -> list[str]:
        c = [f"TITLE={t['title']}", f"ARTIST={t['artist']}", f"ALBUM={t['album']}",
             f"TRACKNUMBER={t['track']}", f"DATE={t.get('date', '1972')}"]
        if t.get("albumartist"):
            c.append(f"ALBUMARTIST={t['albumartist']}")
        if t.get("disc"):
            c.append(f"DISCNUMBER={t['disc']}")
        if t.get("mb_albumid"):
            c.append(f"MUSICBRAINZ_ALBUMID={t['mb_albumid']}")
        c += [f"{t.get('genre_key', 'GENRE')}={g}" for g in t.get("genres", [])]
        c += t.get("extra_comments", [])
        return c

    def flac(self, rel: str, t: dict, *, pictures: tuple = ("front",), padding: bool = True) -> Path:
        p = self._dest(rel)
        shutil.copyfile(self.base["flac"], p)
        args = ["metaflac", "--remove-all-tags", "--no-utf8-convert"]
        args += [f"--set-tag={c}" for c in self.vorbis_comments(t)]
        run(*args, str(p))
        for which in pictures:
            if which == "front":
                spec = f"3|image/jpeg|front cover||{self.tmp / 'front.jpg'}"
            else:
                spec = f"4|image/png|back cover ✓||{self.tmp / 'back.png'}"
            run("metaflac", f"--import-picture-from={spec}", str(p))
        if not padding:
            run("metaflac", "--remove", "--block-type=PADDING", "--dont-use-padding", str(p))
        return p

    def ogg(self, rel: str, t: dict, codec: str, picture: bool = True, padding: int = 0) -> Path:
        p = self._dest(rel)
        shutil.copyfile(self.base["opus" if codec == "opus" else "ogg"], p)
        comments = self.vorbis_comments(t)
        if picture:
            pic = flac_picture_block(self.front, "image/jpeg", 3, "front cover")
            comments.append("METADATA_BLOCK_PICTURE=" + base64.b64encode(pic).decode())
        ogg_set_comments(p, comments, padding=padding)
        return p

    def mp3(self, rel: str, t: dict, *, version: int = 4, enc: int = 3, extra_frames: list | None = None,
            ape: dict | None = None, v1: bytes | None = None, padding: int = 128) -> Path:
        p = self._dest(rel)
        audio = self.base["mp3"].read_bytes()
        frames = id3_common_frames(t, version, enc) + (extra_frames or [])
        data = id3v2_tag(frames, version, padding) + audio
        if ape:
            data += ape_v2_tag(ape)
        if v1:
            data += v1
        p.write_bytes(data)
        return p

    def mp4(self, rel: str, t: dict, *, alac: bool = False, extra_items: list[bytes] | None = None,
            genre_items: list[bytes] | None = None, cover: bool = True) -> Path:
        p = self._dest(rel)
        shutil.copyfile(self.base["alac" if alac else "m4a"], p)
        t2 = dict(t)
        if genre_items is not None:
            t2["genres"] = []
        items = mp4_common_items(t2) + (genre_items or []) + (extra_items or [])
        if cover:
            items.append(mp4_covr(self.front))
        mp4_set_ilst(p, items)
        return p

    # ---- corpus ------------------------------------------------------------------
    def albums(self) -> None:
        def tr(artist, album, n, title, genres, **kw):
            d = {"artist": artist, "albumartist": artist, "album": album, "track": str(n),
                 "title": title, "genres": genres}
            d.update(kw)
            return d

        # Example album 1: FLAC, several GENRE fields, cover.
        a, al = "King Crimson", "In the Court of the Crimson King"
        for n, title in enumerate(["21st Century Schizoid Man", "I Talk to the Wind", "Epitaph"], 1):
            rel = f"King Crimson/1969 - {al}/{n:02d} {title}.flac"
            self.flac(rel, tr(a, al, n, title, ["Progressive Rock", "Art Rock"], date="1969"))
            self.record(rel, "flac", "album", ["Progressive Rock", "Art Rock"], "multi-genre-fields", "picture")

        # Example album 2: MP3 ID3v2.4, multi-value TCON.
        a, al = "Genesis", "Selling England by the Pound"
        for n, title in enumerate(["Dancing with the Moonlit Knight", "I Know What I Like", "Firth of Fifth"], 1):
            rel = f"Genesis/1973 - {al}/{n:02d} {title}.mp3"
            self.mp3(rel, tr(a, al, n, title, ["Progressive Rock", "Symphonic Rock"], date="1973"),
                     extra_frames=[("APIC", id3_apic_body(self.front, "image/jpeg", 3, "", 3))])
            self.record(rel, "mp3", "album", ["Progressive Rock", "Symphonic Rock"], "id3v2.4", "multi-tcon", "apic")

        # Example album 3: M4A AAC with a ';'-delimited single genre value.
        a, al = "Yes", "Close to the Edge"
        for n, title in enumerate(["Close to the Edge", "And You and I", "Siberian Khatru"], 1):
            rel = f"Yes/1972 - {al}/{n:02d} {title}.m4a"
            self.mp4(rel, tr(a, al, n, title, ["Rock;Progressive Rock"], date="1972"))
            self.record(rel, "mp4", "album", ["Rock;Progressive Rock"], "delimited-genre", "covr")

        # Multi-disc album with disc subdirectories.
        a, al = "Gama Test Orchestra", "Two Disc Set"
        for d in (1, 2):
            for n in (1, 2):
                rel = f"{a}/{al}/CD{d}/{n:02d} Disc {d} Track {n}.flac"
                self.flac(rel, tr(a, al, n, f"Disc {d} Track {n}", ["Classical"], disc=str(d)))
                self.record(rel, "flac", "album", ["Classical"], "multi-disc-subdir", f"disc{d}")
        # Multi-disc album in one directory, discnumber tags only.
        al = "Flat Double Album"
        for d in (1, 2):
            for n in (1, 2):
                rel = f"{a}/{al}/{d}-{n:02d} Flat {d}.{n}.mp3"
                self.mp3(rel, tr(a, al, n, f"Flat {d}.{n}", ["Classical"], disc=f"{d}/2"))
                self.record(rel, "mp3", "album", ["Classical"], "multi-disc-flat", f"disc{d}")

        # Same title, different album artists.
        for a in ("Gama Duplicate A", "Gama Duplicate B"):
            for n in (1, 2):
                rel = f"{a}/Greatest Hits/{n:02d} Hit {n}.ogg"
                self.ogg(rel, tr(a, "Greatest Hits", n, f"Hit {n}", ["Pop"]), "vorbis")
                self.record(rel, "ogg-vorbis", "album", ["Pop"], "duplicate-title")

        # Two editions of one album, different MusicBrainz release ids.
        a, al = "Gama Edition Artist", "Anniversary"
        for ed, mbid in (("1999 Original", "11111111-1111-4111-8111-111111111111"),
                         ("2019 Remaster", "22222222-2222-4222-8222-222222222222")):
            for n in (1, 2):
                rel = f"{a}/{al} ({ed})/{n:02d} Song {n}.opus"
                self.ogg(rel, tr(a, al, n, f"Song {n}", ["Indie Rock"], mb_albumid=mbid), "opus")
                self.record(rel, "ogg-opus", "album", ["Indie Rock"], "edition", mbid)

        # Inconsistent per-track genres and albumartist in one directory.
        a, al = "Gama Inconsistent", "Mixed Bag"
        tracks = [(1, ["Jazz"], a), (2, ["Jazz", "Fusion"], a), (3, [], a), (4, ["jazz", "Jazz"], "Someone Else")]
        for n, g, aa in tracks:
            rel = f"{a}/{al}/{n:02d} Mixed {n}.flac"
            self.flac(rel, tr(a, al, n, f"Mixed {n}", g, albumartist=aa))
            self.record(rel, "flac", "album", g or None, "inconsistent", f"albumartist={aa}")

    def formats(self) -> None:
        base = {"artist": "Gama Format Artist", "albumartist": "Gama Format Artist", "album": "Format Zoo",
                "track": "1", "title": "Format"}

        def t(**kw):
            d = dict(base)
            d.update(kw)
            return d

        F = "Format Zoo/"
        # ---- FLAC
        self.flac(F + "flac_single_genre.flac", t(genres=["Rock"]))
        self.record(F + "flac_single_genre.flac", "flac", "format", ["Rock"], "single-genre")
        self.flac(F + "flac_multi_genre.flac", t(genres=["Rock", "Blues", "Rock"], genre_key="genre",
                                                 extra_comments=["REPLAYGAIN_TRACK_GAIN=-6.50 dB",
                                                                 "CUSTOM_UNRELATED=keep me ✓ ünïcødé"]))
        self.record(F + "flac_multi_genre.flac", "flac", "format", ["Rock", "Blues", "Rock"],
                    "multi-genre-fields", "lowercase-key", "duplicate-genre", "replaygain")
        rel = F + "flac_two_pictures_no_padding.flac"
        self.flac(rel, t(genres=["Ambient"]), pictures=("front", "back"), padding=False)
        self.record(rel, "flac", "format", ["Ambient"], "two-pictures", "no-padding")
        self.flac(F + "flac_delimited.flac", t(genres=["Rock;Progressive Rock"]))
        self.record(F + "flac_delimited.flac", "flac", "format", ["Rock;Progressive Rock"], "delimited-genre")
        self.flac(F + "flac_mixed_style.flac", t(genres=["Rock;Pop", "Jazz"]))
        self.record(F + "flac_mixed_style.flac", "flac", "format", ["Rock;Pop", "Jazz"], "mixed-style")
        self.flac(F + "flac_no_genre.flac", t(genres=[]))
        self.record(F + "flac_no_genre.flac", "flac", "format", None, "no-genre")
        # FLAC with a (non-standard) ID3v2.3 tag prepended.
        rel = F + "flac_with_id3v2_prefix.flac"
        src = self.flac(rel, t(genres=["Electronic"]))
        prefix = id3v2_tag([("TIT2", id3_text_body(["Prepended ID3v2"], 0))], 3, padding=16)
        src.write_bytes(prefix + src.read_bytes())
        self.record(rel, "flac", "format", ["Electronic"], "id3-prefix")
        # FLAC with a latin-1 (invalid UTF-8) comment, as old rippers wrote them.
        rel = F + "flac_latin1_comment.flac"
        self.flac(rel, t(genres=["Jazz"]))
        flac_set_raw_comments(self.lib / rel, [c.encode() for c in self.vorbis_comments(t(genres=["Jazz"]))]
                              + [b"COMMENT=caf\xe9 au lait"])
        self.record(rel, "flac", "format", ["Jazz"], "non-utf8-comment")

        # ---- MP3
        rel = F + "mp3_v24_full.mp3"
        extra = [
            ("TXXX", id3_txxx_body("CUSTOM_UNRELATED", "keep me ✓ ünïcødé", 3)),
            ("TXXX", id3_txxx_body("REPLAYGAIN_TRACK_GAIN", "-3.20 dB", 3)),
            ("USLT", id3_uslt_body("la la la\nsecond line ✓", 3)),
            ("APIC", id3_apic_body(self.front, "image/jpeg", 3, "front", 3)),
            ("APIC", id3_apic_body(self.back, "image/png", 4, "back", 3)),
            ("UFID", id3_ufid_body("http://musicbrainz.org", b"8f0e2c6e-0000-4000-8000-000000000001")),
            ("POPM", id3_popm_body("gama@example.invalid", 196, 7)),
        ]
        self.mp3(rel, t(genres=["Post-Punk", "New Wave", "Gothic Rock"]), extra_frames=extra,
                 ape={"MP3GAIN_MINMAX": "120,210", "REPLAYGAIN_TRACK_GAIN": "-3.2 dB", "Genre": "Ape Genre"},
                 v1=id3v1("Format", "Gama Format Artist", "Format Zoo", "1979", genre_byte=43))
        self.record(rel, "mp3", "format", ["Post-Punk", "New Wave", "Gothic Rock"],
                    "id3v2.4", "multi-tcon", "apic", "uslt", "txxx", "ufid", "popm", "apev2", "id3v1")
        rel = F + "mp3_v23_slash_genre.mp3"
        self.mp3(rel, t(genres=["Rock/Post-Punk"]), version=3, enc=1,
                 extra_frames=[("TXXX", id3_txxx_body("CUSTOM_UNRELATED", "keep me", 1)),
                               ("APIC", id3_apic_body(self.front, "image/jpeg", 3, "", 0))])
        self.record(rel, "mp3", "format", ["Rock/Post-Punk"], "id3v2.3", "utf16", "slash-genre")
        rel = F + "mp3_v23_numeric_genre.mp3"
        self.mp3(rel, t(genres=["(17)"]), version=3, enc=0)
        self.record(rel, "mp3", "format", ["(17)"], "id3v2.3", "numeric-tcon")
        rel = F + "mp3_v22.mp3"
        p = self._dest(rel)
        v22 = id3v2_tag([("TT2", id3_text_body(["Format v2.2"], 0)), ("TP1", id3_text_body(["Gama Format Artist"], 0)),
                         ("TCO", id3_text_body(["Rock"], 0))], 2)
        p.write_bytes(v22 + self.base["mp3"].read_bytes())
        self.record(rel, "mp3", "format", ["Rock"], "id3v2.2")
        rel = F + "mp3_id3v1_only.mp3"
        p = self._dest(rel)
        p.write_bytes(self.base["mp3"].read_bytes() + id3v1("V1 Title", "V1 Artist", "V1 Album", "1999", genre_byte=17))
        self.record(rel, "mp3", "format", None, "id3v1-only")
        rel = F + "mp3_untagged.mp3"
        shutil.copyfile(self.base["mp3"], self._dest(rel))
        self.record(rel, "mp3", "format", None, "untagged")

        # ---- MP4
        rel = F + "m4a_aac_moov_after_mdat.m4a"
        self.mp4(rel, t(genres=["Jazz"]), extra_items=[
            mp4_freeform("com.apple.iTunes", "MusicBrainz Track Id", ["8f0e2c6e-0000-4000-8000-000000000002"]),
            mp4_freeform("com.apple.iTunes", "CUSTOM_UNRELATED", ["keep me ✓"])])
        self.record(rel, "mp4", "format", ["Jazz"], "moov-after-mdat", "freeform", "covr")
        rel = F + "m4a_aac_faststart.m4a"
        tmp_fs = self.tmp / "fs_src.m4a"
        shutil.copyfile(self.base["m4a"], tmp_fs)
        ffmpeg("-i", str(tmp_fs), "-map", "0", "-c", "copy", "-metadata", "title=Faststart", "-metadata",
               "artist=Gama Format Artist", "-metadata", "album=Format Zoo", "-metadata", "genre=Funk",
               "-movflags", "+faststart", str(self._dest(rel)))
        self.record(rel, "mp4", "format", ["Funk"], "faststart")
        rel = F + "m4a_alac.m4a"
        self.mp4(rel, t(genres=["Ambient"]), alac=True,
                 extra_items=[mp4_freeform("com.apple.iTunes", "MusicBrainz Album Id",
                                           ["33333333-3333-4333-8333-333333333333"])])
        self.record(rel, "mp4", "format", ["Ambient"], "alac", "freeform")
        rel = F + "m4a_delimited.m4a"
        self.mp4(rel, t(genres=["Rock;Progressive Rock"]))
        self.record(rel, "mp4", "format", ["Rock;Progressive Rock"], "delimited-genre")
        rel = F + "m4a_multi_data.m4a"
        self.mp4(rel, t(), genre_items=[mp4_text_item(b"\xa9gen", ["Rock", "Progressive Rock"])])
        self.record(rel, "mp4", "format", ["Rock", "Progressive Rock"], "multi-data")
        rel = F + "m4a_gnre.m4a"
        self.mp4(rel, t(), genre_items=[mp4_gnre(17)])  # ID3 index 17 = Rock
        self.record(rel, "mp4", "format", ["<gnre 18>"], "gnre")
        rel = F + "m4a_fragmented.m4a"
        ffmpeg("-i", str(self.base["m4a"]), "-c", "copy", "-metadata", "genre=Rock",
               "-movflags", "frag_keyframe+empty_moov", str(self._dest(rel)))
        self.record(rel, "mp4", "format", ["Rock"], "fragmented")

        # ---- Ogg
        rel = F + "ogg_vorbis_multi_genre.ogg"
        self.ogg(rel, t(genres=["Electronic", "Ambient"], extra_comments=["CUSTOM_UNRELATED=keep me ✓"]), "vorbis")
        self.record(rel, "ogg-vorbis", "format", ["Electronic", "Ambient"], "multi-genre-fields", "picture")
        rel = F + "opus_multi_genre.opus"
        self.ogg(rel, t(genres=["Electronic", "Ambient"], extra_comments=["R128_TRACK_GAIN=-512"]), "opus")
        self.record(rel, "ogg-opus", "format", ["Electronic", "Ambient"], "multi-genre-fields", "picture")

        # ---- Unsupported format
        # Padded comment packets (mutagen/Picard style), one spanning many pages.
        rel = F + "ogg_vorbis_padded.ogg"
        self.ogg(rel, t(genres=["Electronic"]), "vorbis", padding=3000)
        self.record(rel, "ogg-vorbis", "format", ["Electronic"], "padded-comments")
        rel = F + "ogg_vorbis_big_comment.ogg"
        self.ogg(rel, t(genres=["Electronic", "Ambient"], extra_comments=["CUSTOM_BIG=" + "x" * 100_000]), "vorbis",
                 padding=1032)
        self.record(rel, "ogg-vorbis", "format", ["Electronic", "Ambient"], "padded-comments", "multi-page-comments")
        rel = F + "opus_padded.opus"
        self.ogg(rel, t(genres=["Electronic"]), "opus", padding=512)
        self.record(rel, "ogg-opus", "format", ["Electronic"], "padded-comments")
        # FLAC without any VORBIS_COMMENT block.
        rel = F + "flac_no_comment_block.flac"
        p = self._dest(rel)
        shutil.copyfile(self.base["flac"], p)
        run("metaflac", "--remove", "--block-type=VORBIS_COMMENT", "--dont-use-padding", str(p))
        self.record(rel, "flac", "format", None, "no-comment-block")

        rel = F + "unsupported.wav"
        shutil.copyfile(self.base["wav"], self._dest(rel))
        self.record(rel, "wav", "format", None, "unsupported")

    def broken(self) -> None:
        B = "Broken/"
        flac_src = self.lib / "Format Zoo/flac_single_genre.flac"
        mp3_src = self.lib / "Format Zoo/mp3_v24_full.mp3"
        m4a_src = self.lib / "Format Zoo/m4a_aac_moov_after_mdat.m4a"
        for src, name in ((flac_src, "truncated.flac"), (mp3_src, "truncated.mp3"), (m4a_src, "truncated.m4a")):
            b = src.read_bytes()
            self._dest(B + name).write_bytes(b[: int(len(b) * 0.6)])
            self.record(B + name, "broken", "broken", None, "truncated")
        fs = (self.lib / "Format Zoo/m4a_aac_faststart.m4a").read_bytes()
        self._dest(B + "m4a_header_only.m4a").write_bytes(fs[:4096])
        self.record(B + "m4a_header_only.m4a", "broken", "broken", None, "header-only")
        self._dest(B + "zero_bytes.mp3").write_bytes(b"")
        self.record(B + "zero_bytes.mp3", "broken", "broken", None, "zero-bytes")
        self._dest(B + "garbage.flac").write_bytes(random.Random(1234).randbytes(50_000))
        self.record(B + "garbage.flac", "broken", "broken", None, "garbage")
        shutil.copyfile(flac_src, self._dest(B + "actually_flac.mp3"))
        self.record(B + "actually_flac.mp3", "flac", "broken", ["Rock"], "misnamed")

    def build(self) -> dict:
        if self.lib.exists():
            raise FileExistsError(f"{self.lib} already exists; refusing to overwrite")
        try:
            self.make_bases()
            self.albums()
            self.formats()
            self.broken()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)
        on_disk = sorted(str(p.relative_to(self.lib)) for p in self.lib.rglob("*") if p.is_file())
        if on_disk != sorted(self.manifest):
            raise RuntimeError(f"manifest/disk mismatch: {set(on_disk) ^ set(self.manifest)}")
        (self.out / "manifest.json").write_text(json.dumps(self.manifest, indent=2, ensure_ascii=False))
        return self.manifest


def build(out_dir: Path) -> dict:
    bad = unsafe_output_dir(out_dir)
    if bad is not None:
        raise ValueError(f"refusing to build fixtures in {out_dir}: it is at or under library root {bad}")
    missing = tools_available()
    if missing:
        raise RuntimeError(f"missing tools: {missing}")
    return Builder(Path(out_dir)).build()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    m = build(Path(sys.argv[1]))
    print(f"{len(m)} files in {Path(sys.argv[1]) / 'library'}")
