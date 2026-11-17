"""Side experiments: read-only write, ID3v2.3 save options. Usage: side_experiments.py <workdir>"""
import os, shutil, sys, hashlib
from pathlib import Path
import mutagen
from mutagen.id3 import ID3, TCON
W = Path(sys.argv[1])
ro = W / "albums/partial_failure/05 readonly.m4a"
f = mutagen.File(ro); f.tags["©gen"] = ["X"]
try:
    f.save(); print("readonly: mutagen save SUCCEEDED (bad)")
except Exception as e:
    print(f"readonly: mutagen.save() -> {type(e).__module__}.{type(e).__name__}: {e}")
tmp = ro.with_name(".probe.m4a"); shutil.copyfile(ro, tmp)
os.replace(tmp, ro)
print(f"readonly: os.replace(tmp, readonly_file) succeeded; new mode {oct(ro.stat().st_mode & 0o777)} "
      "-> rename-over bypasses the read-only bit; only the explicit preflight check prevents it")
src = W / "edge/mp3_id3v23.mp3"
for ver, sep in ((4, "/"), (3, "/"), (3, ";")):
    p = W / f"edge/v23_test_{ver}_{'slash' if sep == '/' else 'semi'}.mp3"; shutil.copyfile(src, p)
    t = ID3(p); t.setall("TCON", [TCON(encoding=3, text=["Ambient", "Electronic", "Soundtrack"])])
    t.save(p, v2_version=ver, v23_sep=sep)
    r = ID3(p, translate=False)
    b = p.read_bytes(); i = b.find(b"TCON")
    print(f"save v2_version={ver} v23_sep={sep!r}: file v2.{r.version[1]}, TCON text={r['TCON'].text}, raw={b[i+10:i+10+60]!r}, frames={sorted(r.keys())}")
    p.unlink()
