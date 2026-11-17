"""Index a library root (plan 1.2 and M6).

* Walk with ``os.scandir`` (directory symlinks are not followed, so the walk
  stays inside the root and cannot loop). Files with an audio extension are
  indexed; ``.gama-tmp-*`` files are ignored.
* A file is (re)read only when it is new or its size, mtime or inode changed.
  Reads run in child processes (``readproc``). Files that vanished are removed.
* Albums are rebuilt from the tracks after every scan. The identity key is
  stable, so an unchanged album keeps its id (and its place in users' baskets).

Album identity: the album directory is the track's parent, or the grandparent
when the parent is named like ``CD1``/``Disc 2``/``disk_3``. The key is
``sha1(root_id, album_dir, norm(albumartist), norm(album), mb_albumid)`` with
``norm`` = NFC, trim, collapse whitespace, casefold. A missing albumartist
stays empty (never the track artist), so compilations stay one album.
Different tags inside one directory split it into several albums, which are
then flagged ``inconsistent``; nothing is merged silently.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from gama.metadata.model import ReadResult
from gama.planning.planner import AUDIO_EXTENSIONS, TEMP_PREFIX

from .models import Album, LibraryRoot, MediaFile, Track

DISC_DIR_RE = re.compile(r"^(cd|disc|disk)[\s_-]*\d+$", re.IGNORECASE)
_WS = re.compile(r"\s+")


def norm(s: str | None) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFC", s or "").strip()).casefold()


def album_dir_of(relpath: str) -> str:
    parent = os.path.dirname(relpath)
    if DISC_DIR_RE.match(os.path.basename(parent)):
        return os.path.dirname(parent)
    return parent


def identity_key(root_id: int, album_dir: str, albumartist: str | None, album: str | None,
                 mb_albumid: str | None) -> str:
    parts = [str(root_id), album_dir, norm(albumartist), norm(album), (mb_albumid or "").strip()]
    return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()


@dataclass
class ScanStats:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    skipped_paths: list[str] = field(default_factory=list)  # e.g. names that are not valid Unicode
    albums: int = 0


def _default_reader(paths: list[str]) -> list[ReadResult]:
    from gama.metadata.readproc import read_files
    return read_files(paths)


def walk(root: str) -> tuple[dict[str, os.stat_result], list[str]]:
    """{relpath: lstat} for indexable files, plus relpaths that cannot be stored."""
    found: dict[str, os.stat_result] = {}
    bad: list[str] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                stack.append(e.path)
                continue
            if e.name.startswith(TEMP_PREFIX) or os.path.splitext(e.name)[1].lower() not in AUDIO_EXTENSIONS:
                continue
            if not (e.is_file(follow_symlinks=False) or e.is_symlink()):
                continue
            rel = os.path.relpath(e.path, root)
            try:
                rel.encode("utf-8")
            except UnicodeEncodeError:
                bad.append(rel.encode("utf-8", "surrogateescape").decode("utf-8", "replace"))
                continue
            try:
                # Same view as the reader (metadata.reader.file_stat): a symlink is compared by its target.
                found[rel] = e.stat(follow_symlinks=e.is_symlink())
            except OSError:
                continue
    return found, bad


def _track_fields(r: ReadResult) -> dict:
    f = r.tags.fields
    return {
        "title": f.get("title") or "", "artist": f.get("artist") or "", "albumartist": f.get("albumartist") or "",
        "album_title": f.get("album") or "", "discnumber": f.get("discnumber") or "",
        "tracknumber": f.get("tracknumber") or "", "mb_albumid": f.get("mb_albumid") or "",
        "genres": list(r.genre.values), "genre_style": r.genre.style,
        "warnings": list(r.warnings),
    }


def _store_result(mf: MediaFile, r: ReadResult, now) -> None:
    st = r.stat
    if st is not None:
        mf.size, mf.mtime_ns, mf.inode = st.size, st.mtime_ns, st.inode
    mf.format = r.format
    mf.detected_by = r.detected_by
    mf.read_ok = r.ok
    mf.read_error = None if r.ok else f"{r.reason}: {r.error}" if r.error else r.reason
    mf.writable_reason = r.skip_reason
    mf.scanned_at = now
    mf.save()
    if r.ok and r.tags is not None:
        Track.objects.update_or_create(file=mf, defaults=_track_fields(r))
    else:
        Track.objects.filter(file=mf).delete()


def scan_root(root: LibraryRoot, *, reader: Callable[[list[str]], list[ReadResult]] | None = None) -> ScanStats:
    stats = ScanStats()
    found, stats.skipped_paths = walk(root.path)
    existing = {mf.relpath: mf for mf in MediaFile.objects.filter(root=root)}
    to_read: list[str] = []
    for rel, st in found.items():
        mf = existing.get(rel)
        if mf is not None and (mf.size, mf.mtime_ns, mf.inode) == (st.st_size, st.st_mtime_ns, st.st_ino):
            stats.unchanged += 1
        else:
            to_read.append(rel)
    gone = [mf for rel, mf in existing.items() if rel not in found]

    results = (reader or _default_reader)([os.path.join(root.path, rel) for rel in to_read]) if to_read else []
    now = timezone.now()
    with transaction.atomic():
        for mf in gone:
            mf.delete()
            stats.removed += 1
        for rel, r in zip(to_read, results):
            mf = existing.get(rel)
            if mf is None:
                st = found[rel]
                mf = MediaFile(root=root, relpath=rel, size=st.st_size, mtime_ns=st.st_mtime_ns, inode=st.st_ino,
                               scanned_at=now)
                stats.added += 1
            else:
                stats.updated += 1
            _store_result(mf, r, now)
        stats.albums = rebuild_albums(root)
    return stats


def _disc_key(t: Track) -> str:
    m = re.match(r"\s*(\d+)", t.discnumber or "")
    if m:
        return str(int(m.group(1)))
    parent = os.path.basename(os.path.dirname(t.file.relpath))
    return parent.casefold() if DISC_DIR_RE.match(parent) else "1"


def rebuild_albums(root: LibraryRoot) -> int:
    """Regroup every track of ``root`` into albums. Returns the number of albums."""
    tracks = list(Track.objects.filter(file__root=root).select_related("file"))
    groups: dict[str, list[Track]] = defaultdict(list)
    meta: dict[str, tuple[str, str, str, str]] = {}
    for t in tracks:
        adir = album_dir_of(t.file.relpath)
        key = identity_key(root.id, adir, t.albumartist, t.album_title, t.mb_albumid)
        groups[key].append(t)
        meta.setdefault(key, (adir, t.album_title, t.albumartist, t.mb_albumid))
    per_dir = Counter(m[0] for m in meta.values())

    keep: set[str] = set()
    for key, ts in groups.items():
        adir, album, albumartist, mbid = meta[key]
        genre_sets = {tuple(t.genres) for t in ts}
        union: list[str] = []
        for t in ts:
            for g in t.genres:
                if g not in union:
                    union.append(g)
        split = per_dir[adir] > 1
        summary = {"union": union, "consistent": len(genre_sets) == 1, "split_directory": split}
        a, _ = Album.objects.update_or_create(identity_key=key, defaults={
            "root": root, "album_dir": adir, "album": album, "albumartist": albumartist, "mb_albumid": mbid,
            "track_count": len(ts), "disc_count": len({_disc_key(t) for t in ts}),
            "formats": sorted({t.file.format for t in ts if t.file.format}),
            "genres_summary": summary, "inconsistent": split or len(genre_sets) > 1})
        Track.objects.filter(id__in=[t.id for t in ts]).update(album=a)
        keep.add(key)
    Album.objects.filter(root=root).exclude(identity_key__in=keep).delete()
    return len(keep)


def ensure_roots(paths) -> list[LibraryRoot]:
    """LibraryRoot rows for the configured root paths (created when missing)."""
    out = []
    for p in paths:
        p = os.fspath(p)
        root, _ = LibraryRoot.objects.get_or_create(path=p, defaults={"name": os.path.basename(p.rstrip(os.sep)) or p})
        out.append(root)
    return out
