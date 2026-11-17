"""Independent (no mutagen) ID3v2 frame walker: print raw TCON payload. Usage: id3_tcon_bytes.py files..."""
import sys
for path in sys.argv[1:]:
    b = open(path, "rb").read(1 << 20)
    if b[:3] != b"ID3":
        print(f"{path}: no ID3v2 header"); continue
    ver = b[3]; size = sum((b[6 + i] & 0x7f) << (7 * (3 - i)) for i in range(4)); p = 10
    while p < 10 + size and b[p] != 0:
        fs = (sum((b[p + 4 + i] & 0x7f) << (7 * (3 - i)) for i in range(4)) if ver == 4
              else int.from_bytes(b[p + 4:p + 8], "big"))
        if b[p:p + 4] == b"TCON":
            print(f"{path.rsplit('/', 1)[1]}: ID3v2.{ver} TCON payload {b[p + 10:p + 10 + fs]!r}")
        p += 10 + fs
