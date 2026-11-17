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


def _mdat_relative(body: bytes, width: int, mdats: list[tuple[int, int]]) -> bytes:
    """stco/co64 body with every offset rewritten as (mdat index, offset into its payload).

    A table that was not updated after the tag grew points at the wrong bytes and
    so changes here; offsets outside every mdat are kept absolute and flagged.
    """
    count = int.from_bytes(body[4:8], "big")
    out = [body[:8]]
    for k in range(count):
        off = int.from_bytes(body[8 + k * width:8 + (k + 1) * width], "big")
        for i, (start, end) in enumerate(mdats):
            if start <= off < end:
                out.append(b"m" + i.to_bytes(4, "big") + (off - start).to_bytes(8, "big"))
                break
        else:
            out.append(b"x" + off.to_bytes(8, "big"))
    return b"".join(out)


def _top_atoms(f, size: int) -> list[tuple[bytes, int, int, int]]:
    atoms, pos = [], 0
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
        atoms.append((typ, pos, pos + hlen, pos + asize))
        pos += asize
    return atoms


def structure(path: str) -> dict:
    """Raw container view.

    boxes: [[box path, sha256]] for moov outside udta/meta/ilst, chunk offsets
           made relative to the mdat payload they point into;
    mdat:  sha256 of all mdat payloads;
    ilst:  the raw ilst child atoms (bytes) except ©gen/gnre, for the snapshot
           multiset and the read-time re-render check.
    """
    boxes: list[list[str]] = []
    ilst: list[bytes] = []
    mdat = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        atoms = _top_atoms(f, size)
        mdats = [(ps, e) for t, s, ps, e in atoms if t == b"mdat"]
        for t, s, ps, e in atoms:
            if t == b"mdat":
                f.seek(ps)
                left = e - ps
                while left:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        raise ValueError("short read in mdat")
                    mdat.update(chunk)
                    left -= len(chunk)
            elif t == b"moov":
                f.seek(s)
                moov = f.read(e - s)
                _walk(moov, ps - s, len(moov), "moov", boxes, ilst, mdats)
    return {"boxes": boxes, "mdat": mdat.hexdigest(), "ilst": ilst}


def _walk(buf: bytes, start: int, end: int, path: str, out: list, ilst: list, mdats: list) -> None:
    for t, s, ps, e in _children(buf, start, end):
        if t in (b"free", b"skip"):
            continue
        name = f"{path}/{t.decode('latin-1')}"
        if t == b"meta" and path == "moov/udta":
            # hdlr is created by writers when missing; ilst items are collected raw.
            for t2, s2, ps2, e2 in _children(buf, ps + 4, e):
                if t2 == b"ilst":
                    ilst.extend(buf[s3:e3] for t3, s3, _, e3 in _children(buf, ps2, e2)
                                if t3 not in (b"\xa9gen", b"gnre"))
                elif t2 not in (b"hdlr", b"free", b"skip"):
                    out.append([f"{name}/{t2.decode('latin-1')}", snapshot.sha256(buf[s2:e2])])
        elif t in CONTAINERS:
            _walk(buf, ps, e, name, out, ilst, mdats)
        elif t in (b"stco", b"co64"):
            out.append([name, snapshot.sha256(_mdat_relative(buf[ps:e], 4 if t == b"stco" else 8, mdats))])
        else:
            out.append([name, snapshot.sha256(buf[s:e])])


def _render_problem(tags, raw_items: list[bytes]) -> str | None:
    """mp4-items-normalised if mutagen would not write the non-genre items back byte-for-byte.

    mutagen re-renders every ilst item on save (unknown covr types become JPEG,
    locales are zeroed, duplicate atoms are merged, ...). Render them in memory
    the way MP4Tags.save does and compare with the raw atoms as a multiset.
    """
    from mutagen.mp4 import _key2name
    from mutagen.mp4._atom import Atom

    if tags is None:
        return None if not raw_items else "mp4-items-normalised"
    try:
        rendered = [bytes(tags._render(k, v)) for k, v in tags.items() if k not in (GEN, "gnre")]
        for k, failed in getattr(tags, "_failed_atoms", {}).items():
            if k in (GEN, "gnre") or k in tags:
                continue
            rendered += [bytes(Atom.render(_key2name(k), d)) for d in failed]
    except Exception:  # noqa: BLE001 - if mutagen cannot render it, it cannot save it either
        return "mp4-items-normalised"
    return None if sorted(rendered) == sorted(raw_items) else "mp4-items-normalised"


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
        raw = {"boxes": [], "mdat": None, "ilst": [], "structure_error": str(e)}
        problem = problem or "mp4-truncated"
    raw_items = raw.pop("ilst")
    # Raw item atoms as a multiset (mutagen re-sorts items on save): any re-render shows.
    raw["ilst_items"] = sorted(snapshot.sha256(b) for b in raw_items)
    problem = problem or _render_problem(f.tags, raw_items)
    native["text_problem"] = problem
    part = {"items": items, "failed_atoms": failed_part, **raw}
    return TrackTags(fmt, fields, genres.parse_field(values), pictures, native,
                     snapshot.build(fmt, f.info, part, pictures, trailers))


def apply_genres(path: str, values: list[str]) -> None:
    """Set ©gen to exactly ``values`` (one data atom each; ``[]`` deletes it).

    A numeric ``gnre`` atom is parsed by mutagen into ©gen and is not written
    back, so the result contains only ©gen (with the name). Policy refuses
    files whose gnre/©gen mutagen could not parse.
    """
    f = MP4(path)
    if f.tags is None:
        f.add_tags()
    if values:
        f.tags[GEN] = list(values)
    else:
        f.tags.pop(GEN, None)
    f.tags.pop("gnre", None)
    f.save()
