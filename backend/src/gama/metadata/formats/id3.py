"""MP3 with ID3v2. ID3v1 is never loaded (``load_v1=False``)."""
from __future__ import annotations

from mutagen.id3 import TCON
from mutagen.id3._tags import save_frame  # pinned mutagen; canonical frame bytes
from mutagen.id3._util import ID3SaveConfig
from mutagen.mp3 import MP3

from gama.metadata import genres, snapshot
from gama.metadata.model import Picture, TrackTags

FIELDS = {
    "title": "TIT2", "artist": "TPE1", "albumartist": "TPE2", "album": "TALB",
    "discnumber": "TPOS", "tracknumber": "TRCK", "date": "TDRC", "mb_albumid": "TXXX:MusicBrainz Album Id",
}
_CANON = ID3SaveConfig(4, "/")


def resolve_reference(value: str) -> str:
    """Display name of one stored TCON value ("(17)" -> "Rock"), via mutagen's TCON.genres.

    A value that expands to several genres (e.g. "(17)(18)") is shown as stored.
    """
    try:
        g = TCON(encoding=3, text=[value]).genres
    except Exception:  # noqa: BLE001
        return value
    return g[0] if len(g) == 1 else value


def read(path: str, fmt: str, trailers: dict) -> TrackTags:
    # translate=False: TCON text exactly as stored (numeric references kept).
    f = MP3(path, load_v1=False, translate=False)
    tags = f.tags
    version = f"2.{tags.version[1]}" if tags is not None else None
    values = list(tags["TCON"].text) if tags is not None and "TCON" in tags else []
    field = genres.parse_field([str(v) for v in values], resolve=resolve_reference)

    frames, unknown, pictures = [], [], []
    fields: dict[str, str | None] = {k: None for k in FIELDS}
    if tags is not None:
        # The snapshot uses mutagen's v2.4 view, so a lossless 2.3 -> 2.4 upgrade
        # (TYER+TDAT -> TDRC) compares equal; TCON is excluded.
        tags.update_to_v24()
        for fr in tags.values():
            if fr.FrameID == "TCON":
                continue
            frames.append([fr.HashKey, snapshot.sha256(save_frame(fr, config=_CANON))])
        frames.sort()
        unknown = sorted(snapshot.sha256(bytes(u)) for u in getattr(tags, "unknown_frames", []))
        for name, k in FIELDS.items():
            if k in tags:
                fields[name] = str(tags[k].text[0]) if getattr(tags[k], "text", None) else None
        for fr in tags.getall("APIC"):
            pictures.append(Picture(int(fr.type), fr.mime, fr.desc, len(fr.data), snapshot.sha256(fr.data)))
    part = {"id3_version": version, "frames": frames, "unknown_frames": unknown}
    native = {
        "id3_version": version,
        "has_id3v1": trailers["id3v1"] is not None,
        "ape_keys": trailers["ape_keys"],
        "numeric_references": list(field.components) != list(field.values) and field.style == "native",
    }
    return TrackTags(fmt, fields, field, pictures, native, snapshot.build(fmt, f.info, part, pictures, trailers))
