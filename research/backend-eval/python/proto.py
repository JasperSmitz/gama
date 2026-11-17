#!/usr/bin/env python3
"""Backend-eval prototype (python / mutagen). Disposable research code, not Gama.

proto read <paths...>
proto set-genres --genres "A;B" --backup-dir D --journal J [--continue-on-error] [--fail-after N] <files...>
proto set-fields [--backup-dir D --journal J] [--continue-on-error] [--fail-after N] <files...>
proto rollback --journal J
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

import mutagen
from mutagen.apev2 import APEv2, APENoHeaderError
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TCON, TIT2, TPE1, TALB, TPE2, TDRC, TRCK, TPOS
from mutagen.id3 import ID3FileType
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

PER_FILE_TIMEOUT_S = 30

EXT_FORMAT = {".flac": "FLAC", ".mp3": "MP3", ".m4a": "MP4", ".mp4": "MP4",
              ".ogg": "OggVorbis", ".oga": "OggVorbis", ".opus": "OggOpus"}

FIELDS = {"title": "Gama Title ✓", "artist": "Gama Artist", "album": "Gama Album",
          "album_artist": "Gama Album Artist", "year": "2024",
          "track": 7, "track_total": 12, "disc": 2, "disc_total": 3}

# Keys each operation is allowed to change, per tag family (lower-case for Vorbis).
VC_GENRE = {"genre"}
VC_FIELDS = {"title", "artist", "album", "albumartist", "date", "tracknumber",
             "tracktotal", "totaltracks", "discnumber", "disctotal", "totaldiscs"}
ID3_GENRE = {"TCON"}
ID3_FIELDS = {"TIT2", "TPE1", "TALB", "TPE2", "TDRC", "TRCK", "TPOS"}
MP4_GENRE = {"©gen", "gnre"}
MP4_FIELDS = {"©nam", "©ART", "©alb", "aART", "©day", "trkn", "disk"}


class Timeout(Exception):
    pass


@contextmanager
def deadline(seconds: int):
    def handler(signum, frame):
        raise Timeout(f"timed out after {seconds}s")
    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def sha256_file(p: str | Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def err_str(e: BaseException) -> str:
    return f"{type(e).__module__}.{type(e).__name__}: {e}"


def fsync_dir(d: str | Path) -> None:
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- reading

def family(f) -> str:
    if isinstance(f, ID3FileType) or isinstance(getattr(f, "tags", None), ID3):
        return "id3"
    if isinstance(f, MP4):
        return "mp4"
    if isinstance(f, (FLAC, OggVorbis, OggOpus)) or hasattr(f.tags, "vendor"):
        return "vorbis"
    return "other"


def pic_dict(ptype, mime, data: bytes) -> dict:
    return {"type": ptype, "mime": mime, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def int_pair(v: str | None) -> tuple[int | None, int | None]:
    if not v:
        return None, None
    a, _, b = str(v).partition("/")
    def toint(s):
        try:
            return int(s.strip())
        except ValueError:
            return None
    return toint(a), toint(b) if b else None


def first(lst):
    return lst[0] if lst else None


def pictures_of(f) -> list[dict]:
    fam = family(f)
    out = []
    if isinstance(f, FLAC):
        out += [pic_dict(p.type, p.mime, p.data) for p in f.pictures]
    if fam == "vorbis" and f.tags is not None:
        for v in f.tags.get("metadata_block_picture", []):
            try:
                p = Picture(base64.b64decode(v))
                out.append(pic_dict(p.type, p.mime, p.data))
            except Exception as e:  # noqa: BLE001
                out.append({"error": err_str(e)})
    elif fam == "id3" and f.tags is not None:
        for fr in f.tags.getall("APIC"):
            out.append(pic_dict(int(fr.type), fr.mime, fr.data))
    elif fam == "mp4" and f.tags is not None:
        for c in f.tags.get("covr", []):
            mime = {MP4Cover.FORMAT_JPEG: "image/jpeg", MP4Cover.FORMAT_PNG: "image/png"}.get(
                c.imageformat, f"format:{c.imageformat}")
            out.append(pic_dict(None, mime, bytes(c)))
    return out


def short(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n} chars)"


def read_tags(f, path: str) -> dict:
    fam = family(f)
    t = f.tags
    r: dict = {"title": None, "artist": None, "album": None, "album_artist": None,
               "year": None, "track": None, "track_total": None, "disc": None,
               "disc_total": None, "genres": [], "other_tags": {}}
    if t is None:
        pass
    elif fam == "vorbis":
        g = lambda k: first(t.get(k, []))  # noqa: E731
        r.update(title=g("title"), artist=g("artist"), album=g("album"),
                 album_artist=g("albumartist") or g("album artist"),
                 year=g("date") or g("year"), genres=list(t.get("genre", [])))
        r["track"], r["track_total"] = int_pair(g("tracknumber"))
        r["disc"], r["disc_total"] = int_pair(g("discnumber"))
        tt = g("tracktotal") or g("totaltracks")
        dt = g("disctotal") or g("totaldiscs")
        if tt:
            r["track_total"] = int_pair(tt)[0]
        if dt:
            r["disc_total"] = int_pair(dt)[0]
        skip = VC_FIELDS | VC_GENRE | {"album artist", "year", "metadata_block_picture"}
        for k, v in t:
            if k.lower() not in skip:
                r["other_tags"].setdefault(k, []).append(short(v))
        r["vendor"] = t.vendor
    elif fam == "id3":
        txt = lambda fid: (str(t[fid].text[0]) if fid in t and t[fid].text else None)  # noqa: E731
        r.update(title=txt("TIT2"), artist=txt("TPE1"), album=txt("TALB"),
                 album_artist=txt("TPE2"), year=txt("TDRC"))
        r["track"], r["track_total"] = int_pair(txt("TRCK"))
        r["disc"], r["disc_total"] = int_pair(txt("TPOS"))
        if "TCON" in t:
            r["genres"] = list(t["TCON"].genres)
            r["genres_raw"] = [str(x) for x in t["TCON"].text]
        r["id3_version"] = ".".join(map(str, t.version))
        for fr in t.values():
            if fr.FrameID in ID3_FIELDS | ID3_GENRE | {"APIC"}:
                continue
            if hasattr(fr, "text") and fr.FrameID not in ("COMM", "USLT", "TXXX"):
                vals = [short(str(x)) for x in fr.text]
            else:
                vals = [short(fr.pprint())]
            r["other_tags"].setdefault(fr.HashKey, []).extend(vals)
    elif fam == "mp4":
        g = lambda k: (str(t[k][0]) if k in t and t[k] else None)  # noqa: E731
        r.update(title=g("©nam"), artist=g("©ART"), album=g("©alb"),
                 album_artist=g("aART"), year=g("©day"))
        if "trkn" in t and t["trkn"]:
            r["track"], r["track_total"] = t["trkn"][0]
        if "disk" in t and t["disk"]:
            r["disc"], r["disc_total"] = t["disk"][0]
        r["genres"] = [str(x) for x in t.get("©gen", [])]
        if "gnre" in t:
            from mutagen.id3 import TCON as _T
            r["genres_numeric_gnre"] = [str(x) for x in t["gnre"]]
            if not r["genres"]:
                r["genres"] = [_T.GENRES[x - 1] if 0 < x <= len(_T.GENRES) else str(x)
                               for x in t["gnre"]]
        for k, vs in t.items():
            if k in MP4_FIELDS | MP4_GENRE | {"covr"}:
                continue
            out = []
            for v in vs if isinstance(vs, list) else [vs]:
                if isinstance(v, bytes):
                    out.append(short(v.decode("utf-8", "replace")))
                else:
                    out.append(short(str(v)))
            r["other_tags"][k] = out
    else:
        for k, v in (t.items() if hasattr(t, "items") else []):
            r["other_tags"][str(k)] = [short(str(v))]
    # APEv2 on MP3 is invisible to mutagen.mp3.MP3; read it separately.
    if fam == "id3":
        try:
            ape = APEv2(path)
            for k, v in ape.items():
                r["other_tags"]["APEv2:" + k] = [short(str(v))]
        except APENoHeaderError:
            pass
        except Exception as e:  # noqa: BLE001
            r["other_tags"]["APEv2:error"] = [err_str(e)]
    return r


def sniff_class(path: str):
    """Pure content sniff, used only as a fallback when mutagen's pick fails."""
    with open(path, "rb") as fh:
        head = fh.read(64)
    if head.startswith(b"fLaC"):
        return FLAC
    if head.startswith(b"OggS"):
        return OggOpus if b"OpusHead" in head else OggVorbis
    if head[4:8] == b"ftyp":
        return MP4
    return None


def detect(path: str, info: dict | None = None):
    """mutagen.File scores every format class on extension + header bytes; ties
    are broken by list order. If that pick fails to parse, retry with a pure
    content sniff and record that the fallback was used."""
    info = info if info is not None else {}
    with deadline(PER_FILE_TIMEOUT_S):
        try:
            f = mutagen.File(path)
            info["method"] = "mutagen.File score (extension + header bytes)"
        except Exception as first_err:  # noqa: BLE001
            cls = sniff_class(path)
            if cls is None:
                raise
            try:
                f = cls(path)
            except Exception:  # noqa: BLE001
                raise first_err from None
            info["method"] = "content sniff fallback (mutagen.File pick failed)"
            info["mutagen_default_error"] = err_str(first_err)
    if f is None:
        raise ValueError("mutagen.File returned None (no format scored > 0: unsupported/unknown)")
    return f


def read_one(path: str) -> dict:
    t0 = time.perf_counter()
    res: dict = {"path": path, "ok": False, "error": None, "format": None}
    ext_guess = EXT_FORMAT.get(Path(path).suffix.lower())
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
        res["magic"] = head[:4].hex()
        det: dict = {}
        res["detection"] = det
        f = detect(path, det)
        fmt = type(f).__name__
        res["format"] = fmt
        res["codec"] = getattr(f.info, "codec", None) or type(f.info).__name__
        det.update(extension_implies=ext_guess, agrees_with_extension=fmt == ext_guess)
        res["length_s"] = round(getattr(f.info, "length", 0) or 0, 3)
        res["sample_rate"] = getattr(f.info, "sample_rate", None)
        with deadline(PER_FILE_TIMEOUT_S):
            res.update(read_tags(f, path))
            res["pictures"] = pictures_of(f)
        res["ok"] = True
    except Exception as e:  # noqa: BLE001
        res["error"] = err_str(e)
        res["error_is_mutagen_error"] = isinstance(e, mutagen.MutagenError)
    res["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return res


def iter_files(paths):
    for p in paths:
        p = Path(p)
        if p.is_dir():
            for q in sorted(p.rglob("*")):
                if q.is_file():
                    yield str(q)
        else:
            yield str(p)


def cmd_read(a) -> int:
    out = [read_one(p) for p in iter_files(a.paths)]
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- tag change + snapshot

def canon(v) -> str:
    if isinstance(v, bytes):
        return "b:" + hashlib.sha256(v).hexdigest() + f":{getattr(v, 'imageformat', '')}{getattr(v, 'dataformat', '')}"
    return repr(v)


def snapshot(f, exclude: set[str]) -> dict:
    """Comparable view of every tag/picture not in `exclude` (+ stream info)."""
    fam = family(f)
    t = f.tags
    snap: dict = {"info": [round(getattr(f.info, "length", 0) or 0, 3),
                           getattr(f.info, "sample_rate", None),
                           getattr(f.info, "channels", None)]}
    if isinstance(f, FLAC):
        snap["flac_pictures"] = [canon(p.write()) for p in f.pictures]
    if t is None:
        snap["tags"] = []
    elif fam == "vorbis":
        snap["tags"] = [(k.lower(), v) for k, v in t if k.lower() not in exclude]
        snap["vendor"] = t.vendor
    elif fam == "id3":
        snap["tags"] = sorted((fr.HashKey, canon(fr._writeData()) if hasattr(fr, "_writeData") else repr(fr))
                              for fr in t.values() if fr.FrameID not in exclude)
    elif fam == "mp4":
        snap["tags"] = sorted((k, [canon(x) for x in (v if isinstance(v, list) else [v])])
                              for k, v in t.items() if k not in exclude)
    else:
        raise ValueError(f"unsupported tag family for {type(f).__name__}")
    return snap


def ensure_tags(f):
    if f.tags is None:
        f.add_tags()


def apply_genres(f, genres: list[str]) -> None:
    ensure_tags(f)
    fam = family(f)
    if fam == "vorbis":
        f.tags["genre"] = list(genres)
    elif fam == "id3":
        f.tags.setall("TCON", [TCON(encoding=3, text=list(genres))])
    elif fam == "mp4":
        f.tags["©gen"] = list(genres)
        f.tags.pop("gnre", None)
    else:
        raise ValueError(f"cannot write genres to {type(f).__name__}")


def check_genres(f, genres: list[str]) -> list[str]:
    fam = family(f)
    if fam == "vorbis":
        got = list(f.tags.get("genre", []))
    elif fam == "id3":
        got = [str(x) for x in f.tags["TCON"].text] if "TCON" in f.tags else []
    else:
        got = [str(x) for x in f.tags.get("©gen", [])] + (["<gnre present>"] if "gnre" in f.tags else [])
    return [] if got == list(genres) else [f"genres after write {got} != {genres}"]


def apply_fields(f) -> None:
    ensure_tags(f)
    fam = family(f)
    F = FIELDS
    t = f.tags
    if fam == "vorbis":
        for k in ("totaltracks", "totaldiscs"):
            if k in t:
                del t[k]
        t["title"], t["artist"], t["album"] = [F["title"]], [F["artist"]], [F["album"]]
        t["albumartist"], t["date"] = [F["album_artist"]], [F["year"]]
        t["tracknumber"], t["tracktotal"] = [str(F["track"])], [str(F["track_total"])]
        t["discnumber"], t["disctotal"] = [str(F["disc"])], [str(F["disc_total"])]
    elif fam == "id3":
        for cls, val in ((TIT2, F["title"]), (TPE1, F["artist"]), (TALB, F["album"]),
                         (TPE2, F["album_artist"]), (TDRC, F["year"]),
                         (TRCK, f"{F['track']}/{F['track_total']}"),
                         (TPOS, f"{F['disc']}/{F['disc_total']}")):
            t.setall(cls.__name__, [cls(encoding=3, text=[val])])
    elif fam == "mp4":
        t["©nam"], t["©ART"], t["©alb"] = [F["title"]], [F["artist"]], [F["album"]]
        t["aART"], t["©day"] = [F["album_artist"]], [F["year"]]
        t["trkn"] = [(F["track"], F["track_total"])]
        t["disk"] = [(F["disc"], F["disc_total"])]
    else:
        raise ValueError(f"cannot write fields to {type(f).__name__}")


def check_fields(f) -> list[str]:
    r = read_tags(f, f.filename)
    probs = []
    for k, v in FIELDS.items():
        got = r.get(k)
        if (str(got) if got is not None else None) != str(v):
            probs.append(f"{k}: {got!r} != {v!r}")
    return probs


# ---------------------------------------------------------------- journal

class Journal:
    def __init__(self, path: str, data: dict | None = None):
        self.path = Path(path)
        self.data = data or {"version": 1, "created": time.time(), "entries": []}

    @classmethod
    def load(cls, path: str) -> "Journal":
        return cls(path, json.loads(Path(path).read_text()))

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        fsync_dir(self.path.parent)

    def add(self, entry: dict) -> dict:
        self.data["entries"].append(entry)
        self.save()
        return entry

    def mark(self, entry: dict, state: str, **kw) -> None:
        entry["state"] = state
        entry.update(kw)
        self.save()


def restore_entry(e: dict) -> dict:
    """Restore one file from its backup via temp+fsync+rename; verify sha256."""
    path, backup = e["path"], e.get("backup")
    r = {"path": path, "ok": False}
    try:
        if not backup or not os.path.exists(backup):
            raise FileNotFoundError(f"backup missing: {backup}")
        if os.path.exists(path) and sha256_file(path) == e["sha256_before"]:
            r.update(ok=True, action="already-identical")
            return r
        d = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(prefix=".proto-restore-", suffix=Path(path).suffix, dir=d)
        try:
            with os.fdopen(fd, "wb") as out, open(backup, "rb") as src:
                shutil.copyfileobj(src, out, 1 << 20)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(tmp, e.get("mode", 0o644))
            os.replace(tmp, path)
            fsync_dir(d)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        got = sha256_file(path)
        r.update(ok=got == e["sha256_before"], action="restored", sha256_after_restore=got)
        if not r["ok"]:
            r["error"] = "sha256 mismatch after restore"
    except Exception as ex:  # noqa: BLE001
        r["error"] = err_str(ex)
    return r


# ---------------------------------------------------------------- safe batch write

class SimulatedCrash(Exception):
    pass


def preflight(path: str) -> tuple[dict, object | None]:
    r: dict = {"path": path, "ok": False}
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        r["mode"] = oct(stat.S_IMODE(st.st_mode))
        if not os.access(path, os.W_OK):
            # Note: the rename-over strategy only needs write access to the
            # *directory*, so without this check a read-only file would be replaced.
            raise PermissionError("file is not writable (os.access W_OK false)")
        d = os.path.dirname(os.path.abspath(path))
        if not os.access(d, os.W_OK | os.X_OK):
            raise PermissionError(f"directory not writable: {d}")
        f = detect(path)
        if family(f) not in ("vorbis", "id3", "mp4"):
            raise ValueError(f"unsupported format {type(f).__name__}")
        r.update(ok=True, format=type(f).__name__)
        return r, f
    except Exception as e:  # noqa: BLE001
        r["error"] = err_str(e)
        return r, None


def write_one(path: str, op: str, genres: list[str], backup_dir: Path, j: Journal) -> dict:
    exclude = {"genres": VC_GENRE | ID3_GENRE | MP4_GENRE,
               "fields": VC_FIELDS | ID3_FIELDS | MP4_FIELDS}[op]
    st = os.stat(path)
    mode = stat.S_IMODE(st.st_mode)
    sha_before = sha256_file(path)
    # 2. backup + journal entry (fsynced)
    backup = backup_dir / f"{len(j.data['entries']):04d}-{hashlib.sha256(os.path.abspath(path).encode()).hexdigest()[:12]}-{Path(path).name}"
    shutil.copy2(path, backup)
    with open(backup, "rb") as fh:
        os.fsync(fh.fileno())
    fsync_dir(backup_dir)
    if sha256_file(backup) != sha_before:
        raise IOError("backup sha256 mismatch")
    entry = j.add({"path": os.path.abspath(path), "backup": str(backup.resolve()),
                   "sha256_before": sha_before, "mode": mode, "state": "backed_up"})
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".proto-tmp-", suffix=Path(path).suffix, dir=d)
    os.close(fd)
    try:
        with deadline(PER_FILE_TIMEOUT_S * 4):
            # 3. copy original -> temp in same dir, edit temp with mutagen, fsync
            shutil.copyfile(path, tmp)
            pre = detect(path)
            pre_snap = snapshot(pre, exclude)
            cls = type(pre)  # open the temp copy with the class detected for the original
            f = cls(tmp)
            if op == "genres":
                apply_genres(f, genres)
            else:
                apply_fields(f)
            f.save()
            with open(tmp, "rb+") as fh:
                os.fsync(fh.fileno())
            # 4. reopen and verify
            post = cls(tmp)
            probs = check_genres(post, genres) if op == "genres" else check_fields(post)
            post_snap = snapshot(post, exclude)
            for k in sorted(set(pre_snap) | set(post_snap)):
                if pre_snap.get(k) != post_snap.get(k):
                    a, b = pre_snap.get(k), post_snap.get(k)
                    if k == "tags":
                        a, b = [x for x in a if x not in b], [x for x in b if x not in a]
                    probs.append(f"unrelated '{k}' changed: {short(repr(a), 300)} -> {short(repr(b), 300)}")
            if probs:
                raise ValueError("post-write verification failed: " + "; ".join(probs))
        # 5. preserve mode, atomic rename, mark committed
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_dir(d)
        j.mark(entry, "committed", sha256_after=sha256_file(path))
        return {"path": path, "ok": True, "state": "committed"}
    except BaseException as e:
        j.mark(entry, "failed", error=err_str(e))
        raise
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def cmd_write(a, op: str) -> int:
    genres = [g for g in (a.genres or "").split(";") if g] if op == "genres" else []
    if op == "genres" and not genres:
        print(json.dumps({"ok": False, "error": "empty --genres"}))
        return 2
    if a.backup_dir is None:
        a.backup_dir = tempfile.mkdtemp(prefix="proto-backup-")
    backup_dir = Path(a.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    journal = a.journal or str(backup_dir / "journal.json")
    report: dict = {"command": f"set-{op}", "journal": journal, "backup_dir": str(backup_dir),
                    "preflight": [], "results": [], "rolled_back": [], "aborted": False}
    files = list(iter_files(a.files))
    # 1. preflight
    healthy = []
    for p in files:
        r, _ = preflight(p)
        report["preflight"].append(r)
        if r["ok"]:
            healthy.append(p)
    bad = [r for r in report["preflight"] if not r["ok"]]
    if bad and not a.continue_on_error:
        report.update(ok=False, aborted=True, error=f"preflight failed for {len(bad)} file(s); nothing modified")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1
    j = Journal(journal)
    j.data["command"] = f"set-{op}"
    j.data["skipped_preflight"] = [r["path"] for r in bad]
    j.save()
    commits = 0
    fatal = None
    for p in healthy:
        try:
            if a.fail_after is not None and commits >= a.fail_after:
                raise SimulatedCrash(f"--fail-after {a.fail_after}: simulated failure after {commits} commits")
            report["results"].append(write_one(p, op, genres, backup_dir, j))
            commits += 1
        except Exception as e:  # noqa: BLE001
            report["results"].append({"path": p, "ok": False, "error": err_str(e),
                                      "trace": traceback.format_exc(limit=3)[-600:]})
            if a.continue_on_error and not isinstance(e, SimulatedCrash):
                continue
            fatal = e
            break
    if fatal is not None:
        # 6. roll back everything committed in this batch
        for e in j.data["entries"]:
            if e["state"] == "committed":
                rr = restore_entry(e)
                report["rolled_back"].append(rr)
                j.mark(e, "rolled_back" if rr["ok"] else "rollback_failed")
        report["aborted"] = True
    report["ok"] = fatal is None and not bad and all(r["ok"] for r in report["results"])
    report["summary"] = {"files": len(files), "preflight_failed": len(bad),
                         "committed": sum(1 for e in j.data["entries"] if e["state"] == "committed"),
                         "failed": sum(1 for r in report["results"] if not r["ok"]),
                         "rolled_back_ok": sum(1 for r in report["rolled_back"] if r["ok"])}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


def cmd_rollback(a) -> int:
    j = Journal.load(a.journal)
    res = []
    for e in j.data["entries"]:
        if e["state"] in ("backed_up", "failed"):
            # original never replaced; still verify it is intact
            pass
        r = restore_entry(e)
        res.append(r)
        if r["ok"]:
            j.mark(e, "rolled_back")
    ok = all(r["ok"] for r in res)
    print(json.dumps({"command": "rollback", "ok": ok, "results": res}, indent=2, ensure_ascii=False))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="proto")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("paths", nargs="+")
    for name in ("set-genres", "set-fields"):
        s = sub.add_parser(name)
        if name == "set-genres":
            s.add_argument("--genres", required=True)
            s.add_argument("--backup-dir", required=True)
            s.add_argument("--journal", required=True)
        else:
            s.add_argument("--backup-dir")
            s.add_argument("--journal")
        s.add_argument("--continue-on-error", action="store_true")
        s.add_argument("--fail-after", type=int)
        s.add_argument("files", nargs="+")
    rb = sub.add_parser("rollback")
    rb.add_argument("--journal", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "read":
        return cmd_read(a)
    if a.cmd == "set-genres":
        return cmd_write(a, "genres")
    if a.cmd == "set-fields":
        return cmd_write(a, "fields")
    return cmd_rollback(a)


if __name__ == "__main__":
    sys.exit(main())
