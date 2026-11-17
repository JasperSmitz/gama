"""MP4/M4A (iTunes-style ``ilst``).

Besides mutagen's parsed ``ilst`` items, the snapshot holds a raw view of the
container: every ``moov`` box outside ``udta/meta/ilst`` (chunk offset tables
normalised to relative offsets; ``free``/``skip`` padding ignored) and a hash
of the ``mdat`` payload, so a write that damages chapters, QuickTime ``udta``
atoms, sample tables or audio is visible without ffmpeg.
"""
from __future__ import annotations

import hashlib

from mutagen._constants import GENRES
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from gama.metadata import genres, snapshot
from gama.metadata.model import Picture, TrackTags

GEN = "\xa9gen"
FIELDS = {"title": "\xa9nam", "artist": "\xa9ART", "albumartist": "aART", "album": "\xa9alb",
          "date": "\xa9day", "mb_albumid": "----:com.apple.iTunes:MusicBrainz Album Id"}
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"mvex", b"udta"}
UNPARSED = "mp4-genre-unparsed"


def canon(v):
    """JSON-safe, comparable rendering of one MP4 tag value."""
    if isinstance(v, MP4Cover):
        return ["cover", int(v.imageformat), len(v), snapshot.sha256(bytes(v))]
    if isinstance(v, MP4FreeForm):
        return ["freeform", int(v.dataformat), int(getattr(v, "version", 0)), snapshot.sha256(bytes(v))]
    if isinstance(v, bytes):
        return ["bytes", snapshot.sha256(v)]
    if isinstance(v, str):
        return snapshot.text_value(v)
    if isinstance(v, tuple):
        return [canon(x) for x in v]
    if isinstance(v, (bool, int, float)):
        return v
    return ["repr", repr(v)]


def _pair(t) -> str | None:
    if not t:
        return None
    n, total = (tuple(t) + (0,))[:2]
    return f"{n}/{total}" if total else str(n)


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
            raise ValueError(f"bad MP4 atom {typ!r}")
        yield typ, i, i + hlen, i + size
        i += size


def _relative_offsets(body: bytes, width: int) -> bytes:
    """stco/co64 body with offsets made relative to the first chunk."""
    head, count = body[:8], int.from_bytes(body[4:8], "big")
    offs = [int.from_bytes(body[8 + k * width:8 + (k + 1) * width], "big") for k in range(count)]
    base = offs[0] if offs else 0
    return head + b"".join((o - base).to_bytes(8, "big", signed=True) for o in offs)


def structure(path: str) -> dict:
    """Raw container view: [[box path, sha256]] for moov outside ilst, plus mdat sha256."""
    boxes: list[list[str]] = []
    mdat = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        pos = 0
        while pos + 8 <= size:
            f.seek(pos)
            hdr = f.read(16)
            asize, typ, hlen = int.from_bytes(hdr[:4], "big"), hdr[4:8], 8
            if asize == 1:
                asize, hlen = int.from_bytes(hdr[8:16], "big"), 16
            elif asize == 0:
                asize = size - pos
            if asize < hlen or pos + asize > size:
                raise ValueError("MP4 atom runs past end of file")
            if typ == b"mdat":
                f.seek(pos + hlen)
                left = asize - hlen
                while left:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        raise ValueError("short read in mdat")
                    mdat.update(chunk)
                    left -= len(chunk)
            elif typ == b"moov":
                f.seek(pos)
                moov = f.read(asize)
                _walk(moov, hlen, len(moov), "moov", boxes)
            pos += asize
    return {"boxes": boxes, "mdat": mdat.hexdigest()}


def _walk(buf: bytes, start: int, end: int, path: str, out: list) -> None:
    for t, s, ps, e in _children(buf, start, end):
        if t in (b"free", b"skip"):
            continue
        name = f"{path}/{t.decode('latin-1')}"
        if t == b"meta" and path == "moov/udta":
            # ilst is covered by the parsed items; hdlr is created by writers when missing.
            for t2, s2, ps2, e2 in _children(buf, ps + 4, e):
                if t2 not in (b"ilst", b"hdlr", b"free", b"skip"):
                    out.append([f"{name}/{t2.decode('latin-1')}", snapshot.sha256(buf[s2:e2])])
        elif t in CONTAINERS:
            _walk(buf, ps, e, name, out)
        elif t in (b"stco", b"co64"):
            out.append([name, snapshot.sha256(_relative_offsets(buf[ps:e], 4 if t == b"stco" else 8))])
        else:
            out.append([name, snapshot.sha256(buf[s:e])])


def _genre_problem(failed: dict, facts: dict, values: list[str]) -> str | None:
    """Refuse a write when a genre atom exists that mutagen did not turn into values."""
    if GEN in failed or "gnre" in failed:
        for raw in failed.get(GEN, []):
            if _undecodable_text(raw):
                return "non-utf8-text"
        return UNPARSED
    if (facts.get("has_gnre") or facts.get("has_gen")) and not values:
        return UNPARSED
    # gnre holds an ID3 genre index + 1. mutagen maps 0 to GENRES[-1] (a genre the
    # file never had), so check the raw value range ourselves.
    if any(not 1 <= v <= len(GENRES) for v in facts.get("gnre_values", [])):
        return UNPARSED
    return None


def _undecodable_text(item: bytes) -> bool:
    try:
        for t, s, ps, e in _children(item, 0, len(item)):
            if t == b"data" and int.from_bytes(item[ps:ps + 4], "big") & 0xFFFFFF == 1:
                item[ps + 8:e].decode("utf-8")
    except UnicodeDecodeError:
        return True
    except ValueError:
        return False
    return False


def read(path: str, fmt: str, trailers: dict, facts: dict | None = None) -> TrackTags:
    f = MP4(path)
    tags = f.tags if f.tags is not None else {}
    facts = facts or {}
    # mutagen reads a valid numeric ``gnre`` atom as its name under ©gen.
    values = [str(v) for v in tags.get(GEN, [])]
    fields: dict[str, str | None] = {}
    for name, k in FIELDS.items():
        v = tags.get(k)
        if not v:
            fields[name] = None
        else:
            x = v[0]
            fields[name] = bytes(x).decode("utf-8", "replace") if isinstance(x, bytes) else str(x)
    fields["tracknumber"] = _pair(tags.get("trkn", [None])[0])
    fields["discnumber"] = _pair(tags.get("disk", [None])[0])
    pictures = [Picture(3, "image/png" if c.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg", "", len(c),
                        snapshot.sha256(bytes(c))) for c in tags.get("covr", [])]
    items = sorted([[k, [canon(x) for x in (v if isinstance(v, list) else [v])]]
                    for k, v in tags.items() if k not in (GEN, "gnre")], key=lambda i: i[0])
    # mutagen keeps atoms it cannot parse and writes them back byte-for-byte; record
    # their raw hashes so they are part of the snapshot too.
    failed = getattr(f.tags, "_failed_atoms", {}) if f.tags is not None else {}
    failed_part = sorted([k, snapshot.sha256(bytes(d))] for k, ds in failed.items() if k not in (GEN, "gnre")
                         for d in ds)
    native = {"has_gnre": bool(facts.get("has_gnre")), "has_gen": bool(facts.get("has_gen")),
              "text_problem": None}
    problem = _genre_problem(failed, facts, values)
    try:
        raw = structure(path)
    except ValueError as e:  # truncated or malformed container: readable, never writable
        raw = {"boxes": [], "mdat": None, "structure_error": str(e)}
        problem = problem or "mp4-truncated"
    native["text_problem"] = problem
    part = {"items": items, "failed_atoms": failed_part, **raw}
    return TrackTags(fmt, fields, genres.parse_field(values), pictures, native,
                     snapshot.build(fmt, f.info, part, pictures, trailers))
