"""Read one file: detect, read tags, classify. ``read_file`` never raises."""
from __future__ import annotations

import os
import stat as stat_mod
from pathlib import Path

from gama.metadata import detect as detect_mod
from gama.metadata import policy, snapshot
from gama.metadata.formats import id3, mp4, vorbis
from gama.metadata.model import FileStat, ReadResult

KNOWN_AUDIO_EXTENSIONS = set().union(*detect_mod.EXTENSIONS.values())


def file_stat(path: str | Path) -> FileStat:
    lst = os.lstat(path)
    is_link = stat_mod.S_ISLNK(lst.st_mode)
    st = os.stat(path) if is_link else lst
    return FileStat(st.st_size, st.st_mtime_ns, st.st_ino, st.st_nlink, st.st_uid, st.st_gid,
                    stat_mod.S_IMODE(st.st_mode), is_link)


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:500]


def read_file(path: str | Path, euid: int | None = None) -> ReadResult:
    path = str(path)
    euid = os.geteuid() if euid is None else euid
    try:
        st = file_stat(path)
    except FileNotFoundError as e:
        return ReadResult(path, False, reason="missing", error=_err(e), skip_reason="unreadable")
    except Exception as e:  # noqa: BLE001
        return ReadResult(path, False, reason="unreadable", error=_err(e), skip_reason="unreadable")
    try:
        if not stat_mod.S_ISREG(os.stat(path).st_mode):
            return ReadResult(path, False, reason="not-regular-file", skip_reason="unsupported-format", stat=st)
        det = detect_mod.detect(path)
    except Exception as e:  # noqa: BLE001
        return ReadResult(path, False, reason="unreadable", error=_err(e), skip_reason="unreadable", stat=st)
    if det.empty:
        return ReadResult(path, False, reason="empty-file", skip_reason="unreadable", stat=st)
    if det.format is None and Path(path).suffix.lower() in KNOWN_AUDIO_EXTENSIONS:
        # Named like a supported file but no recognisable header: corrupt, not "unsupported".
        return ReadResult(path, False, reason="unreadable", error="no recognisable audio header",
                          skip_reason="unreadable", stat=st)
    if not det.supported:
        return ReadResult(path, False, format=det.format, reason="unsupported-format",
                          error=f"format: {det.format or 'unknown'}", skip_reason="unsupported-format", stat=st)
    try:
        trailers = snapshot.read_trailers(path)
        if det.format in vorbis.CLASSES:
            tags = vorbis.read(path, det.format, trailers)
        elif det.format == "mp3":
            tags = id3.read(path, det.format, trailers)
        else:
            tags = mp4.read(path, det.format, trailers, det.mp4)
    except Exception as e:  # noqa: BLE001 - fuzzing found IndexError/ValueError, not just MutagenError
        return ReadResult(path, False, format=det.format, reason="unreadable", error=_err(e),
                          skip_reason="unreadable", stat=st)
    try:
        skip, warnings = policy.classify(det, tags, st, euid)
    except Exception as e:  # noqa: BLE001 - a policy bug must not crash a scan; refuse the file
        skip, warnings = "unreadable", [f"policy error: {_err(e)}"]
    return ReadResult(path, True, format=det.format, tags=tags, skip_reason=skip, warnings=warnings, stat=st)
