"""Generate fuzzed copies (truncations + byte corruption in the header region). Usage: make_fuzz.py <out_dir> <src files...>"""
import random, sys
from pathlib import Path
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
rng = random.Random(1234)
for src in map(Path, sys.argv[2:]):
    b = src.read_bytes()
    for i in range(60):
        d = bytearray(b[: rng.choice([rng.randrange(1, 300000), len(b)])])
        for _ in range(rng.randrange(1, 40)):
            if len(d) > 8:
                pos = rng.randrange(0, min(len(d), 400000))
                d[pos] = rng.randrange(256) if rng.random() < .7 else rng.choice([0, 0xff, 0x7f])
        (out / f"{src.stem[:12]}_{i:02d}{src.suffix}").write_bytes(bytes(d))
