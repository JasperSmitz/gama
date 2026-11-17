"""Magic-byte format detection. The mutagen class is chosen from the content,
never by ``mutagen.File`` scoring and never by the extension alone.

Raw container facts the policy needs (ID3 version, prepended ID3 on FLAC,
MP4 fragmentation/truncation, ``gnre`` atoms) are found here without mutagen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED = ("flac", "mp3", "mp4", "ogg-vorbis", "ogg-opus")

EXTENSIONS = {
    "flac": {".flac"},
    "mp3": {".mp3"},
    "mp4": {".m4a", ".m4b", ".mp4"},
    "ogg-vorbis": {".ogg", ".oga"},
    "ogg-opus": {".opus", ".ogg"},
}

HEAD = 64 * 1024


@dataclass
class Detection:
    format: str | None            # one of SUPPORTED, an unsupported name ("wav", ...), or None
    supported: bool = False
    extension_ok: bool = False
    id3v2_version: int | None = None   # leading ID3v2 tag major version (2, 3, 4)
    id3_prefix: bool = False           # FLAC preceded by an ID3v2 tag
    mp4: dict = field(default_factory=dict)  # fragmented, truncated, has_gnre, has_gen
    empty: bool = False


def _syncsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _is_mpeg_sync(b: bytes) -> bool:
    # 11 sync bits, a valid MPEG version and a layer that is not "reserved".
    return len(b) >= 2 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0 and (b[1] & 0x18) != 0x08 and (b[1] & 0x06) != 0


def sniff(head: bytes) -> tuple[str | None, int | None, bool]:
    """(format, id3v2 version, id3_prefix) from the first bytes of a file."""
    id3_ver = None
    body = head
    prefix = False
    if head[:3] == b"ID3" and len(head) >= 10:
        id3_ver = head[3]
        end = 10 + _syncsafe(head[6:10]) + (10 if head[5] & 0x10 else 0)
        body = head[end:]
        if body[:4] == b"fLaC":
            return "flac", id3_ver, True
        # ID3v2 followed by MPEG audio (possibly after padding we did not read)
        return "mp3", id3_ver, prefix
    if head[:4] == b"fLaC":
        return "flac", None, False
    if head[4:8] == b"ftyp":
        return "mp4", None, False
    if head[:4] == b"OggS" and len(head) >= 28:
        pkt = head[27 + head[26]:]
        if pkt.startswith(b"OpusHead"):
            return "ogg-opus", None, False
        if pkt.startswith(b"\x01vorbis"):
            return "ogg-vorbis", None, False
        return "ogg-other", None, False
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav", None, False
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "aiff", None, False
    if head[:4] == b"MAC ":
        return "ape", None, False
    if head[:4] == b"wvpk":
        return "wavpack", None, False
    if head[:16] == bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c"):
        return "asf", None, False
    if head[:4] == b"DSD ":
        return "dsf", None, False
    if _is_mpeg_sync(head):
        return "mp3", None, False
    return None, None, False


def _mp4_facts(f, size: int) -> dict:
    """Top-level atom walk: fragmentation, truncation, gnre/©gen presence."""
    facts = {"fragmented": False, "truncated": False, "has_gnre": False, "has_gen": False, "moov": False}
    pos = 0
    moov = None
    while pos + 8 <= size:
        f.seek(pos)
        hdr = f.read(16)
        asize = int.from_bytes(hdr[0:4], "big")
        typ = hdr[4:8]
        hlen = 8
        if asize == 1:
            asize, hlen = int.from_bytes(hdr[8:16], "big"), 16
        elif asize == 0:
            asize = size - pos
        if asize < hlen:
            facts["truncated"] = True
            break
        if pos + asize > size:
            facts["truncated"] = True  # an atom claims more bytes than the file has
            break
        if typ == b"moof":
            facts["fragmented"] = True
        if typ == b"moov":
            facts["moov"] = True
            f.seek(pos + hlen)
            moov = f.read(asize - hlen)
        pos += asize
    if pos < size:
        facts["truncated"] = True  # trailing bytes too short to be an atom
    if moov is not None:
        _scan_moov(moov, facts)
    return facts


def _children(buf: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        size = int.from_bytes(buf[i:i + 4], "big")
        typ = buf[i + 4:i + 8]
        hlen = 8
        if size == 1:
            size, hlen = int.from_bytes(buf[i + 8:i + 16], "big"), 16
        elif size == 0:
            size = end - i
        if size < hlen or i + size > end:
            raise ValueError("bad atom")
        yield typ, i + hlen, i + size
        i += size


def _scan_moov(moov: bytes, facts: dict) -> None:
    try:
        for t, s, e in _children(moov, 0, len(moov)):
            if t == b"mvex":
                facts["fragmented"] = True
            if t == b"udta":
                for t2, s2, e2 in _children(moov, s, e):
                    if t2 == b"meta":
                        for t3, s3, e3 in _children(moov, s2 + 4, e2):
                            if t3 == b"ilst":
                                for t4, _, _ in _children(moov, s3, e3):
                                    if t4 == b"gnre":
                                        facts["has_gnre"] = True
                                    if t4 == b"\xa9gen":
                                        facts["has_gen"] = True
    except ValueError:
        facts["truncated"] = True


def detect(path: str | Path) -> Detection:
    """Detect the format of ``path`` from its content. Raises OSError only."""
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(HEAD)
        f.seek(0, 2)
        size = f.tell()
        if size == 0:
            return Detection(None, empty=True)
        fmt, id3_ver, prefix = sniff(head)
        if id3_ver is not None and len(head) >= 10:
            # The ID3 tag may be larger than HEAD (big pictures): look past it.
            end = 10 + _syncsafe(head[6:10]) + (10 if head[5] & 0x10 else 0)
            f.seek(end)
            if f.read(4) == b"fLaC":
                fmt, prefix = "flac", True
        det = Detection(fmt, supported=fmt in SUPPORTED, id3v2_version=id3_ver, id3_prefix=prefix)
        if fmt == "mp4":
            det.mp4 = _mp4_facts(f, size)
    det.extension_ok = fmt in EXTENSIONS and path.suffix.lower() in EXTENSIONS[fmt]
    return det
