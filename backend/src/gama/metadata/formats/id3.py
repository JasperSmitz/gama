"""MP3 with ID3v2. ID3v1 is never loaded (``load_v1=False``).

The tag is read **as stored** (``translate=False``) and the snapshot is built
from that view, never from mutagen's v2.4 translation, so frames an upgrade
would drop stay visible. Raw checks (``textcheck``) catch frames mutagen drops
or merges on load. A v2.3 tag is writable only if mutagen's ``update_to_v24``
provably loses nothing (simulated here at read time).
"""
from __future__ import annotations

import re
from collections import Counter

from mutagen.id3 import ID3, TCON
from mutagen.id3._tags import save_frame  # pinned mutagen; canonical frame bytes
from mutagen.id3._util import ID3SaveConfig
from mutagen.mp3 import MP3

from gama.metadata import genres, snapshot, textcheck
from gama.metadata.model import Picture, TrackTags

FIELDS = {
    "title": "TIT2", "artist": "TPE1", "albumartist": "TPE2", "album": "TALB",
    "discnumber": "TPOS", "tracknumber": "TRCK", "date": "TDRC", "mb_albumid": "TXXX:MusicBrainz Album Id",
}
_CANON = ID3SaveConfig(4, "/")

# Skip codes set here (see policy.SKIP).
UPGRADE_LOSSY = "id3v2.3-lossy-frames"
UNKNOWN_V23 = "id3v2.3-unknown-frames"


def resolve_reference(value: str) -> str:
    """Display name of one stored TCON value ("(17)" -> "Rock"), via mutagen's TCON.genres.

    A value that expands to several genres (e.g. "(17)(18)") is shown as stored.
    """
    try:
        g = TCON(encoding=3, text=[value]).genres
    except Exception:  # noqa: BLE001
        return value
    return g[0] if len(g) == 1 else value


def _canon(fr) -> str:
    return snapshot.sha256(save_frame(fr, config=_CANON))


def _single(tags, fid: str, pattern: str) -> bool:
    frs = tags.getall(fid)
    if not frs:
        return True
    return len(frs) == 1 and len(frs[0].text) == 1 and re.fullmatch(pattern, str(frs[0].text[0])) is not None


def upgrade_losses(path: str, loaded) -> list[str]:
    """HashKeys of frames that ``update_to_v24`` would drop or alter.

    Allowed conversions, each only when provably exact:
    TYER(+TDAT(+TIME)) -> TDRC when no TDRC exists and every value matches
    the formats mutagen converts; TORY -> TDOR (no TDOR yet, 4-digit year);
    IPLS -> TIPL (no TIPL yet). Everything else must survive byte-identically.
    """
    before = {fr.HashKey: _canon(fr) for fr in loaded.values() if fr.FrameID != "TCON"}
    up = ID3(path, load_v1=False, translate=False)
    up.update_to_v24()
    after = {fr.HashKey: _canon(fr) for fr in up.values() if fr.FrameID != "TCON"}

    has = {k[:4] for k in before}
    date_ok = ("TDRC" not in has and _single(loaded, "TYER", r"[0-9]{4}(-[0-9]{2}-[0-9]{2})?")
               and _single(loaded, "TDAT", r"[0-9]{4}") and _single(loaded, "TIME", r"[0-9]{4}")
               and not ("TDAT" in has and "TYER" not in has) and not ("TIME" in has and "TDAT" not in has))
    allowed_gone = set()
    allowed_new = set()
    if date_ok and has & {"TYER", "TDAT", "TIME"}:
        allowed_gone |= {"TYER", "TDAT", "TIME"}
        allowed_new.add("TDRC")
    if "TORY" in has and "TDOR" not in has and _single(loaded, "TORY", r"[0-9]{4}"):
        allowed_gone.add("TORY")
        allowed_new.add("TDOR")
    if "IPLS" in has and "TIPL" not in has:
        allowed_gone.add("IPLS")
        allowed_new.add("TIPL")

    lost = [k for k, h in before.items() if after.get(k) != h and k[:4] not in allowed_gone]
    lost += [k for k in after if k not in before and k[:4] not in allowed_new]
    return sorted(lost)


def read(path: str, fmt: str, trailers: dict) -> TrackTags:
    # translate=False: every frame exactly as stored (numeric TCON references kept,
    # v2.3 frames not converted).
    f = MP3(path, load_v1=False, translate=False)
    tags = f.tags
    version = f"2.{tags.version[1]}" if tags is not None else None
    values = list(tags["TCON"].text) if tags is not None and "TCON" in tags else []
    field = genres.parse_field([str(v) for v in values], resolve=resolve_reference)

    problem = None
    lossy: list[str] = []
    raw_ids: list[str] = []
    frames, unknown, pictures = [], [], []
    fields: dict[str, str | None] = {k: None for k in FIELDS}
    if tags is not None:
        unknown_raw = list(getattr(tags, "unknown_frames", []))
        raw_frames = textcheck.id3_raw_frames(path)
        if raw_frames is not None:
            raw_ids = sorted(fid for fid, _ in raw_frames if fid != "TCON")
            problem = textcheck.id3_text_problem(raw_frames)
            parsed = Counter(fr.FrameID for fr in tags.values())
            parsed.update(bytes(u[:4]).decode("latin-1") for u in unknown_raw)
            if problem is None and parsed != Counter(fid for fid, _ in raw_frames):
                problem = textcheck.ID3_DROPPED  # junk, empty, invalid-ID or merged (#754) frames
        if problem is None and version == "2.3":
            if unknown_raw:
                problem = UNKNOWN_V23  # mutagen writes unknown frames back only in their own version
            else:
                lossy = upgrade_losses(path, tags)
                if lossy:
                    problem = UPGRADE_LOSSY

        for fr in tags.values():
            if fr.FrameID != "TCON":
                frames.append([fr.HashKey, _canon(fr)])
        frames.sort()
        unknown = sorted(snapshot.sha256(bytes(u)) for u in unknown_raw)
        for name, k in FIELDS.items():
            if k in tags and getattr(tags[k], "text", None):
                fields[name] = str(tags[k].text[0])
        if fields["date"] is None and "TYER" in tags and tags["TYER"].text:
            fields["date"] = str(tags["TYER"].text[0])
        for fr in tags.getall("APIC"):
            pictures.append(Picture(int(fr.type), fr.mime, fr.desc, len(fr.data), snapshot.sha256(fr.data)))
    # The snapshot is the tag as stored. A 2.3 -> 2.4 upgrade changes id3_version,
    # raw_frame_ids and the converted frames on purpose; the verifier (M4) must map
    # exactly the conversions upgrade_losses() allows, nothing else.
    part = {"id3_version": version, "frames": frames, "unknown_frames": unknown, "raw_frame_ids": raw_ids}
    native = {
        "id3_version": version,
        "has_id3v1": trailers["id3v1"] is not None,
        "ape_keys": trailers["ape_keys"],
        "text_problem": problem,
        "upgrade_lossy_frames": lossy,
        "numeric_references": list(field.components) != list(field.values) and field.style == "native",
    }
    return TrackTags(fmt, fields, field, pictures, native, snapshot.build(fmt, f.info, part, pictures, trailers))
