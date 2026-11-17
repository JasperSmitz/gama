#!/usr/bin/env python3
"""Neutral oracle for Gama's genre edits.

Compares an edited copy (``work``) with the untouched original (``master``)
using only the Python standard library (raw tag/container parsers below) and
ffmpeg (audio checksums). It never imports mutagen, so it can catch mistakes
the library under test would not see in its own output.

``compare(master, work, mode, expected_genres) -> list[str]`` returns problems
(empty list = pass):

* ``mode="identical"``: ``work`` must be byte-identical with the same mode bits.
* ``mode="genres"``: only the stored genre values may differ, and they must
  equal ``expected_genres`` exactly (in order, one entry per Vorbis field /
  ID3v2.4 TCON value / MP4 ``data`` atom). ``expected_genres=None`` means the
  genres must equal the master's. If ``expected_genres`` differs from the
  master's genres, an unchanged file is a problem (a writer that skips files
  does not pass).

What a "genres" comparison checks, per format:

* all: file mode bits; container format; decoded-PCM and compressed-packet
  md5 of the first audio stream (ffmpeg);
* FLAC: prepended ID3 bytes, every metadata block except PADDING and
  VORBIS_COMMENT byte-for-byte (STREAMINFO, PICTURE incl. type/mime/desc,
  SEEKTABLE, ...), vendor string, non-genre comments in order (key case
  included), audio frames byte-identical;
* MP3: ID3v2 frame inventory except TCON, decoded (handles tag-level and
  per-frame unsynchronisation, data-length indicators, compression, and decodes
  each UTF-16 value on its own); the version may only go 2.3 -> 2.4, in which
  case TYER/TDAT/TIME may become TDRC with the same date; ID3v1 fields decoded
  and raw bytes; APEv2 items and raw bytes; the MPEG audio region
  byte-identical (covers the Xing/LAME frame);
* MP4: every ``ilst`` item except ``©gen``/``gnre`` decoded (incl. freeform
  ``----`` atoms and ``covr``), every non-``udta`` ``moov`` box except chunk
  offset tables byte-identical (covers edit lists and sample tables), ``mdat``
  payload byte-identical, fragmented-ness unchanged; a remaining ``gnre`` atom
  is reported as genre value ``"<gnre N>"``;
* Ogg Vorbis/Opus: page CRCs valid, identification/setup packets identical,
  vendor, non-genre comments in order, genres as separate ``GENRE`` fields,
  audio packets identical.

``compare_tree(master_root, work_root, expected)`` applies ``compare`` to a
whole library: files listed in ``expected`` (relpath -> genres) use "genres"
mode, all others must be identical; extra files (temp/backup leaks) and
missing files are problems.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import stat
import struct
import subprocess
import sys
import zlib
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

ID3_DATE = {"TYER", "TDAT", "TIME", "TDRC", "TORY", "TDOR", "TRDA"}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


# --------------------------------------------------------------------------- detection

def detect(data: bytes) -> str | None:
    """Container format by content: flac, mp3, mp4, ogg-vorbis, ogg-opus, wav, or None."""
    if data[:4] == b"fLaC":
        return "flac"
    if data[:3] == b"ID3" and len(data) >= 10:
        end = 10 + _syncsafe(data[6:10]) + (10 if data[5] & 0x10 else 0)
        if data[end:end + 4] == b"fLaC":
            return "flac"
        return "mp3"
    if data[4:8] == b"ftyp":
        return "mp4"
    if data[:4] == b"OggS" and len(data) > 40:
        body = data[27 + data[26]:]
        if body.startswith(b"OpusHead"):
            return "ogg-opus"
        if body.startswith(b"\x01vorbis"):
            return "ogg-vorbis"
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    return None


# --------------------------------------------------------------------------- ID3v2

def _syncsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _unsync(b: bytes) -> bytes:
    return b.replace(b"\xff\x00", b"\xff")


def id3v2_parse(data: bytes) -> dict | None:
    """Leading ID3v2 tag: {"version", "end", "frames": [(id, body)]} or None.

    Bodies are returned decoded: unsynchronisation removed, data-length
    indicator stripped, compressed frames inflated (encrypted frames raw).
    """
    if data[:3] != b"ID3" or len(data) < 10:
        return None
    ver, flags = data[3], data[5]
    size = _syncsafe(data[6:10])
    end = 10 + size + (10 if ver == 4 and flags & 0x10 else 0)
    tag = data[10:10 + size]
    if flags & 0x80 and ver in (2, 3):
        tag = _unsync(tag)
    pos = 0
    if flags & 0x40 and ver in (3, 4):  # extended header
        pos = _syncsafe(tag[0:4]) if ver == 4 else 4 + int.from_bytes(tag[0:4], "big")
    frames: list[tuple[str, bytes]] = []
    idlen, hdrlen = (3, 6) if ver == 2 else (4, 10)
    while pos + hdrlen <= len(tag):
        fid = tag[pos:pos + idlen]
        if not fid.strip(b"\0") or not all(48 <= c <= 57 or 65 <= c <= 90 for c in fid):
            break
        if ver == 2:
            fsize = int.from_bytes(tag[pos + 3:pos + 6], "big")
            fflags = 0
        elif ver == 4:
            fsize = _syncsafe(tag[pos + 4:pos + 8])
            fflags = int.from_bytes(tag[pos + 8:pos + 10], "big")
        else:
            fsize = int.from_bytes(tag[pos + 4:pos + 8], "big")
            fflags = int.from_bytes(tag[pos + 8:pos + 10], "big")
        body = tag[pos + hdrlen:pos + hdrlen + fsize]
        pos += hdrlen + fsize
        if ver == 4:
            if fflags & 0x0040:  # grouping identity byte
                body = body[1:]
            if fflags & 0x0002 or flags & 0x80:  # per-frame / tag-wide unsync
                body = _unsync(body)
            if fflags & 0x0001:  # data length indicator
                body = body[4:]
            if fflags & 0x0008 and not fflags & 0x0004:
                body = zlib.decompress(body)
        elif ver == 3:
            if fflags & 0x0080 and not fflags & 0x0040:  # compressed (4-byte size prefix)
                body = zlib.decompress(body[4:])
        frames.append((fid.decode("latin-1"), body))
    return {"version": ver, "end": end, "frames": frames}


def _split_enc(raw: bytes, enc: int) -> list[bytes]:
    """Split on the encoding's NUL terminator (2-byte aligned for UTF-16)."""
    if enc in (1, 2):
        parts, cur = [], bytearray()
        for i in range(0, len(raw) - len(raw) % 2, 2):
            pair = raw[i:i + 2]
            if pair == b"\0\0":
                parts.append(bytes(cur))
                cur = bytearray()
            else:
                cur += pair
        parts.append(bytes(cur))
        return parts
    return raw.split(b"\0")


def _dec(b: bytes, enc: int) -> str:
    if enc == 0:
        return b.decode("latin-1")
    if enc == 1:
        if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return b.decode("utf-16", "replace")
        return b.decode("utf-16-le", "replace")  # BOM-less continuation, seen in the wild
    if enc == 2:
        return b.decode("utf-16-be", "replace")
    return b.decode("utf-8", "replace")


def id3_text_values(body: bytes) -> list[str]:
    """Values of a text frame; each UTF-16 value is decoded on its own (own BOM)."""
    if not body:
        return []
    enc = body[0]
    parts = _split_enc(body[1:], enc)
    while parts and parts[-1] == b"":  # trailing terminator
        parts.pop()
    return [_dec(p, enc) for p in parts]


def id3_frame_view(fid: str, body: bytes) -> tuple:
    """Encoding-independent comparable view of one frame."""
    enc = body[0] if body else 0
    if fid in ("TXXX", "TXX", "WXXX", "WXX"):
        parts = _split_enc(body[1:], enc)
        desc = _dec(parts[0], enc) if parts else ""
        if fid.startswith("T"):
            rest = [_dec(p, enc) for p in parts[1:]]
            while rest and rest[-1] == "":
                rest.pop()
            return (fid, desc, tuple(rest))
        return (fid, desc, sha(body[1 + len(parts[0]) + (2 if enc in (1, 2) else 1):]))
    if fid.startswith("T"):
        return (fid, tuple(id3_text_values(body)))
    if fid in ("USLT", "COMM", "ULT", "COM"):
        lang = body[1:4].decode("latin-1", "replace")
        parts = _split_enc(body[4:], enc)
        dlen = len(parts[0]) + (2 if enc in (1, 2) else 1)
        text = _dec(body[4 + dlen:], enc).rstrip("\0")
        return (fid, lang, _dec(parts[0], enc), text)
    if fid == "APIC":
        mime_end = body.index(b"\0", 1)
        mime = body[1:mime_end].decode("latin-1")
        ptype = body[mime_end + 1]
        rest = body[mime_end + 2:]
        parts = _split_enc(rest, enc)
        dlen = len(parts[0]) + (2 if enc in (1, 2) else 1)
        return (fid, mime, ptype, _dec(parts[0], enc), sha(rest[dlen:]))
    # UFID, POPM, PRIV, GEOB, RVA2, MCDI, ...: raw body (optional trailing NULs ignored)
    return (fid, sha(body.rstrip(b"\0")))


def id3_genres(parsed: dict) -> list[str]:
    out: list[str] = []
    for fid, body in parsed["frames"]:
        if fid in ("TCON", "TCO"):
            out.extend(id3_text_values(body))
    return out


def id3_date_string(parsed: dict) -> str:
    """Date as YYYY[-MM-DD[THH:MM]] from TDRC or TYER/TDAT/TIME."""
    f = {fid: id3_text_values(b) for fid, b in parsed["frames"]}
    if "TDRC" in f and f["TDRC"]:
        return f["TDRC"][0]
    y = (f.get("TYER") or [""])[0]
    d = (f.get("TDAT") or [""])[0]
    t = (f.get("TIME") or [""])[0]
    s = y
    if y and len(d) == 4:
        s += f"-{d[2:4]}-{d[0:2]}"
        if len(t) == 4:
            s += f"T{t[0:2]}:{t[2:4]}"
    return s


# --------------------------------------------------------------------------- ID3v1 / APEv2

def id3v1_parse(data: bytes) -> dict | None:
    if len(data) < 128 or data[-128:-125] != b"TAG":
        return None
    t = data[-128:]

    def s(a: int, b: int) -> str:
        return t[a:b].split(b"\0")[0].decode("latin-1").rstrip(" ")
    track = t[126] if t[125] == 0 else None
    return {"title": s(3, 33), "artist": s(33, 63), "album": s(63, 93), "year": s(93, 97),
            "comment": s(97, 125 if track is not None else 127), "track": track, "genre": t[127], "raw": t}


def ape_parse(data: bytes) -> dict | None:
    """APEv2/v1 tag ending at EOF or just before an ID3v1 trailer."""
    end = len(data) - (128 if id3v1_parse(data) else 0)
    ftr = data[end - 32:end]
    if ftr[:8] != b"APETAGEX":
        return None
    _ver, size, count, flags = struct.unpack("<IIII", ftr[8:24])
    start = end - size - (32 if flags & 0x80000000 else 0)
    items_start = end - size
    if start < 0:
        return None
    items, pos = {}, items_start
    for _ in range(count):
        vlen, iflags = struct.unpack("<II", data[pos:pos + 8])
        kend = data.index(b"\0", pos + 8)
        key = data[pos + 8:kend].decode("ascii", "replace")
        val = data[kend + 1:kend + 1 + vlen]
        items[key.lower()] = (key, iflags, sha(val), val[:60].decode("utf-8", "replace"))
        pos = kend + 1 + vlen
    return {"start": start, "end": end, "raw": data[start:end], "items": items}


# --------------------------------------------------------------------------- FLAC

FLAC_BLOCK = {0: "STREAMINFO", 1: "PADDING", 2: "APPLICATION", 3: "SEEKTABLE",
              4: "VORBIS_COMMENT", 5: "CUESHEET", 6: "PICTURE"}


def vorbis_comment_parse(b: bytes) -> tuple[str, list[str]]:
    n = int.from_bytes(b[0:4], "little")
    vendor = b[4:4 + n].decode("utf-8", "replace")
    i = 4 + n
    count = int.from_bytes(b[i:i + 4], "little")
    i += 4
    out = []
    for _ in range(count):
        ln = int.from_bytes(b[i:i + 4], "little")
        out.append(b[i + 4:i + 4 + ln].decode("utf-8", "replace"))
        i += 4 + ln
    return vendor, out


def flac_picture_view(b: bytes) -> tuple:
    ptype, mlen = struct.unpack(">II", b[:8])
    mime = b[8:8 + mlen].decode("latin-1")
    i = 8 + mlen
    dlen = int.from_bytes(b[i:i + 4], "big")
    desc = b[i + 4:i + 4 + dlen].decode("utf-8", "replace")
    i += 4 + dlen
    w, h, depth, colors, ilen = struct.unpack(">IIIII", b[i:i + 20])
    return ("PICTURE", ptype, mime, desc, (w, h, depth, colors), sha(b[i + 20:i + 20 + ilen]))


def flac_parse(data: bytes) -> dict:
    prefix = b""
    if data[:3] == b"ID3":
        end = 10 + _syncsafe(data[6:10]) + (10 if data[5] & 0x10 else 0)
        prefix, data = data[:end], data[end:]
    if data[:4] != b"fLaC":
        raise ValueError("no fLaC marker")
    pos, blocks = 4, []
    while True:
        hdr = data[pos]
        btype, blen = hdr & 0x7F, int.from_bytes(data[pos + 1:pos + 4], "big")
        blocks.append((btype, data[pos + 4:pos + 4 + blen]))
        pos += 4 + blen
        if hdr & 0x80:
            break
        if pos >= len(data):
            raise ValueError("metadata blocks run past EOF")
    vendor, comments = "", []
    for t, b in blocks:
        if t == 4:
            vendor, comments = vorbis_comment_parse(b)
    return {"prefix": prefix, "blocks": blocks, "vendor": vendor, "comments": comments, "audio": data[pos:]}


def _is_genre(comment: str) -> bool:
    return comment.split("=", 1)[0].upper() == "GENRE"


def _genre_values(comments: list[str]) -> list[str]:
    return [c.split("=", 1)[1] for c in comments if _is_genre(c)]


# --------------------------------------------------------------------------- Ogg

def ogg_parse(data: bytes) -> dict:
    """Pages (with CRC check) and packets of the first logical stream."""
    from_crc = _ogg_crc
    pages, packets, cur, pos, bad_crc = [], [], b"", 0, 0
    serial = None
    while pos < len(data):
        if data[pos:pos + 4] != b"OggS":
            raise ValueError(f"lost Ogg sync at {pos}")
        nseg = data[pos + 26]
        lacing = data[pos + 27:pos + 27 + nseg]
        start = pos + 27 + nseg
        end = start + sum(lacing)
        page = data[pos:end]
        if from_crc(page[:22] + b"\0\0\0\0" + page[26:]) != int.from_bytes(page[22:26], "little"):
            bad_crc += 1
        ser = data[pos + 14:pos + 18]
        serial = serial or ser
        if ser == serial:
            pages.append({"granule": data[pos + 6:pos + 14], "flags": data[pos + 5]})
            off = start
            for lv in lacing:
                cur += data[off:off + lv]
                off += lv
                if lv < 255:
                    packets.append(cur)
                    cur = b""
        pos = end
    codec = "opus" if packets and packets[0].startswith(b"OpusHead") else "vorbis"
    nhdr = 2 if codec == "opus" else 3
    cpkt = packets[1]
    i = 8 if codec == "opus" else 7
    vendor, comments = vorbis_comment_parse(cpkt[i:])
    return {"codec": codec, "bad_crc": bad_crc, "packets": packets, "nhdr": nhdr,
            "vendor": vendor, "comments": comments, "last_granule": pages[-1]["granule"] if pages else b""}


def _crc_table() -> list[int]:
    tbl = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        tbl.append(r & 0xFFFFFFFF)
    return tbl


_TBL = _crc_table()


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _TBL[((crc >> 24) & 0xFF) ^ b]
    return crc


# --------------------------------------------------------------------------- MP4

MP4_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"mvex", b"udta"}


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
            raise ValueError(f"bad MP4 atom {typ!r} at offset {i}")
        yield typ, i, i + hdr, i + size
        i += size


def mp4_parse(data: bytes) -> dict:
    top = list(mp4_atoms(data, 0, len(data)))
    mdat = b"".join(data[ps:e] for t, s, ps, e in top if t == b"mdat")
    fragmented = any(t == b"moof" for t, *_ in top)
    boxes: list[tuple[str, str]] = []
    ilst: list[tuple[bytes, int, int]] = []

    def walk(start: int, end: int, path: str) -> None:
        nonlocal fragmented
        for t, s, ps, e in mp4_atoms(data, start, end):
            name = path + "/" + t.decode("latin-1")
            if t == b"mvex":
                fragmented = True
            if t == b"udta" and path == "moov":
                for t2, s2, ps2, e2 in mp4_atoms(data, ps, e):
                    if t2 == b"meta":
                        for t3, s3, ps3, e3 in mp4_atoms(data, ps2 + 4, e2):
                            if t3 == b"ilst":
                                ilst.append((t3, ps3, e3))
                            elif t3 not in (b"free", b"hdlr"):
                                boxes.append((name + "/meta/" + t3.decode("latin-1"), sha(data[s3:e3])))
                    elif t2 != b"free":
                        boxes.append((name + "/" + t2.decode("latin-1"), sha(data[s2:e2])))
                continue
            if t in MP4_CONTAINERS:
                walk(ps, e, name)
            elif t in (b"stco", b"co64", b"free", b"skip"):
                continue  # chunk offsets move legitimately when the tag grows
            else:
                boxes.append((name, sha(data[s:e])))

    moov = [a for a in top if a[0] == b"moov"]
    if not moov:
        raise ValueError("no moov atom")
    walk(moov[0][2], moov[0][3], "moov")
    items: list[tuple] = []
    genres: list[str] = []
    for _, ps, e in ilst:
        for t, s, ips, ie in mp4_atoms(data, ps, e):
            datas, mean, name = [], "", ""
            for t2, s2, ps2, e2 in mp4_atoms(data, ips, ie):
                if t2 == b"data":
                    datas.append((int.from_bytes(data[ps2:ps2 + 4], "big"), sha(data[ps2 + 8:e2]), data[ps2 + 8:e2]))
                elif t2 == b"mean":
                    mean = data[ps2 + 4:e2].decode("utf-8", "replace")
                elif t2 == b"name":
                    name = data[ps2 + 4:e2].decode("utf-8", "replace")
            if t == b"\xa9gen":
                genres.extend(d[2].decode("utf-8", "replace") for d in datas)
            elif t == b"gnre":
                genres.extend(f"<gnre {int.from_bytes(d[2], 'big')}>" for d in datas)
            else:
                key = t.decode("latin-1") + (f":{mean}:{name}" if t == b"----" else "")
                items.append((key, tuple((d[0], d[1]) for d in datas)))
    return {"mdat": mdat, "fragmented": fragmented, "boxes": boxes, "items": items, "genres": genres}


# --------------------------------------------------------------------------- generic

def raw_genres(path: Path) -> list[str] | None:
    """Stored genre values in order, or None when the format has no tag we read."""
    data = Path(path).read_bytes()
    fmt = detect(data)
    try:
        if fmt == "flac":
            return _genre_values(flac_parse(data)["comments"])
        if fmt == "mp3":
            p = id3v2_parse(data)
            return id3_genres(p) if p else []
        if fmt == "mp4":
            return mp4_parse(data)["genres"]
        if fmt in ("ogg-vorbis", "ogg-opus"):
            return _genre_values(ogg_parse(data)["comments"])
    except (ValueError, IndexError, struct.error, zlib.error):
        return None
    return None


def _ffmpeg_md5(path: Path, copy: bool) -> str | None:
    args = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0"]
    if copy:
        args += ["-c", "copy"]
    r = subprocess.run(args + ["-f", "md5", "-"], capture_output=True)
    out = r.stdout.decode().strip()
    return out if r.returncode == 0 and out.startswith("MD5=") else None


def _mpeg_audio_region(data: bytes) -> bytes:
    p = id3v2_parse(data)
    start = p["end"] if p else 0
    end = len(data)
    ape = ape_parse(data)
    if ape:
        end = ape["start"]
    elif id3v1_parse(data):
        end -= 128
    return data[start:end]


def _diff_lists(label: str, a: list, b: list) -> list[str]:
    if a == b:
        return []
    ca, cb = Counter(a), Counter(b)
    lost, added = list((ca - cb).elements()), list((cb - ca).elements())
    if not lost and not added:
        return [f"{label}: same entries, order changed"]
    return [f"{label}: lost {_short(lost)} added {_short(added)}"]


def _short(x: object) -> str:
    s = repr(x)
    return s if len(s) <= 160 else s[:157] + "..."


def _compare_flac(m: bytes, w: bytes) -> tuple[list[str], list[str]]:
    pm, pw = flac_parse(m), flac_parse(w)
    probs: list[str] = []
    if pm["prefix"] != pw["prefix"]:
        probs.append("prepended ID3 bytes changed")

    def view(blocks):
        out = []
        for t, b in blocks:
            if t in (1, 4):
                continue
            out.append(flac_picture_view(b) if t == 6 else (FLAC_BLOCK.get(t, f"BLOCK{t}"), sha(b)))
        return out
    probs += _diff_lists("FLAC metadata blocks (excl. padding/comments)", view(pm["blocks"]), view(pw["blocks"]))
    if sum(1 for t, _ in pw["blocks"] if t == 4) > 1:
        probs.append("more than one VORBIS_COMMENT block")
    if pm["vendor"] != pw["vendor"]:
        probs.append(f"Vorbis vendor changed: {pm['vendor']!r} -> {pw['vendor']!r}")
    probs += _diff_lists("non-genre Vorbis comments",
                         [c for c in pm["comments"] if not _is_genre(c)], [c for c in pw["comments"] if not _is_genre(c)])
    if pm["audio"] != pw["audio"]:
        probs.append("FLAC audio frames changed")
    return probs, _genre_values(pw["comments"])


def _compare_mp3(m: bytes, w: bytes, allow_id3v1_genre_change: bool) -> tuple[list[str], list[str]]:
    probs: list[str] = []
    im, iw = id3v2_parse(m), id3v2_parse(w)
    if iw is None:
        if im is not None:
            probs.append("ID3v2 tag removed")
        genres_w: list[str] = []
    else:
        genres_w = id3_genres(iw)
        vm = im["version"] if im else None
        if vm is not None and vm != iw["version"] and not (vm == 3 and iw["version"] == 4):
            probs.append(f"ID3v2 version changed 2.{vm} -> 2.{iw['version']} (only 2.3 -> 2.4 is allowed)")
        upgraded = vm == 3 and iw["version"] == 4
        excl = {"TCON", "TCO"} | (ID3_DATE if upgraded else set())

        def inv(p):
            return [id3_frame_view(fid, b) for fid, b in p["frames"] if fid not in excl] if p else []
        probs += _diff_lists("ID3v2 frames (excl. TCON)", sorted(inv(im), key=repr), sorted(inv(iw), key=repr))
        if upgraded and id3_date_string(im) != id3_date_string(iw):
            probs.append(f"ID3 date changed in 2.3->2.4 upgrade: {id3_date_string(im)!r} -> {id3_date_string(iw)!r}")
    v1m, v1w = id3v1_parse(m), id3v1_parse(w)
    if (v1m is None) != (v1w is None):
        probs.append(f"ID3v1 tag {'added' if v1m is None else 'removed'}")
    elif v1m and v1w:
        field_changed = False
        for k in ("title", "artist", "album", "year", "comment", "track", "genre"):
            if v1m[k] != v1w[k] and not (k == "genre" and allow_id3v1_genre_change):
                probs.append(f"ID3v1 {k} changed: {v1m[k]!r} -> {v1w[k]!r}")
                field_changed = True
        if not field_changed and v1m["raw"] != v1w["raw"] and not allow_id3v1_genre_change:
            probs.append("ID3v1 raw bytes changed")
    am, aw = ape_parse(m), ape_parse(w)
    if (am is None) != (aw is None):
        probs.append(f"APEv2 tag {'added' if am is None else 'removed'}")
    elif am and aw:
        for k in sorted(set(am["items"]) | set(aw["items"])):
            if am["items"].get(k) != aw["items"].get(k):
                probs.append(f"APEv2 item {k!r} changed: {am['items'].get(k)} -> {aw['items'].get(k)}")
        if am["raw"] != aw["raw"]:
            probs.append("APEv2 raw bytes changed")
    if _mpeg_audio_region(m) != _mpeg_audio_region(w):
        probs.append("MPEG audio region (incl. Xing/LAME frame) changed")
    return probs, genres_w


def _compare_mp4(m: bytes, w: bytes) -> tuple[list[str], list[str]]:
    pm, pw = mp4_parse(m), mp4_parse(w)
    probs: list[str] = []
    if pm["mdat"] != pw["mdat"]:
        probs.append("MP4 mdat payload changed")
    if pm["fragmented"] != pw["fragmented"]:
        probs.append("MP4 fragmentation changed")
    probs += _diff_lists("MP4 moov boxes (excl. udta, chunk offsets)", pm["boxes"], pw["boxes"])
    probs += _diff_lists("MP4 ilst items (excl. ©gen/gnre)", sorted(pm["items"]), sorted(pw["items"]))
    return probs, pw["genres"]


def _compare_ogg(m: bytes, w: bytes) -> tuple[list[str], list[str]]:
    pm, pw = ogg_parse(m), ogg_parse(w)
    probs: list[str] = []
    if pw["bad_crc"]:
        probs.append(f"{pw['bad_crc']} Ogg page(s) with a bad CRC")
    if pm["codec"] != pw["codec"]:
        probs.append("Ogg codec changed")
        return probs, []
    if pm["packets"][0] != pw["packets"][0]:
        probs.append("Ogg identification header changed")
    if pm["codec"] == "vorbis" and pm["packets"][2] != pw["packets"][2]:
        probs.append("Vorbis setup header changed")
    if pm["vendor"] != pw["vendor"]:
        probs.append(f"Ogg vendor changed: {pm['vendor']!r} -> {pw['vendor']!r}")
    probs += _diff_lists("non-genre Ogg comments",
                         [c for c in pm["comments"] if not _is_genre(c)], [c for c in pw["comments"] if not _is_genre(c)])
    am, aw = pm["packets"][pm["nhdr"]:], pw["packets"][pw["nhdr"]:]
    if am != aw:
        probs.append(f"Ogg audio packets changed ({len(am)} -> {len(aw)} packets)")
    if pm["last_granule"] != pw["last_granule"]:
        probs.append("Ogg final granule position changed")
    return probs, _genre_values(pw["comments"])


def compare(master: Path, work: Path, mode: str = "genres", expected_genres: list[str] | None = None,
            *, allow_id3v1_genre_change: bool = False, check_audio: bool = True) -> list[str]:
    """Problems found in ``work`` relative to ``master`` (empty list = pass)."""
    master, work = Path(master), Path(work)
    if mode not in ("genres", "identical"):
        raise ValueError(f"unknown mode {mode!r}")
    if not work.exists():
        return ["file missing"]
    probs: list[str] = []
    mm, mw = stat.S_IMODE(master.stat().st_mode), stat.S_IMODE(work.stat().st_mode)
    if mm != mw:
        probs.append(f"file mode changed: {oct(mm)} -> {oct(mw)}")
    m, w = master.read_bytes(), work.read_bytes()
    if mode == "identical":
        if m != w:
            probs.append("file content changed (expected byte-identical)")
        return probs
    fmt = detect(m)
    master_genres = raw_genres(master)
    expected = master_genres if expected_genres is None else list(expected_genres)
    if m == w:
        if expected != master_genres:
            probs.append(f"expected genres {expected} but file is byte-identical (genres {master_genres})")
        return probs
    if detect(w) != fmt:
        probs.append(f"container format changed: {fmt} -> {detect(w)}")
        return probs
    try:
        if fmt == "flac":
            p, got = _compare_flac(m, w)
        elif fmt == "mp3":
            p, got = _compare_mp3(m, w, allow_id3v1_genre_change)
        elif fmt == "mp4":
            p, got = _compare_mp4(m, w)
        elif fmt in ("ogg-vorbis", "ogg-opus"):
            p, got = _compare_ogg(m, w)
        else:
            return probs + [f"unsupported format {fmt} was modified"]
    except (ValueError, IndexError, struct.error, zlib.error) as e:
        return probs + [f"cannot parse modified file: {type(e).__name__}: {e}"]
    probs += p
    if got != expected:
        probs.append(f"stored genres {got} != expected {expected}")
    if check_audio:
        for label, copy in (("decoded audio (PCM md5)", False), ("compressed audio packets md5", True)):
            a, b = _ffmpeg_md5(master, copy), _ffmpeg_md5(work, copy)
            if b is None or a != b:
                probs.append(f"{label} differs: {a} -> {b}")
    return probs


def compare_tree(master_root: Path, work_root: Path, expected: Mapping[str, list[str] | None] | None = None,
                 **kw) -> dict[str, list[str]]:
    """Compare two library trees. Returns {relpath: problems} for failing paths only.

    Files named in ``expected`` (relpath -> genres, None = unchanged genres)
    are compared in "genres" mode; every other file must be identical. Extra
    files in ``work_root`` (leftover temp or backup files) are problems.
    """
    expected = dict(expected or {})
    master_root, work_root = Path(master_root), Path(work_root)

    def files(root: Path) -> set[str]:
        return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() or p.is_symlink()}
    mf, wf = files(master_root), files(work_root)
    out: dict[str, list[str]] = {}
    for rel in sorted(mf):
        if rel in expected:
            probs = compare(master_root / rel, work_root / rel, "genres", expected[rel], **kw)
        else:
            probs = compare(master_root / rel, work_root / rel, "identical")
        if probs:
            out[rel] = probs
    for rel in sorted(wf - mf):
        out[rel] = ["unexpected extra file (leftover temp/backup?)"]
    for rel in sorted(set(expected) - mf):
        out[rel] = ["expected file not present in master"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("master", type=Path)
    ap.add_argument("work", type=Path)
    ap.add_argument("--expect", type=Path, help="JSON {relpath: [genres] | null} of files allowed to change")
    a = ap.parse_args()
    expected = json.loads(a.expect.read_text()) if a.expect else {}
    res = compare_tree(a.master, a.work, expected)
    for rel, probs in res.items():
        print(f"FAIL {rel}")
        for p in probs:
            print(f"     - {p}")
    print(f"{len(res)} path(s) with problems")
    sys.exit(1 if res else 0)


if __name__ == "__main__":
    main()
