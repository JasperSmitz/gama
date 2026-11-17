"""MP4/M4A (iTunes-style ``ilst``)."""
from __future__ import annotations

from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from gama.metadata import genres, snapshot
from gama.metadata.model import Picture, TrackTags

GEN = "\xa9gen"
FIELDS = {"title": "\xa9nam", "artist": "\xa9ART", "albumartist": "aART", "album": "\xa9alb",
          "date": "\xa9day", "mb_albumid": "----:com.apple.iTunes:MusicBrainz Album Id"}


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


def read(path: str, fmt: str, trailers: dict, facts: dict | None = None) -> TrackTags:
    f = MP4(path)
    tags = f.tags if f.tags is not None else {}
    # mutagen reads a numeric ``gnre`` atom as its name under ©gen.
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
    facts = facts or {}
    native = {"has_gnre": bool(facts.get("has_gnre")), "has_gen": bool(facts.get("has_gen"))}
    return TrackTags(fmt, fields, genres.parse_field(values), pictures, native,
                     snapshot.build(fmt, f.info, {"items": items}, pictures, trailers))
