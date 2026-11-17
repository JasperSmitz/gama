"""Raw checks for text that mutagen would re-serialize lossily.

mutagen decodes Vorbis comments with ``errors="replace"`` and silently drops
comments whose key is invalid, and it silently drops ID3 frames it cannot
parse. Saving such a file would rewrite or lose bytes the user never asked to
change, so the policy refuses to write it. These checks read the raw bytes
(no mutagen) and return a skip code or None.
"""
from __future__ import annotations

from collections.abc import Callable

NON_UTF8 = "non-utf8-text"
LOSSY_COMMENTS = "lossy-comments"
ID3_DROPPED = "id3-frames-dropped"


def _valid_vorbis_key(k: bytes) -> bool:
    return bool(k) and all(0x20 <= c <= 0x7D and c != 0x3D for c in k)


def comment_tail(body: bytes) -> bytes | None:
    """Bytes after the comment list (Vorbis framing byte + padding, Opus padding/extra data).

    None if the structure cannot be walked."""
    try:
        n = int.from_bytes(body[0:4], "little")
        i = 4 + n
        count = int.from_bytes(body[i:i + 4], "little")
        i += 4
        for _ in range(count):
            i += 4 + int.from_bytes(body[i:i + 4], "little")
    except (IndexError, ValueError):
        return None
    return body[i:] if i <= len(body) else None


def tail_problem(fmt: str, tail: bytes) -> str | None:
    """lossy-comments if mutagen would not write ``tail`` back unchanged.

    mutagen writes: FLAC - nothing after the list; Ogg Vorbis - the framing byte
    plus zero padding (we keep its length); Opus - zero padding (length kept), or
    the original extra data when its first byte has the low bit set.
    """
    if fmt == "flac":
        ok = tail == b""
    elif fmt == "ogg-vorbis":
        ok = tail[:1] == b"\x01" and not tail[1:].strip(b"\x00")
    else:
        ok = not tail.strip(b"\x00") or bool(tail[0] & 1)
    return None if ok else LOSSY_COMMENTS


def vorbis_comment_problem(body: bytes, fmt: str | None = None) -> str | None:
    """Check a raw Vorbis comment structure (vendor + list, no packet prefix)."""
    try:
        n = int.from_bytes(body[0:4], "little")
        if 4 + n > len(body):
            return LOSSY_COMMENTS
        body[4:4 + n].decode("utf-8")
        i = 4 + n
        count = int.from_bytes(body[i:i + 4], "little")
        i += 4
        for _ in range(count):
            ln = int.from_bytes(body[i:i + 4], "little")
            raw = body[i + 4:i + 4 + ln]
            if len(raw) != ln:
                return LOSSY_COMMENTS
            i += 4 + ln
            raw.decode("utf-8")
            if b"=" not in raw or not _valid_vorbis_key(raw.split(b"=", 1)[0]):
                return LOSSY_COMMENTS
    except UnicodeDecodeError:
        return NON_UTF8
    return tail_problem(fmt, body[i:]) if fmt else None


def flac_comment_block(path: str) -> bytes | None:
    with open(path, "rb") as f:
        head = f.read(10)
        off = 0
        if head[:3] == b"ID3":
            off = 10 + ((head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]) + (10 if head[5] & 0x10 else 0)
        f.seek(off)
        if f.read(4) != b"fLaC":
            return None
        while True:
            hdr = f.read(4)
            if len(hdr) < 4:
                return None
            btype, blen = hdr[0] & 0x7F, int.from_bytes(hdr[1:4], "big")
            if btype == 4:
                return f.read(blen)
            f.seek(blen, 1)
            if hdr[0] & 0x80:
                return None


def ogg_comment_body(path: str) -> bytes | None:
    """Raw comment structure from the second packet of the first Ogg stream."""
    packets: list[bytes] = []
    cur = b""
    serial = None
    with open(path, "rb") as f:
        while len(packets) < 2:
            hdr = f.read(27)
            if len(hdr) < 27 or hdr[:4] != b"OggS":
                return None
            lacing = f.read(hdr[26])
            body = f.read(sum(lacing))
            serial = serial or hdr[14:18]
            if hdr[14:18] != serial:
                continue
            off = 0
            for lv in lacing:
                cur += body[off:off + lv]
                off += lv
                if lv < 255:
                    packets.append(cur)
                    cur = b""
    pkt = packets[1]
    if pkt.startswith(b"OpusTags"):
        return pkt[8:]
    if pkt.startswith(b"\x03vorbis"):
        return pkt[7:]
    return None


def _decode_strict(raw: bytes, enc: int) -> None:
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(enc)
    if codec is None:
        raise UnicodeDecodeError("id3", raw, 0, 1, f"unknown text encoding {enc}")
    if enc in (1, 2) and len(raw) % 2:
        raise UnicodeDecodeError(codec, raw, 0, 1, "odd length")
    raw.decode(codec)


ID3_FOOTER = "id3-footer"
ID3_EXT_HEADER = "id3-extended-header"
ID3_FRAME_FLAGS = "id3-frame-flags"


def _walk_id3(path: str, plain_sizes: Callable[[bytes], bool] | None):
    """(version, tag flags, ext header bytes, [(id, frame flags, raw body)]) or None."""
    with open(path, "rb") as f:
        head = f.read(10)
        if head[:3] != b"ID3" or head[3] not in (3, 4):
            return None
        size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
        tag = f.read(size)
    ver, flags = head[3], head[5]
    if flags & 0x80 and ver == 3:
        tag = tag.replace(b"\xff\x00", b"\xff")
    pos = 0
    ext = b""
    if flags & 0x40:
        pos = ((tag[0] << 21) | (tag[1] << 14) | (tag[2] << 7) | tag[3]) if ver == 4 else 4 + int.from_bytes(tag[0:4], "big")
        ext = tag[:pos]
    # iTunes wrote v2.4 frame sizes as plain integers; mutagen decides per tag
    # (determine_bpi) and the caller passes that decision in, so both walks split
    # the frames identically.
    plain = ver == 3 or (plain_sizes is not None and plain_sizes(tag[pos:]))
    frames = []
    while pos + 10 <= len(tag):
        fid = tag[pos:pos + 4]
        if fid[0] == 0:
            break  # padding (frames hidden after padding are an accepted limitation, see README)
        b = tag[pos + 4:pos + 8]
        fsize = int.from_bytes(b, "big") if plain else ((b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3])
        fflags = int.from_bytes(tag[pos + 8:pos + 10], "big")
        frames.append((fid.decode("latin-1"), fflags, tag[pos + 10:pos + 10 + fsize]))
        pos += 10 + fsize
    return ver, flags, ext, frames


def id3_raw_frames(path: str, plain_sizes: Callable[[bytes], bool] | None = None
                   ) -> list[tuple[str, bytes]] | None:
    """(frame id, raw body) of an ID3v2.3/2.4 tag; None if absent or v2.2.

    Every non-padding header counts, even an invalid ID or an empty body:
    mutagen drops those, and the caller's count comparison must notice.
    Bodies of v2.4 frames with grouping/compression/encryption/unsync/data-length
    flags are returned empty (not unpacked); the text check skips them.
    """
    w = _walk_id3(path, plain_sizes)
    if w is None:
        return None
    ver, _, _, frames = w
    return [(fid, body if not (ver == 4 and fflags & 0x004F) else b"") for fid, fflags, body in frames]


def id3_tag_problem(path: str, plain_sizes: Callable[[bytes], bool] | None = None) -> str | None:
    """Tag features mutagen silently drops on save.

    * v2.4 footer: mutagen replaces header + body and leaves the old 10-byte
      footer in front of the audio;
    * extended header with CRC or tag restrictions (mutagen writes none);
    * frame status flags (tag/file alter preservation, read-only), reset to 0.
    """
    w = _walk_id3(path, plain_sizes)
    if w is None:
        return None
    ver, flags, ext, frames = w
    if ver == 4 and flags & 0x10:
        return ID3_FOOTER
    if ext:
        if ver == 4 and len(ext) >= 6 and ext[5] & 0x30:
            return ID3_EXT_HEADER
        if ver == 3 and len(ext) >= 6 and int.from_bytes(ext[4:6], "big") & 0x8000:
            return ID3_EXT_HEADER
    status_mask = 0x7000 if ver == 4 else 0xE000
    if any(fflags & status_mask for _, fflags, _ in frames):
        return ID3_FRAME_FLAGS
    return None


def id3_text_problem(frames: list[tuple[str, bytes]]) -> str | None:
    """non-utf8-text if any text-bearing frame does not decode in its declared encoding."""
    for fid, body in frames:
        if not body or not (fid.startswith("T") or fid in ("COMM", "USLT")):
            continue
        enc = body[0]
        payload = body[1:] if fid.startswith("T") else body[4:]
        try:
            if enc in (1, 2):
                if len(payload) % 2:
                    return NON_UTF8  # a stray byte mutagen would not write back
                # Each NUL-terminated UTF-16 value decodes on its own.
                parts, cur = [], bytearray()
                for k in range(0, len(payload) - len(payload) % 2, 2):
                    pair = payload[k:k + 2]
                    if pair == b"\0\0":
                        parts.append(bytes(cur))
                        cur = bytearray()
                    else:
                        cur += pair
                parts.append(bytes(cur))
                for p in parts:
                    if p:
                        p.decode("utf-16" if enc == 1 and p[:2] in (b"\xff\xfe", b"\xfe\xff") else
                                 "utf-16-le" if enc == 1 else "utf-16-be")
            else:
                _decode_strict(payload, enc)
        except (UnicodeDecodeError, ValueError):
            return NON_UTF8
    return None
