"""E4 helper: before/after details for edge files (master vs work)."""
import sys, json
from pathlib import Path
from mutagen.id3 import ID3
from mutagen.apev2 import APEv2
from mutagen.flac import FLAC
master, work = Path(sys.argv[1]), Path(sys.argv[2])

def tail_info(p):
    b = p.read_bytes()
    v1 = b[-128:] if b[-128:-125] == b"TAG" else None
    d = {"size": len(b), "starts": b[:4].hex(), "id3v1": None, "apev2_present": b"APETAGEX" in b[-200000:]}
    if v1:
        d["id3v1"] = {"title": v1[3:33].rstrip(b"\0 ").decode("latin1"), "genre_byte": v1[127]}
    return d

for name in ("mp3_id3v23.mp3", "mp3_id3v1_only.mp3", "mp3_id3v24_apev2_id3v1.mp3", "truncated.mp3"):
    print(f"== {name}")
    for label, p in (("master", master / name), ("work", work / name)):
        t = ID3(p, translate=False, load_v1=False) if True else None
        raw = {k: (str(v)[:60] if hasattr(v, "text") else type(v).__name__) for k, v in t.items()}
        print(f"  {label}: v2.{t.version[1]} frames={sorted(raw)}")
        print(f"     tail={tail_info(p)}")
        try:
            print(f"     APEv2={dict((k, str(v)) for k, v in APEv2(p).items())}")
        except Exception as e:
            print(f"     APEv2: {type(e).__name__}")
    if name == "mp3_id3v23.mp3":
        for label, p in (("master", master / name), ("work", work / name)):
            t = ID3(p, translate=False)
            print(f"  {label} date frames:", {k: str(t[k]) for k in t if k[:4] in ("TYER", "TDAT", "TDRC", "TIME", "TCON")})
for name in ("flac_with_id3v2_prefix.flac", "flac_two_pictures_no_padding.flac", "actually_flac.mp3", "truncated.flac"):
    print(f"== {name}")
    for label, p in (("master", master / name), ("work", work / name)):
        f = FLAC(p)
        print(f"  {label}: starts={p.read_bytes()[:4]!r} blocks={[type(b).__name__ + ':' + str(getattr(b, 'length', '')) for b in f.metadata_blocks]} pics={[(x.type, x.mime, len(x.data)) for x in f.pictures]}")
