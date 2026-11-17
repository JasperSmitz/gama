"""Plain data types of the metadata layer. No Django; everything is JSON-safe.

``to_dict``/``from_dict`` round-trip exactly, because results cross a process
boundary (``readproc``) and are stored as JSON later (plans, job items).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class FileStat:
    size: int
    mtime_ns: int
    inode: int
    nlink: int
    uid: int
    gid: int
    mode: int
    is_symlink: bool


@dataclass(frozen=True)
class GenreField:
    """The genre metadata of one file.

    values:     exactly as stored, in order (one entry per Vorbis field / TCON
                value / ©gen data atom).
    style:      "native" | "delimited" | "mixed" | "empty".
    delimiter:  the separator with its spacing (e.g. ";" or "; ") when
                style == "delimited", else None.
    components: what the user sees as "the genres". For ID3 numeric references
                ("(17)") this is the resolved name ("Rock").
    """
    values: tuple[str, ...]
    style: str
    delimiter: str | None
    components: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"values": list(self.values), "style": self.style, "delimiter": self.delimiter,
                "components": list(self.components)}

    @classmethod
    def from_dict(cls, d: dict) -> GenreField:
        return cls(tuple(d["values"]), d["style"], d["delimiter"], tuple(d["components"]))


@dataclass(frozen=True)
class Picture:
    type: int
    mime: str
    desc: str
    size: int
    sha256: str


@dataclass
class TrackTags:
    """Everything Gama reads from one supported file.

    fields:   common text fields for the index (title, artist, albumartist,
              album, discnumber, tracknumber, date, mb_albumid); None if absent.
    native:   format details the writer needs (e.g. Vorbis genre key spelling,
              ID3 version, whether an MP4 ``gnre`` atom exists).
    snapshot: canonical, JSON-safe view of everything except the genre, used to
              prove that a write changed nothing else.
    """
    format: str
    fields: dict[str, str | None]
    genre: GenreField
    pictures: list[Picture]
    native: dict
    snapshot: dict

    def to_dict(self) -> dict:
        return {"format": self.format, "fields": dict(self.fields), "genre": self.genre.to_dict(),
                "pictures": [asdict(p) for p in self.pictures], "native": self.native, "snapshot": self.snapshot}

    @classmethod
    def from_dict(cls, d: dict) -> TrackTags:
        return cls(d["format"], d["fields"], GenreField.from_dict(d["genre"]),
                   [Picture(**p) for p in d["pictures"]], d["native"], d["snapshot"])


@dataclass
class ReadResult:
    """Outcome of reading one file. ``read()`` never raises; failures land here.

    ok:          metadata was read (tags is set).
    reason:      machine code when not ok ("unsupported-format", "unreadable",
                 "empty-file", "missing", "not-regular-file", "timeout",
                 "reader-crashed").
    error:       human-readable detail (exception text) when not ok.
    skip_reason: policy code when the file must not be written (None = writable).
    warnings:    policy warning codes (see policy.WARNINGS).
    """
    path: str
    ok: bool
    format: str | None = None
    detected_by: str = "content"
    tags: TrackTags | None = None
    error: str | None = None
    reason: str | None = None
    skip_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    stat: FileStat | None = None

    @property
    def writable(self) -> bool:
        return self.ok and self.skip_reason is None

    @property
    def genre(self) -> GenreField | None:
        return self.tags.genre if self.tags else None

    def to_dict(self) -> dict:
        return {"path": self.path, "ok": self.ok, "format": self.format, "detected_by": self.detected_by,
                "tags": self.tags.to_dict() if self.tags else None, "error": self.error, "reason": self.reason,
                "skip_reason": self.skip_reason, "warnings": list(self.warnings),
                "stat": asdict(self.stat) if self.stat else None}

    @classmethod
    def from_dict(cls, d: dict) -> ReadResult:
        return cls(path=d["path"], ok=d["ok"], format=d["format"], detected_by=d["detected_by"],
                   tags=TrackTags.from_dict(d["tags"]) if d["tags"] else None, error=d["error"],
                   reason=d["reason"], skip_reason=d["skip_reason"], warnings=list(d["warnings"]),
                   stat=FileStat(**d["stat"]) if d["stat"] else None)
