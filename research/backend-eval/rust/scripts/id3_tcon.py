#!/usr/bin/env python3
"""Library-free ID3v2 dump of TCON + frame list (supplements verify.py, because
ffprobe only reports the first value of a multi-value ID3v2.4 TCON frame).
Usage: id3_tcon.py <files...>"""
import sys

def ss(b):  # synchsafe int
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]

ENC = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}

for path in sys.argv[1:]:
    b = open(path, "rb").read()
    if b[:3] != b"ID3":
        print(f"{path}: no ID3v2 header")
        continue
    ver, flags, size = b[3], b[5], ss(b[6:10])
    pos, end, frames, tcon = 10, 10 + size, [], None
    if flags & 0x40:
        pos += ss(b[10:14]) if ver == 4 else 4 + int.from_bytes(b[10:14], "big")
    while pos + 10 <= end and b[pos] != 0:
        fid = b[pos:pos + 4].decode("latin-1")
        fsz = ss(b[pos + 4:pos + 8]) if ver == 4 else int.from_bytes(b[pos + 4:pos + 8], "big")
        body = b[pos + 10:pos + 10 + fsz]
        frames.append(fid)
        if fid == "TCON":
            txt = body[1:].decode(ENC.get(body[0], "latin-1"), errors="replace")
            tcon = [t for t in txt.split("\x00") if t]
        pos += 10 + fsz
    print(f"{path.split('/')[-1]}: ID3v2.{ver} tag_flags=0x{flags:02x} TCON={tcon} frames={frames}")
