"""Canonical non-genre snapshot helpers and raw ID3v1/APEv2 extraction.

The format modules build the tag part of a snapshot; this module adds the
stream info and the raw trailer hashes, and provides the canonicalisation
helpers so every format renders values the same JSON-safe way.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

LONG_VALUE = 200  # longer text values are stored as a hash to keep snapshots small


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def text_value(v: str) -> str:
    return v if len(v) <= LONG_VALUE else f"sha256:{sha256(v.encode('utf-8', 'surrogatepass'))}"


def read_trailers(path: str | Path) -> dict:
    """Raw ID3v1 trailer and APEv2 tag at the end of a file (bytes or None).

    Returns {"id3v1": bytes|None, "apev2": bytes|None, "ape_keys": [str]}.
    The APEv2 tag may sit at EOF or directly before an ID3v1 trailer.
    """
    out: dict = {"id3v1": None, "apev2": None, "ape_keys": []}
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        end = size
        if size >= 128:
            f.seek(size - 128)
            t = f.read(128)
            if t[:3] == b"TAG":
                out["id3v1"] = t
                end = size - 128
        if end >= 32:
            f.seek(end - 32)
            ftr = f.read(32)
            if ftr[:8] == b"APETAGEX":
                _ver, tsize, count, flags = struct.unpack("<IIII", ftr[8:24])
                start = end - tsize - (32 if flags & 0x80000000 else 0)
                if 0 <= start < end and tsize >= 32:
                    f.seek(start)
                    raw = f.read(end - start)
                    out["apev2"] = raw
                    out["ape_keys"] = _ape_keys(raw, flags, count)
    return out


def _ape_keys(raw: bytes, flags: int, count: int) -> list[str]:
    pos = 32 if flags & 0x80000000 else 0
    keys = []
    try:
        for _ in range(count):
            vlen = int.from_bytes(raw[pos:pos + 4], "little")
            kend = raw.index(b"\0", pos + 8)
            keys.append(raw[pos + 8:kend].decode("ascii", "replace"))
            pos = kend + 1 + vlen
    except ValueError:
        pass
    return keys


def stream_info(info) -> dict:
    """Comparable stream properties from a mutagen ``info`` object."""
    return {
        "length": round(float(getattr(info, "length", 0.0) or 0.0), 6),
        "sample_rate": int(getattr(info, "sample_rate", 0) or 0),
        "channels": int(getattr(info, "channels", 0) or 0),
        "bitrate": int(getattr(info, "bitrate", 0) or 0),
    }


def build(fmt: str, info, tags_part: dict, pictures: list, trailers: dict) -> dict:
    return {
        "format": fmt,
        "stream": stream_info(info),
        "tags": tags_part,
        "pictures": [[p.type, p.mime, p.desc, p.size, p.sha256] for p in pictures],
        "raw": {
            "id3v1": sha256(trailers["id3v1"]) if trailers["id3v1"] else None,
            "apev2": sha256(trailers["apev2"]) if trailers["apev2"] else None,
        },
    }
