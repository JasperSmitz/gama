"""E1 helper: compare proto read output against ffprobe for core fields."""
import json, subprocess, sys, re
d = json.load(open(sys.argv[1]))
A = {"albumartist": "album_artist", "album artist": "album_artist", "tracknumber": "track",
     "discnumber": "disc", "year": "date", "tracktotal": "track_total", "totaltracks": "track_total",
     "disctotal": "disc_total", "totaldiscs": "disc_total"}
for r in d:
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", r["path"]], capture_output=True, text=True)
    name = r["path"].split("/work/")[1]
    if p.returncode:
        print(f"{name}: ffprobe error: {p.stderr.strip()[:120]} | proto ok={r['ok']}")
        continue
    j = json.loads(p.stdout); t = {}
    srcs = [j.get("format", {}).get("tags", {})] + [s.get("tags", {}) for s in j["streams"] if s.get("codec_type") == "audio"][:1]
    for s in srcs:
        for k, v in s.items():
            t[A.get(k.lower(), k.lower())] = v
    if not r["ok"]:
        print(f"{name}: proto FAILED ({r['error'][:80]}) but ffprobe reads format={j['format']['format_name']} title={t.get('title')!r}")
        continue
    diffs = []
    def li(v):
        m = re.match(r"\s*(\d+)", v or ""); return int(m.group(1)) if m else None
    for k in ("title", "artist", "album", "album_artist"):
        if (r.get(k) or None) != (t.get(k) or None):
            diffs.append(f"{k}: proto={r.get(k)!r} ffprobe={t.get(k)!r}")
    if (r.get("year") or "")[:4] != (t.get("date") or "")[:4]:
        diffs.append(f"year: proto={r.get('year')!r} ffprobe={t.get('date')!r}")
    for k in ("track", "disc"):
        if r.get(k) != li(t.get(k)):
            diffs.append(f"{k}: proto={r.get(k)!r} ffprobe={t.get(k)!r}")
    fg = sorted(x.strip() for x in re.split(r"[;\x00]", t.get("genre", "")) if x.strip())
    if sorted(r["genres"]) != fg:
        diffs.append(f"genres: proto={r['genres']} ffprobe={t.get('genre')!r}")
    npics = sum(1 for s in j["streams"] if s.get("disposition", {}).get("attached_pic") or s.get("codec_type") == "video")
    if npics != len(r["pictures"]):
        diffs.append(f"pictures: proto={len(r['pictures'])} ffprobe={npics}")
    print(f"{name}: " + ("match" if not diffs else "; ".join(diffs)))
