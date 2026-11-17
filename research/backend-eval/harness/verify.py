#!/usr/bin/env python3
"""Neutral verifier for the Gama backend evaluation.

Uses only ffmpeg/ffprobe/metaflac (never mutagen, TagLib or lofty) to compare a
candidate's modified working copy against the untouched master fixtures.

  verify.py genres <master_dir> <work_dir> --genres "A;B;C" [--json out.json]
      Every file whose bytes changed must have: identical decoded audio,
      identical compressed audio packets, identical embedded pictures,
      identical non-genre tags, and exactly the expected genre set.

  verify.py fields <master_dir> <work_dir> [--json out.json]
      Every changed file must carry the standard round-trip values below,
      identical audio and pictures, and unchanged unrelated tags.

Files whose bytes did not change are reported as "unchanged".
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

FIELDS_EXPECTED = {
    "title": "Gama Title ✓",
    "artist": "Gama Artist",
    "album": "Gama Album",
    "album_artist": "Gama Album Artist",
    "year": "2024",
    "track": "7",
    "track_total": "12",
    "disc": "2",
    "disc_total": "3",
}

# Normalise the various spellings ffprobe reports for the same concept.
ALIASES = {
    "albumartist": "album_artist", "album artist": "album_artist",
    "tracknumber": "track", "discnumber": "disc", "year": "date",
    "tracktotal": "track_total", "totaltracks": "track_total",
    "disctotal": "disc_total", "totaldiscs": "disc_total",
}
CORE = {"title", "artist", "album", "album_artist", "date", "track", "disc",
        "track_total", "disc_total", "genre"}
# Tags that encoders/muxers legitimately rewrite and are not user metadata.
VOLATILE = {"encoder", "vendor", "major_brand", "minor_version", "compatible_brands",
            "handler_name", "language", "creation_time"}


def sh(*args, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=False, check=check)


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def md5_of(path: Path, mapping: str, copy: bool) -> str | None:
    args = ["ffmpeg", "-v", "error", "-i", str(path), "-map", mapping]
    if copy:
        args += ["-c", "copy"]
    r = sh(*args, "-f", "md5", "-")
    out = r.stdout.decode().strip()
    return out if r.returncode == 0 and out.startswith("MD5=") else None


def probe(path: Path) -> dict:
    r = sh("ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path))
    if r.returncode != 0:
        return {"error": r.stderr.decode().strip()[:300]}
    return json.loads(r.stdout)


def tags_of(p: dict) -> dict[str, str]:
    """Merged tag view: format tags + first audio stream tags (Ogg keeps them there)."""
    out: dict[str, str] = {}
    sources = [p.get("format", {}).get("tags", {})]
    for s in p.get("streams", []):
        if s.get("codec_type") == "audio":
            sources.append(s.get("tags", {}))
            break
    for src in sources:
        for k, v in src.items():
            k2 = ALIASES.get(k.lower(), k.lower())
            out[k2] = v
    return out


def picture_hashes(path: Path, p: dict) -> list[str]:
    hs = []
    for s in p.get("streams", []):
        if s.get("disposition", {}).get("attached_pic") or s.get("codec_type") == "video":
            t = s.get("tags", {})
            digest = md5_of(path, "0:" + str(s["index"]), copy=True) or "unreadable"
            hs.append(f"{digest} type={t.get('comment')!r} desc={t.get('title')!r}")
    return sorted(hs)


def flac_comments(path: Path) -> list[str] | None:
    r = sh("metaflac", "--export-tags-to=-", str(path))
    return r.stdout.decode(errors="replace").splitlines() if r.returncode == 0 else None


def split_genres(v: str | None) -> list[str]:
    if not v:
        return []
    return sorted(g.strip() for g in re.split(r"[;\x00]", v) if g.strip())


def leading_int(v: str | None) -> str | None:
    m = re.match(r"\s*(\d+)", v or "")
    return str(int(m.group(1))) if m else None


def totals(tags: dict) -> tuple[str | None, str | None]:
    """track/disc totals may live in 'track=7/12' or a separate TRACKTOTAL."""
    def tot(key, tkey):
        if tags.get(tkey):
            return leading_int(tags[tkey])
        m = re.match(r"\s*\d+\s*/\s*(\d+)", tags.get(key, ""))
        return str(int(m.group(1))) if m else None
    return tot("track", "track_total"), tot("disc", "disc_total")


def _syncsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def id3v2_frames(path: Path) -> tuple[int, list[tuple[str, bytes]]] | None:
    """(major version, [(frame id, body)]) of a leading ID3v2.3/2.4 tag."""
    data = path.read_bytes()
    if data[:3] != b"ID3" or len(data) < 10:
        return None
    ver, flags = data[3], data[5]
    end = 10 + _syncsafe(data[6:10])
    pos = 10
    if flags & 0x40:  # extended header
        pos += _syncsafe(data[10:14]) if ver == 4 else 4 + int.from_bytes(data[10:14], "big")
    frames = []
    while pos + 10 <= end:
        fid = data[pos:pos + 4]
        if not fid.strip(b"\0"):
            break
        size = _syncsafe(data[pos + 4:pos + 8]) if ver == 4 else int.from_bytes(data[pos + 4:pos + 8], "big")
        frames.append((fid.decode("latin-1"), data[pos + 10:pos + 10 + size]))
        pos += 10 + size
    return ver, frames


def _id3_text(body: bytes) -> list[str]:
    enc, raw = body[0], body[1:]
    if enc in (1, 2):  # UTF-16: split on 2-byte NULs, decode each value separately
        parts, i, cur = [], 0, b""
        while i + 1 < len(raw):
            pair = raw[i:i + 2]
            if pair == b"\0\0":
                parts.append(cur); cur = b""
            else:
                cur += pair
            i += 2
        parts.append(cur)
        codec = "utf-16" if enc == 1 else "utf-16-be"
        return [p.decode(codec, "replace") for p in parts if p]
    codec = "latin-1" if enc == 0 else "utf-8"
    return [t for t in raw.decode(codec, "replace").split("\0") if t]


def id3v2_genres(path: Path) -> list[str] | None:
    """Raw TCON values (ffprobe shows only the first ID3v2.4 value)."""
    r = id3v2_frames(path)
    if r is None:
        return None
    ver, frames = r
    values: list[str] = []
    for fid, body in frames:
        if fid == "TCON" and body:
            parts = _id3_text(body)
            if ver == 3:  # v2.3 has no multi-value; '/' is a common convention
                parts = [q for t in parts for q in t.split("/") if q]
            values.extend(parts)
    return sorted(values)


ID3_DATE = {"TDRC", "TYER", "TDAT", "TIME", "TORY", "TDOR"}
ID3_FIELDS = {"TIT2", "TPE1", "TALB", "TPE2", "TRCK", "TPOS"} | ID3_DATE


def id3v2_inventory(path: Path, exclude: set[str]) -> tuple[int, dict] | None:
    """Comparable view of every ID3v2 frame: text frames by decoded values,
    other frames by a hash of their body. Catches frames ffprobe hides
    (UFID, POPM, PRIV, GEOB...) and extra values of multi-value frames."""
    r = id3v2_frames(path)
    if r is None:
        return None
    ver, frames = r
    inv: dict[str, list] = {}
    for fid, body in frames:
        if fid in exclude or not body:
            continue
        if fid.startswith("T") and fid != "TXXX":
            val = ("text", tuple(_id3_text(body)))
        else:
            # A trailing NUL terminator is optional; lofty drops it on USLT.
            val = ("raw", hashlib.sha256(body.rstrip(b"\0")).hexdigest()[:12])
        inv.setdefault(fid, []).append(val)
    return ver, {k: sorted(v) for k, v in inv.items()}


def ogg_comments(path: Path) -> list[str] | None:
    """Raw Vorbis comment list from the 2nd packet of an Ogg Vorbis/Opus stream
    (ffprobe joins repeated fields with ';', hiding single-vs-multi field)."""
    data = path.read_bytes()
    if data[:4] != b"OggS":
        return None
    pos, packets, cur, serial = 0, [], b"", None
    while pos + 27 <= len(data) and len(packets) < 2 and data[pos:pos + 4] == b"OggS":
        ser = data[pos + 14:pos + 18]
        nseg = data[pos + 26]
        lacing = data[pos + 27:pos + 27 + nseg]
        body = pos + 27 + nseg
        if serial is None:
            serial = ser
        if ser == serial:
            off = body
            for lv in lacing:
                cur += data[off:off + lv]
                off += lv
                if lv < 255:
                    packets.append(cur); cur = b""
        pos = body + sum(lacing)
    if len(packets) < 2:
        return None
    pkt = packets[1]
    if pkt.startswith(b"\x03vorbis"):
        i = 7
    elif pkt.startswith(b"OpusTags"):
        i = 8
    else:
        return None
    vlen = int.from_bytes(pkt[i:i + 4], "little"); i += 4 + vlen
    n = int.from_bytes(pkt[i:i + 4], "little"); i += 4
    out = []
    for _ in range(n):
        ln = int.from_bytes(pkt[i:i + 4], "little"); i += 4
        out.append(pkt[i:i + ln].decode("utf-8", "replace")); i += ln
    return out


def mp4_genres(path: Path) -> list[str] | None:
    """Raw '\xa9gen' data atoms (and numeric 'gnre') from moov/udta/meta/ilst."""
    data = path.read_bytes()

    def atoms(buf: bytes, start: int, end: int):
        i = start
        while i + 8 <= end:
            size = int.from_bytes(buf[i:i + 4], "big")
            typ = buf[i + 4:i + 8]
            hdr = 8
            if size == 1:
                size, hdr = int.from_bytes(buf[i + 8:i + 16], "big"), 16
            elif size == 0:
                size = end - i
            if size < hdr or i + size > end:
                return
            yield typ, i + hdr, i + size
            i += size

    def find(path_types, start, end):
        if not path_types:
            return (start, end)
        for typ, s, e in atoms(data, start, end):
            if typ == path_types[0]:
                if typ == b"meta":
                    s += 4  # full-box version/flags
                r = find(path_types[1:], s, e)
                if r:
                    return r
        return None

    ilst = find([b"moov", b"udta", b"meta", b"ilst"], 0, len(data))
    if not ilst:
        return None
    vals: list[str] = []
    for typ, s, e in atoms(data, *ilst):
        if typ in (b"\xa9gen", b"gnre"):
            for dt, ds, de in atoms(data, s, e):
                if dt == b"data":
                    payload = data[ds + 8:de]
                    vals.append(payload.decode("utf-8", "replace") if typ == b"\xa9gen"
                                else f"<gnre id {int.from_bytes(payload, 'big')}>")
    return sorted(vals)


def raw_genres(path: Path) -> tuple[str, list[str]] | None:
    head = path.read_bytes()[:12]
    if head[:3] == b"ID3" and path.suffix.lower() == ".mp3":
        g = id3v2_genres(path)
        return ("raw ID3v2 TCON", g) if g is not None else None
    if head[4:8] == b"ftyp":
        g = mp4_genres(path)
        return ("raw MP4 ilst", g) if g is not None else None
    return None


def tag_structure(path: Path) -> dict:
    """Container-level tag blocks that ffprobe hides once an ID3v2 tag exists."""
    b = path.read_bytes()
    st: dict = {"id3v2": None, "id3v1": None, "apev2": False}
    if b[:3] == b"ID3" and len(b) >= 10:
        st["id3v2"] = f"2.{b[3]}"
    if len(b) >= 128 and b[-128:-125] == b"TAG":
        st["id3v1"] = hashlib.md5(b[-128:]).hexdigest()[:8]
    st["apev2"] = b"APETAGEX" in b[-(64 * 1024):]
    return st


def compare(master: Path, work: Path, mode: str, genres: list[str]) -> dict:
    rel = str(work)
    res: dict = {"file": rel, "problems": [], "notes": []}
    if not work.exists():
        res["status"] = "MISSING"
        res["problems"].append("file missing from work dir")
        return res
    if sha256(master) == sha256(work):
        res["status"] = "unchanged"
        return res
    res["status"] = "changed"
    pm, pw = probe(master), probe(work)
    if "error" in pw:
        res["problems"].append(f"ffprobe cannot open modified file: {pw['error']}")
        return res
    if "error" in pm:
        res["notes"].append("master unreadable by ffprobe; audio comparison skipped")
    else:
        for label, copy in (("decoded audio (PCM md5)", False), ("compressed audio packets md5", True)):
            a, b = md5_of(master, "0:a:0", copy), md5_of(work, "0:a:0", copy)
            if a is None or b is None or a != b:
                res["problems"].append(f"{label} differs: {a} -> {b}")
        pa, pb = picture_hashes(master, pm), picture_hashes(work, pw)
        if pa != pb:
            res["problems"].append(f"embedded pictures differ: {len(pa)} -> {len(pb)} ({pa} -> {pb})")
    tm, tw = tags_of(pm) if "error" not in pm else {}, tags_of(pw)

    sm, sw = tag_structure(master), tag_structure(work)
    if sm["id3v1"] != sw["id3v1"]:
        if sm["id3v1"] and sw["id3v1"]:
            # Updating an existing v1 tag to mirror the v2 change is accepted
            # behaviour (mutagen, lofty and TagLib all do it).
            res["notes"].append("existing ID3v1 tag re-synced from new values")
        else:
            what = "added" if sm["id3v1"] is None else "removed"
            res["problems"].append(f"ID3v1 tag {what} (outside requested change)")
    if sm["apev2"] != sw["apev2"]:
        res["problems"].append(f"APEv2 tag presence changed: {sm['apev2']} -> {sw['apev2']}")
    if sm["id3v2"] != sw["id3v2"]:
        res["notes"].append(f"ID3v2 version {sm['id3v2']} -> {sw['id3v2']}")
    if (master.stat().st_mode & 0o7777) != (work.stat().st_mode & 0o7777):
        res["problems"].append(f"file mode changed: {oct(master.stat().st_mode & 0o7777)} -> {oct(work.stat().st_mode & 0o7777)}")

    # Raw ID3v2 frame inventory (frames ffprobe hides, extra multi-values).
    excl = {"TCON"} if mode == "genres" else ID3_FIELDS
    im, iw = id3v2_inventory(master, excl), id3v2_inventory(work, excl)
    if im and iw:
        upgraded = im[0] != iw[0]
        for fid in sorted(set(im[1]) | set(iw[1])):
            a, b = im[1].get(fid), iw[1].get(fid)
            if a == b:
                continue
            if upgraded and fid in ID3_DATE:
                res["notes"].append(f"ID3v2.{im[0]}->2.{iw[0]} converted {fid}: {a} -> {b}")
                continue
            msg = f"ID3v2 frame {fid} changed: {str(a)[:90]} -> {str(b)[:90]}"
            # Version upgrades legitimately re-encode non-text frame bodies.
            (res["notes"] if upgraded and a and b and len(a) == len(b) else res["problems"]).append(msg)
    elif im and not iw:
        res["problems"].append("ID3v2 tag removed")

    ignore = VOLATILE | ({"genre"} if mode == "genres" else CORE)
    for k in sorted((set(tm) | set(tw)) - ignore):
        if tm.get(k) != tw.get(k):
            a, b = (tm.get(k) or "<absent>")[:80], (tw.get(k) or "<absent>")[:80]
            res["problems"].append(f"unrelated tag '{k}' changed: {a!r} -> {b!r}")

    if mode == "genres":
        raw = raw_genres(work)
        got = raw[1] if raw else split_genres(tw.get("genre"))
        src = raw[0] if raw else "ffprobe"
        if got != sorted(genres):
            res["problems"].append(f"genres ({src}) {got} != expected {sorted(genres)}")
        else:
            res["notes"].append(f"genres checked via {src}")
        oc_m, oc_w = ogg_comments(master), ogg_comments(work)
        if oc_w is not None:
            gw = sorted(c.split("=", 1)[1] for c in oc_w if c.upper().startswith("GENRE="))
            if gw != sorted(genres):
                res["problems"].append(f"Ogg GENRE fields {gw} != expected (not separate fields?)")
            om = [c for c in (oc_m or []) if not c.upper().startswith("GENRE=")]
            ow = [c for c in oc_w if not c.upper().startswith("GENRE=")]
            if sorted(om) != sorted(ow):
                res["problems"].append(f"Ogg non-genre comments differ: -{sorted(set(om)-set(ow))[:3]} +{sorted(set(ow)-set(om))[:3]}")
            elif om != ow:
                res["notes"].append("Ogg non-genre comments reordered")
        if master.suffix == ".flac" or work.read_bytes()[:4] == b"fLaC":
            cm, cw = flac_comments(master) or [], flac_comments(work) or []
            gw = sorted(c.split("=", 1)[1] for c in cw if c.upper().startswith("GENRE="))
            if gw != sorted(genres):
                res["problems"].append(f"metaflac GENRE fields {gw} != expected (not stored as separate fields?)")
            om = [c for c in cm if not c.upper().startswith("GENRE=")]
            ow = [c for c in cw if not c.upper().startswith("GENRE=")]
            if om != ow:
                res["problems"].append("metaflac: non-genre comments differ (order or content)")
    else:
        exp = FIELDS_EXPECTED
        checks = {
            "title": tw.get("title"), "artist": tw.get("artist"), "album": tw.get("album"),
            "album_artist": tw.get("album_artist"),
            "year": (tw.get("date") or "")[:4], "track": leading_int(tw.get("track")),
            "disc": leading_int(tw.get("disc")),
        }
        tt, dt = totals(tw)
        checks["track_total"], checks["disc_total"] = tt, dt
        for k, v in checks.items():
            if v != exp[k]:
                res["problems"].append(f"field {k}: {v!r} != expected {exp[k]!r}")
        if tm.get("genre") != tw.get("genre"):
            res["problems"].append(f"genre changed during field write: {tm.get('genre')!r} -> {tw.get('genre')!r}")
    res["size_delta"] = work.stat().st_size - master.stat().st_size
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["genres", "fields"])
    ap.add_argument("master", type=Path)
    ap.add_argument("work", type=Path)
    ap.add_argument("--genres", default="")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--require-changed", action="store_true",
                    help="FAIL any file that ffprobe can decode but whose bytes did not change")
    a = ap.parse_args()
    genres = [g for g in a.genres.split(";") if g]
    results = []
    for m in sorted(p for p in a.master.rglob("*") if p.is_file() and p.suffix != ".jpg"):
        rel = m.relative_to(a.master)
        r = compare(m, a.work / rel, a.mode, genres)
        r["file"] = str(rel)
        results.append(r)
    extra = sorted(str(p.relative_to(a.work)) for p in a.work.rglob("*")
                   if p.is_file() and not (a.master / p.relative_to(a.work)).exists())
    if a.require_changed:
        for r in results:
            if r["status"] == "unchanged" and "error" not in probe(a.master / r["file"]):
                r["problems"].append("expected a change but file is byte-identical (skipped by writer?)")
    bad = sum(1 for r in results if r["problems"]) + (1 if extra else 0)
    for r in results:
        flag = "FAIL" if r["problems"] else "ok  "
        print(f"{flag} {r['status']:9} {r['file']}")
        for pr in r["problems"]:
            print(f"       - {pr}")
        for n in r.get("notes", []):
            if not n.startswith("genres checked"):
                print(f"       . note: {n}")
    if extra:
        print(f"leftover files in work dir (temp/backup leaks?): {extra}")
    print(f"\n{len(results)} files, {sum(r['status']=='changed' for r in results)} changed, {bad} with problems")
    if a.json:
        a.json.write_text(json.dumps({"results": results, "leftover": extra}, indent=2, ensure_ascii=False))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
