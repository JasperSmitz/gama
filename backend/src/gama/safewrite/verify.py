"""Verify a mutated temp file against its backup (plan 1.6 step 4).

Both files are re-read in a read child (``readproc``); mutagen never runs in
this process. The temp file passes only if:

(a) its stored genre values equal the expected values exactly;
(b) its snapshot (everything except the genre) equals the backup's, apart
    from the explicitly whitelisted ID3v2.3 -> 2.4 conversions
    (TYER/TDAT/TIME -> TDRC, TORY -> TDOR, IPLS -> TIPL), whose values are
    recomputed here with mutagen's rules and compared;
(c) the raw ID3v1 trailer and APEv2 bytes are identical (part of the snapshot);
(d) the compressed audio packets are identical: ``ffmpeg -map 0:a -c copy -f
    md5``. ``audio="auto"`` runs this when ffmpeg is on PATH, else the result
    records ``not_checked``; ``audio="off"`` never runs it.

This uses mutagen's view plus raw bytes plus ffmpeg. The independent oracle
(``tools/verify``) is used by the test suite. Known mutagen normalisations are
handled by policy (files it would change are refused at read time).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import zip_longest
from pathlib import Path

from gama.metadata.model import ReadResult

UPGRADE_FROM = {"TYER": "TDRC", "TDAT": "TDRC", "TIME": "TDRC", "TORY": "TDOR", "IPLS": "TIPL"}
UPGRADE_TO = {"TDRC", "TDOR", "TIPL"}


@dataclass
class VerifyResult:
    ok: bool
    problems: list[str] = field(default_factory=list)
    audio: str = "not_checked"   # verified | not_checked | failed
    genres_read_back: list[str] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "problems": list(self.problems), "audio": self.audio,
                "genres_read_back": self.genres_read_back, "notes": list(self.notes)}


def _default_reader(paths: list[str]) -> list[ReadResult]:
    from gama.metadata.readproc import read_files
    return read_files(paths)


def audio_md5(path: str | Path) -> str | None:
    """md5 of the compressed audio packets (no decode); None if ffmpeg fails."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a", "-c", "copy",
                        "-f", "md5", "-"], capture_output=True, timeout=300)
    out = r.stdout.decode(errors="replace").strip()
    return out if r.returncode == 0 and out.startswith("MD5=") else None


def _expected_tdrc(conv: dict) -> list[str]:
    """mutagen's update_to_v24 TYER/TDAT/TIME -> TDRC rule (mutagen 1.48.1)."""
    stamps = []
    for tyer, tdat, time in zip_longest(conv.get("TYER", []), conv.get("TDAT", []), conv.get("TIME", []),
                                        fillvalue=""):
        ym = re.match(r"([0-9]{4})(-[0-9]{2}-[0-9]{2})?\Z", tyer)
        dm = re.match(r"([0-9]{2})([0-9]{2})\Z", tdat)
        tm = re.match(r"([0-9]{2})([0-9]{2})\Z", time)
        ts = ""
        if ym:
            year, month_day = ym.groups()
            ts += year
            if dm:
                month_day = "-%s-%s" % dm.groups()[::-1]
            if month_day:
                ts += month_day
                if tm:
                    ts += "T%s:%s:00" % tm.groups()
        if ts:
            stamps.append(ts)
    return stamps


def _diff(label: str, a, b, out: list[str]) -> None:
    if a != b:
        sa, sb = repr(a), repr(b)
        out.append(f"{label} changed: {sa[:200]} -> {sb[:200]}")


def _compare_id3(before: dict, after: dict, out: list[str]) -> list[str]:
    notes: list[str] = []
    vb, va = before.get("id3_version"), after.get("id3_version")
    upgraded = vb == "2.3" and va == "2.4"
    if vb != va and not upgraded and not (vb is None and va == "2.4"):
        out.append(f"ID3 version changed {vb} -> {va}")
        return notes
    if vb is None and va == "2.4":
        notes.append("new ID3v2.4 tag created")
    fb = [f for f in before["frames"]]
    fa = [f for f in after["frames"]]
    ids_b = list(before["raw_frame_ids"])
    cb, ca = dict(before.get("converted", {})), dict(after.get("converted", {}))
    if upgraded:
        notes.append("ID3v2.3 upgraded to v2.4")
        had = {k[:4] for k, _ in fb}
        fb = [f for f in fb if f[0][:4] not in UPGRADE_FROM]
        fa = [f for f in fa if not (f[0][:4] in UPGRADE_TO and f[0][:4] not in had)]
        # Sources (TYER/TDAT/TIME, TORY, IPLS) become one frame of their target each.
        ids_b = [i for i in ids_b if i not in UPGRADE_FROM] + sorted({UPGRADE_FROM[i] for i in ids_b
                                                                      if i in UPGRADE_FROM})
        exp: dict = {k: v for k, v in cb.items() if k not in UPGRADE_FROM}
        if "TDRC" not in cb and _expected_tdrc(cb):
            exp["TDRC"] = _expected_tdrc(cb)
        if "TORY" in cb and "TDOR" not in cb:
            exp["TDOR"] = list(cb["TORY"])
        if "IPLS" in cb and "TIPL" not in cb:
            exp["TIPL"] = cb["IPLS"]
        _diff("ID3 converted date/people values", exp, ca, out)
    else:
        _diff("ID3 date/people values", cb, ca, out)
    _diff("ID3 frames (excl. TCON)", sorted(fb), sorted(fa), out)
    _diff("ID3 raw frame ids (excl. TCON)", sorted(ids_b), sorted(after["raw_frame_ids"]), out)
    _diff("ID3 unknown frames", before.get("unknown_frames"), after.get("unknown_frames"), out)
    return notes


def compare_snapshots(before: dict, after: dict) -> tuple[list[str], list[str]]:
    """(problems, notes) between two snapshots (``TrackTags.snapshot``)."""
    probs: list[str] = []
    notes: list[str] = []
    for k in ("format", "stream", "pictures", "raw"):
        _diff(k, before.get(k), after.get(k), probs)
    tb, ta = before.get("tags", {}), after.get("tags", {})
    if before.get("format") == "mp3":
        notes += _compare_id3(tb, ta, probs)
    else:
        for k in sorted(set(tb) | set(ta)):
            _diff(f"tags.{k}", tb.get(k), ta.get(k), probs)
    return probs, notes


def verify_temp(backup: str | Path, temp: str | Path, expected_values: Sequence[str], *, audio: str = "auto",
                reader: Callable[[list[str]], list[ReadResult]] | None = None) -> VerifyResult:
    """Check ``temp`` (mutated) against ``backup`` (original bytes). Never raises."""
    res = VerifyResult(ok=False)
    try:
        rb, rt = (reader or _default_reader)([str(backup), str(temp)])
    except Exception as e:  # noqa: BLE001
        res.problems.append(f"re-read failed: {type(e).__name__}: {e}")
        return res
    if not rb.ok or rb.tags is None:
        res.problems.append(f"backup unreadable: {rb.reason}: {rb.error}")
    if not rt.ok or rt.tags is None:
        res.problems.append(f"temp file unreadable after write: {rt.reason}: {rt.error}")
    if res.problems:
        return res
    res.genres_read_back = list(rt.genre.values)
    if res.genres_read_back != list(expected_values):
        res.problems.append(f"genres read back {res.genres_read_back} != expected {list(expected_values)}")
    p, n = compare_snapshots(rb.tags.snapshot, rt.tags.snapshot)
    res.problems += p
    res.notes += n
    if rt.skip_reason not in (None, "hardlink", "not-owner", "symlink"):
        # The written file must still be one Gama would accept (e.g. no mixed style, no dropped frames).
        res.problems.append(f"written file is refused by policy: {rt.skip_reason}")

    if audio == "off" or (audio == "auto" and shutil.which("ffmpeg") is None):
        res.audio = "not_checked"
    else:
        try:
            a, b = audio_md5(backup), audio_md5(temp)
        except (OSError, subprocess.TimeoutExpired) as e:
            a = b = None
            res.notes.append(f"ffmpeg failed: {e}")
        if a is None or b is None or a != b:
            res.audio = "failed"
            res.problems.append(f"audio packets differ or could not be checked: {a} -> {b}")
        else:
            res.audio = "verified"
    res.ok = not res.problems
    return res
