#!/usr/bin/env python3
"""Compare `proto read` JSON with ffprobe's view of the same files (E1)."""
import json, subprocess, sys, re
d = json.load(open(sys.argv[1]))
KEYS = {"title": "title", "artist": "artist", "album": "album", "album_artist": "album_artist",
        "year": "date", "track": "track", "disc": "disc", "genres": "genre"}
ALIAS = {"albumartist": "album_artist", "album artist": "album_artist", "tracknumber": "track",
         "discnumber": "disc", "year": "date"}
for r in d["results"]:
    p = r["path"]
    pr = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                         "-show_streams", p], capture_output=True, text=True)
    name = p.split("/work/")[-1]
    if pr.returncode != 0:
        print(f"{name}: ffprobe error: {pr.stderr.strip()[:120]} | proto ok={r['ok']} {r.get('error')}")
        continue
    j = json.loads(pr.stdout)
    tags = {}
    srcs = [j.get("format", {}).get("tags", {})] + [s.get("tags", {}) for s in j.get("streams", []) if s.get("codec_type") == "audio"][:1]
    for s in srcs:
        for k, v in s.items():
            tags[ALIAS.get(k.lower(), k.lower())] = v
    dur = float(j.get("format", {}).get("duration", 0) or 0) * 1000
    if not r["ok"]:
        print(f"{name}: proto FAILED ({r['error']}) but ffprobe reads it (dur {dur:.0f}ms, title={tags.get('title')!r})")
        continue
    diffs = []
    for k, fk in KEYS.items():
        mine = r.get(k)
        theirs = tags.get(fk)
        if k == "genres":
            mine = sorted(mine)
            theirs = sorted(g.strip() for g in re.split(r"[;\x00]", theirs or "") if g.strip())
        elif k in ("track", "disc", "year"):
            m = re.match(r"\s*(\d+)", theirs or "")
            theirs = int(m.group(1)) if m else None
        if mine != theirs:
            diffs.append(f"{k}: proto={mine!r} ffprobe={theirs!r}")
    pd = r["properties"]["duration_ms"]
    if abs(pd - dur) > 1000:
        diffs.append(f"duration: proto={pd}ms ffprobe={dur:.0f}ms")
    print(f"{name}: " + ("match" if not diffs else "; ".join(diffs)))
