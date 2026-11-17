"""Change planning and preview (plan section 1.5). Pure Python; no Django.

``plan_files(files, operation, genres)`` reads every file fresh from disk (not
from the index), fingerprints it, applies the genre operation in memory and
returns an ``EditPlan``: one ``FilePlan`` per file with an action:

* ``change``   - the stored genre values would differ; will be written;
* ``nochange`` - the operation is a no-op for this file;
* ``skip``     - unreadable, unsupported or refused by policy (with a reason).

Nothing is ever written. The plan's ``digest`` covers every item, so the
confirm step can prove the user confirmed exactly what was shown.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from gama.metadata import genres as genres_mod
from gama.metadata.detect import EXTENSIONS
from gama.metadata.model import ReadResult

MAX_FILES = 2000  # Q9: preview hashes every file inside the web request
TEMP_PREFIX = ".gama-tmp-"

# Audio extensions used for the "files the index does not know about" check;
# includes formats Gama cannot write, since those still belong to the album.
AUDIO_EXTENSIONS = set().union(*EXTENSIONS.values()) | {
    ".wav", ".aif", ".aiff", ".ape", ".wv", ".wma", ".dsf", ".dff", ".mpc", ".tta", ".alac", ".aac", ".m4p"}

# Notes from the genre operation, as warnings shown in the preview.
NOTES = {
    "already-present": "Genre already present; no change",
    "case-differs": "Genre already present with different capitalisation; existing spelling kept",
    "not-present": "Genre not present; no change",
    "field-deleted": "Last genre removed; the genre field will be deleted",
    "numeric-reference-stored-as-name": "A numeric ID3 genre reference will be stored as its name",
}
PLAN_SKIPS = {
    "changed-while-planning": "File changed while the preview was being built; refresh the preview",
    "outside-root": "File resolves outside its library root",
}


class PlanError(ValueError):
    """The request cannot be planned (bad input or too many files)."""


@dataclass(frozen=True)
class FileRef:
    """One file to plan: its library root, path relative to it, and album."""
    root: str
    relpath: str
    album_key: str = ""
    album_label: str = ""

    @property
    def path(self) -> str:
        return os.path.join(self.root, self.relpath)


@dataclass
class FilePlan:
    seq: int
    root: str
    relpath: str
    album_key: str
    album_label: str
    action: str                       # change | nochange | skip
    reason: str | None                # skip reason code
    format: str | None
    size: int | None
    mtime_ns: int | None
    inode: int | None
    sha_before: str | None
    style: str | None
    genres_before: list[str] | None   # stored values
    components_before: list[str] | None
    genres_after: list[str] | None    # stored values after the operation (None when skipped)
    components_after: list[str] | None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class EditPlan:
    operation: str
    genres: list[str]
    items: list[FilePlan]
    warnings: list[str]
    digest: str

    def count(self, action: str) -> int:
        return sum(1 for i in self.items if i.action == action)

    @property
    def n_change(self) -> int:
        return self.count("change")

    @property
    def n_nochange(self) -> int:
        return self.count("nochange")

    @property
    def n_skip(self) -> int:
        return self.count("skip")

    def by_album(self) -> dict[str, list[FilePlan]]:
        """Items grouped by album key, in first-seen order."""
        out: dict[str, list[FilePlan]] = {}
        for it in self.items:
            out.setdefault(it.album_key, []).append(it)
        return out

    def to_dict(self) -> dict:
        return {"operation": self.operation, "genres": list(self.genres), "warnings": list(self.warnings),
                "digest": self.digest, "items": [asdict(i) for i in self.items]}


def digest(operation: str, genres: Sequence[str], items: Sequence[FilePlan], warnings: Sequence[str] = ()) -> str:
    """sha256 of the canonical JSON of everything the user is shown."""
    payload = {"operation": operation, "genres": list(genres), "warnings": list(warnings),
               "items": [asdict(i) for i in items]}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _default_reader(paths: list[str]) -> list[ReadResult]:
    from gama.metadata.readproc import read_files  # child processes with timeouts
    return read_files(paths)


def _inside(root: str, path: str) -> bool:
    r = os.path.realpath(root)
    p = os.path.realpath(path)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def _plan_one(seq: int, ref: FileRef, rr: ReadResult, operation: str, genres: tuple[str, ...]) -> FilePlan:
    st = rr.stat
    g = rr.genre
    item = FilePlan(
        seq=seq, root=ref.root, relpath=ref.relpath, album_key=ref.album_key, album_label=ref.album_label,
        action="skip", reason=None, format=rr.format,
        size=st.size if st else None, mtime_ns=st.mtime_ns if st else None, inode=st.inode if st else None,
        sha_before=None, style=g.style if g else None,
        genres_before=list(g.values) if g else None, components_before=list(g.components) if g else None,
        genres_after=None, components_after=None, warnings=list(rr.warnings), error=rr.error)

    if not _inside(ref.root, ref.path):
        item.reason = "outside-root"
        return item
    if not rr.ok:
        item.reason = rr.skip_reason or rr.reason or "unreadable"
        return item
    # Fingerprint, then make sure the file did not change since it was read.
    try:
        item.sha_before = sha256_file(ref.path)
        now = os.stat(ref.path)
    except OSError as e:
        item.reason, item.error = "unreadable", f"{type(e).__name__}: {e}"
        return item
    if st is None or (now.st_size, now.st_mtime_ns, now.st_ino) != (st.size, st.mtime_ns, st.inode):
        item.reason = "changed-while-planning"
        return item
    if rr.skip_reason:
        item.reason = rr.skip_reason
        return item

    res = genres_mod.apply(g, operation, genres)
    item.genres_after = list(res.values)
    item.components_after = list(res.components)
    item.warnings += [n for n in res.notes if n not in item.warnings]
    item.action = "change" if res.changed else "nochange"
    return item


def _unindexed_audio(refs: Sequence[FileRef]) -> list[str]:
    """Audio files in the planned files' directories that are not in the plan."""
    known = {os.path.normpath(r.path) for r in refs}
    dirs = sorted({os.path.dirname(os.path.normpath(r.path)) for r in refs})
    warnings = []
    for d in dirs:
        try:
            names = sorted(e.name for e in os.scandir(d) if e.is_file(follow_symlinks=False))
        except OSError:
            continue
        extra = [n for n in names if not n.startswith(TEMP_PREFIX) and Path(n).suffix.lower() in AUDIO_EXTENSIONS
                 and os.path.join(d, n) not in known]
        if extra:
            warnings.append(f"unindexed-audio: {d}: {len(extra)} audio file(s) not in the selection; rescan suggested")
    return warnings


def plan_files(files: Iterable[FileRef], operation: str, genres: Sequence[str], *,
               reader: Callable[[list[str]], list[ReadResult]] | None = None,
               max_files: int = MAX_FILES) -> EditPlan:
    """Build an EditPlan. Raises PlanError for invalid input; never writes."""
    refs = list(files)
    try:
        wanted = genres_mod.validate_request(operation, genres)
    except genres_mod.GenreError as e:
        raise PlanError(str(e)) from e
    if not refs:
        raise PlanError("no files selected")
    if len(refs) > max_files:
        raise PlanError(f"{len(refs)} files selected; at most {max_files} per plan. Narrow the selection.")
    seen: set[str] = set()
    for r in refs:
        p = os.path.normpath(r.path)
        if p in seen:
            raise PlanError(f"file listed twice: {r.relpath}")
        seen.add(p)

    results = (reader or _default_reader)([r.path for r in refs])
    if len(results) != len(refs) or any(rr.path != r.path for rr, r in zip(results, refs)):
        raise PlanError("reader returned results that do not match the requested files")
    items = [_plan_one(i, ref, rr, operation, wanted) for i, (ref, rr) in enumerate(zip(refs, results))]
    warnings = _unindexed_audio(refs)
    return EditPlan(operation, list(wanted), items, warnings, digest(operation, wanted, items, warnings))

