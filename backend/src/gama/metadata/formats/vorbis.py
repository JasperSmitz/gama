"""FLAC, Ogg Vorbis and Opus (Vorbis comments)."""
from __future__ import annotations

import base64

from mutagen.flac import FLAC, Picture as FlacPicture
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from gama.metadata import genres, snapshot, textcheck
from gama.metadata.model import Picture, TrackTags

CLASSES = {"flac": FLAC, "ogg-vorbis": OggVorbis, "ogg-opus": OggOpus}

FIELDS = {
    "title": ("TITLE",), "artist": ("ARTIST",), "albumartist": ("ALBUMARTIST", "ALBUM ARTIST"),
    "album": ("ALBUM",), "discnumber": ("DISCNUMBER",), "tracknumber": ("TRACKNUMBER",),
    "date": ("DATE", "YEAR"), "mb_albumid": ("MUSICBRAINZ_ALBUMID",),
}
GENRE = "GENRE"


def _picture(p: FlacPicture) -> Picture:
    return Picture(int(p.type), p.mime, p.desc, len(p.data), snapshot.sha256(p.data))


def read(path: str, fmt: str, trailers: dict) -> TrackTags:
    f = CLASSES[fmt](path)
    comments: list[tuple[str, str]] = list(f.tags) if f.tags is not None else []
    values = [v for k, v in comments if k.upper() == GENRE]
    genre_keys: list[str] = []
    for k, _ in comments:
        if k.upper() == GENRE and k not in genre_keys:
            genre_keys.append(k)

    upper = {}
    for k, v in comments:
        upper.setdefault(k.upper(), v)
    fields = {name: next((upper[k] for k in keys if k in upper), None) for name, keys in FIELDS.items()}

    if fmt == "flac":
        pictures = [_picture(p) for p in f.pictures]
    else:
        pictures = []
        for k, v in comments:
            if k.upper() == "METADATA_BLOCK_PICTURE":
                try:
                    pictures.append(_picture(FlacPicture(base64.b64decode(v))))
                except Exception:  # noqa: BLE001 - a broken picture comment is still a comment
                    pass

    raw = textcheck.flac_comment_block(path) if fmt == "flac" else textcheck.ogg_comment_body(path)
    text_problem = textcheck.vorbis_comment_problem(raw, fmt) if raw is not None else None
    tail = textcheck.comment_tail(raw) if raw is not None else None
    part: dict = {
        "vendor": f.tags.vendor if f.tags is not None else None,
        "comments": [[k, snapshot.text_value(v)] for k, v in comments if k.upper() != GENRE],
    }
    # Raw bytes after the comment list (framing byte, padding): mutagen's parsed
    # view hides them, so they are hashed here.
    part["comment_tail"] = snapshot.sha256(tail) if tail is not None else None
    if fmt == "flac":
        # Every metadata block except PADDING (1) and VORBIS_COMMENT (4), in order.
        part["blocks"] = [[b.code, snapshot.sha256(b.write())] for b in f.metadata_blocks if b.code not in (1, 4)]
    native = {"genre_keys": genre_keys, "has_comment_block": f.tags is not None, "text_problem": text_problem,
              "genre_key_list": [k for k, _ in comments if k.upper() == GENRE]}
    return TrackTags(fmt, fields, genres.parse_field(values), pictures, native,
                     snapshot.build(fmt, f.info, part, pictures, trailers))


def apply_genres(path: str, fmt: str, values: list[str]) -> None:
    """Set the GENRE fields to exactly ``values`` (one field each; ``[]`` deletes them).

    Every other comment keeps its key spelling, value and position. The new
    genre fields take the place of the first existing one and reuse its key
    spelling; without one they are appended as ``GENRE``.
    """
    f = CLASSES[fmt](path)
    if f.tags is None:
        f.add_tags()
    entries = list(f.tags)
    first = next((i for i, (k, _) in enumerate(entries) if k.upper() == GENRE), None)
    key = entries[first][0] if first is not None else GENRE
    at = len(entries) if first is None else first
    before = [e for e in entries[:at] if e[0].upper() != GENRE]
    after = [e for e in entries[at:] if e[0].upper() != GENRE]
    new = before + [(key, v) for v in values] + after
    list.__delitem__(f.tags, slice(None))  # VComment is a list of (key, value); bypass dict-style overrides
    list.extend(f.tags, new)
    if fmt == "flac":
        f.save()  # padding lives in its own PADDING block
    else:
        # Keep the comment packet's zero padding at its original length, so the
        # bytes after the comment list do not change. (Opus extra data flagged
        # for preservation is kept by mutagen itself; _padding is then 0.)
        keep = int(getattr(f.tags, "_padding", 0) or 0)
        f.save(padding=lambda info: keep)
