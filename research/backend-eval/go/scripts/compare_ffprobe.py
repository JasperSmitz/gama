#!/usr/bin/env python3
"""E1: compare proto read.json against ffprobe for the core fields + picture count.
usage: compare_ffprobe.py read.json"""
import json, re, subprocess, sys
AL = {"albumartist": "album_artist", "album artist": "album_artist", "tracknumber": "track",
      "discnumber": "disc", "tracktotal": "track_total", "totaltracks": "track_total",
      "disctotal": "disc_total", "totaldiscs": "disc_total"}
def probe(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", p], capture_output=True)
    if r.returncode: return None
    d = json.loads(r.stdout); t = {}
    srcs = [d.get("format", {}).get("tags", {})] + [s.get("tags", {}) for s in d.get("streams", []) if s.get("codec_type") == "audio"][:1]
    for s in srcs:
        for k, v in s.items(): t[AL.get(k.lower(), k.lower())] = v
    pics = sum(1 for s in d.get("streams", []) if s.get("disposition", {}).get("attached_pic"))
    return t, pics
def li(v, tot=False):
    m = re.match(r"\s*\d+\s*/\s*(\d+)" if tot else r"\s*(\d+)", v or "")
    return int(m.group(1)) if m else None
for r in json.load(open(sys.argv[1])):
    p = r["path"]; short = "/".join(p.split("/")[-2:])
    pr = probe(p)
    if pr is None:
        print(f"{short}: ffprobe cannot open; proto ok={r['ok']}"); continue
    t, pics = pr
    if not r["ok"]:
        print(f"{short}: proto FAILED ({r['error'][:70]}) but ffprobe reads it (title={t.get('title')!r})"); continue
    exp = {"title": t.get("title", ""), "artist": t.get("artist", ""), "album": t.get("album", ""),
           "album_artist": t.get("album_artist", ""), "year": li(t.get("date")),
           "track": li(t.get("track")), "disc": li(t.get("disc")),
           "track_total": li(t.get("track_total")) if t.get("track_total") else li(t.get("track"), True),
           "disc_total": li(t.get("disc_total")) if t.get("disc_total") else li(t.get("disc"), True),
           "genres": sorted(g for g in re.split(r"[;\x00]", t.get("genre", "")) if g)}
    got = dict(r); got["genres"] = sorted(g for x in r["genres"] for g in x.split(";") if g)
    diffs = [f"{k}: proto={got[k]!r} ffprobe={v!r}" for k, v in exp.items() if got[k] != v]
    if len(r["pictures"]) != pics: diffs.append(f"pictures: proto={len(r['pictures'])} ffprobe={pics}")
    print(f"{short}: " + ("match" if not diffs else "; ".join(diffs)))
