#!/usr/bin/env python3
"""Build the disposable audio fixture corpus for the Gama backend evaluation.

Uses only the Python stdlib plus ffmpeg/metaflac, so no candidate library
(mutagen, TagLib, lofty) is involved in building the fixtures it is tested on.

Usage: build_fixtures.py <music_root> <out_dir>

<music_root> is only read. Everything is written under <out_dir>/master.
"""
import base64
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

MUSIC = Path(sys.argv[1])
OUT = Path(sys.argv[2]) / "master"


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def copy_real(album_dir: str, pattern_files: list[str], dest: str):
    d = OUT / dest
    d.mkdir(parents=True, exist_ok=True)
    for name in pattern_files:
        shutil.copy2(MUSIC / album_dir / name, d / name)


def flac_picture_block(img: bytes, mime="image/jpeg", ptype=3, desc="") -> bytes:
    """Raw FLAC METADATA_BLOCK_PICTURE body (used base64 in Vorbis/Opus comments)."""
    m, dsc = mime.encode(), desc.encode()
    return (struct.pack(">II", ptype, len(m)) + m + struct.pack(">I", len(dsc)) + dsc
            + struct.pack(">IIIII", 500, 500, 24, 0, len(img)) + img)


def ape_v2_tag(items: dict[str, str]) -> bytes:
    """Minimal APEv2 tag with header + footer, as appended by foobar/mp3gain."""
    body = b""
    for k, v in items.items():
        vb = v.encode()
        body += struct.pack("<II", len(vb), 0) + k.encode() + b"\0" + vb
    size = len(body) + 32
    flags_hdr = 0xA0000000  # has header, this is header
    flags_ftr = 0x80000000  # has header, this is footer
    hdr = b"APETAGEX" + struct.pack("<IIII", 2000, size, len(items), flags_hdr) + b"\0" * 8
    ftr = b"APETAGEX" + struct.pack("<IIII", 2000, size, len(items), flags_ftr) + b"\0" * 8
    return hdr + body + ftr


def id3v1(title, artist, album, year, genre_byte=17) -> bytes:
    f = lambda s, n: s.encode("latin-1")[:n].ljust(n, b"\0")
    return b"TAG" + f(title, 30) + f(artist, 30) + f(album, 30) + f(year, 4) + f("", 28) + b"\0\x07" + bytes([genre_byte])


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # ---- Real files from the user's library (copied, never modified in place) ----
    c418 = "C418 - Minecraft - Volume Alpha (2011)"
    flacs = sorted(p.name for p in (MUSIC / c418).glob("*.flac"))[:3]
    copy_real(c418, flacs, "albums/real_flac")
    jd = "Joy Division - Unknown Pleasures (Collector's Edition) (1979)"
    mp3s = sorted(p.name for p in (MUSIC / jd).glob("*.mp3"))[:3]
    copy_real(jd, mp3s, "albums/real_mp3")
    pt = "CLOSURE _ CONTINUATION. LIVE. AMSTERDAM 07_11_22 - Porcupine Tree"
    m4as = sorted(p.name for p in (MUSIC / pt).glob("*.m4a"))[:3]
    copy_real(pt, m4as, "albums/real_m4a")
    cover = (MUSIC / c418 / "cover.jpg").read_bytes()
    (OUT / "cover.jpg").write_bytes(cover)

    # ---- Synthetic albums: Ogg Vorbis and Opus (not present in the library) ----
    pic_b64 = base64.b64encode(flac_picture_block(cover)).decode()
    for codec, ext, enc in (("vorbis", "ogg", ["-c:a", "libvorbis", "-q:a", "3"]),
                            ("opus", "opus", ["-c:a", "libopus", "-b:a", "96k"])):
        d = OUT / f"albums/synth_{codec}"
        d.mkdir(parents=True)
        for n in (1, 2, 3):
            # ffmpeg joins duplicate keys, so a single GENRE is written here; the
            # multi-value case for Vorbis comments is covered by the real FLACs.
            tags = {
                "TITLE": f"Synthetic {codec} {n}", "ARTIST": "Gama Test Artist",
                "ALBUMARTIST": "Gama Test Album Artist", "ALBUM": f"Synthetic {codec.title()} Album",
                "DATE": "2021", "TRACKNUMBER": str(n), "TRACKTOTAL": "3",
                "DISCNUMBER": "1", "DISCTOTAL": "1", "GENRE": "Electronic",
                "MUSICBRAINZ_TRACKID": f"8f0e2c6e-0000-4000-8000-00000000000{n}",
                "REPLAYGAIN_TRACK_GAIN": "-6.50 dB", "CUSTOM_UNRELATED": "keep me ✓ ünïcødé",
                # Too large for argv, hence the ffmetadata file.
                "METADATA_BLOCK_PICTURE": pic_b64,
            }
            esc = lambda v: "".join("\\" + c if c in "=;#\\\n" else c for c in v)
            meta = d / f".meta{n}.txt"
            meta.write_text(";FFMETADATA1\n" + "".join(f"{k}={esc(v)}\n" for k, v in tags.items()))
            run("ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={220 * n}:duration=8",
                "-f", "lavfi", "-i", "anoisesrc=d=8:a=0.05", "-f", "ffmetadata", "-i", str(meta),
                "-filter_complex", "[0:a][1:a]amix=inputs=2", *enc, "-map_metadata", "2",
                str(d / f"{n:02d} - Synthetic {codec}.{ext}"))
            meta.unlink()

    # ---- Edge cases ----
    e = OUT / "edge"
    e.mkdir()
    src_mp3 = OUT / "albums/real_mp3" / mp3s[0]
    src_flac = OUT / "albums/real_flac" / flacs[0]
    src_m4a = OUT / "albums/real_m4a" / m4as[0]

    # MP3 as ID3v2.3 with a custom TXXX frame and a two-value genre
    run("ffmpeg", "-y", "-i", str(src_mp3), "-map", "0", "-c", "copy", "-id3v2_version", "3",
        "-metadata", "genre=Rock/Post-Punk", "-metadata", "CUSTOM_UNRELATED=keep me",
        str(e / "mp3_id3v23.mp3"))
    # MP3 with only an ID3v1 tag. NOTE: the corpus used for the 2026-11-17
    # results was built with the invalid option "-write_id3v2 0", which ffmpeg
    # ignores, so that file also carried a 34-byte ID3v2.4 tag (TSSE only).
    # The true v1-only case was tested separately (see EVALUATION.md).
    run("ffmpeg", "-y", "-i", str(src_mp3), "-map", "0:a", "-c", "copy", "-map_metadata", "-1",
        "-id3v2_version", "0", "-write_id3v1", "0", str(e / "mp3_id3v1_only.mp3"))
    with open(e / "mp3_id3v1_only.mp3", "ab") as f:
        f.write(id3v1("V1 Title", "V1 Artist", "V1 Album", "1999"))
    # MP3 with ID3v2.4 + trailing APEv2 + ID3v1 (common after mp3gain/foobar)
    data = src_mp3.read_bytes()
    (e / "mp3_id3v24_apev2_id3v1.mp3").write_bytes(
        data + ape_v2_tag({"MP3GAIN_MINMAX": "120,210", "REPLAYGAIN_TRACK_GAIN": "-3.2 dB"})
        + id3v1("Disorder", "Joy Division", "Unknown Pleasures", "1979"))
    # FLAC with no PADDING block and two pictures (front + back)
    shutil.copy2(src_flac, e / "flac_two_pictures_no_padding.flac")
    run("metaflac", "--import-picture-from=4||back cover||" + str(OUT / "cover.jpg"),
        str(e / "flac_two_pictures_no_padding.flac"))
    run("metaflac", "--remove", "--block-type=PADDING", "--dont-use-padding",
        str(e / "flac_two_pictures_no_padding.flac"))
    # FLAC with a (non-standard) ID3v2 tag prepended, seen in the wild
    id3 = b"ID3\x03\x00\x00" + bytes([0, 0, 0, 0x1A]) + b"TIT2" + struct.pack(">I", 16) + b"\0\0" + b"\0Prepended ID3v2"
    id3 = id3[:10] + id3[10:].ljust(0x1A, b"\0")
    (e / "flac_with_id3v2_prefix.flac").write_bytes(id3 + src_flac.read_bytes())
    # ALAC in MP4 (note: no freeform '----' atoms; only one real_m4a file has one)
    run("ffmpeg", "-y", "-i", str(src_flac), "-map", "0:a", "-c:a", "alac",
        "-metadata", "title=ALAC Track", "-metadata", "artist=Gama Test Artist",
        "-metadata", "album=ALAC Album", "-metadata", "genre=Ambient",
        str(e / "m4a_alac.m4a"))
    # MP4 with moov *before* mdat (faststart): growing the tag must rewrite the
    # stco/co64 chunk offsets, a classic source of silent audio corruption.
    run("ffmpeg", "-y", "-i", str(src_m4a), "-map", "0", "-c", "copy", "-movflags", "+faststart",
        str(e / "m4a_faststart.m4a"))
    # Truncated files (60% of original length)
    for src, name in ((src_flac, "truncated.flac"), (src_mp3, "truncated.mp3"), (src_m4a, "truncated.m4a")):
        b = src.read_bytes()
        (e / name).write_bytes(b[: int(len(b) * 0.6)])
    # MP4 truncated so that moov (at end?) is lost: first 64 KiB only
    (e / "m4a_header_only.m4a").write_bytes(src_m4a.read_bytes()[:65536])
    # Zero-byte file, random garbage, and FLAC data with an .mp3 extension
    (e / "zero_bytes.mp3").write_bytes(b"")
    (e / "garbage.flac").write_bytes(os.urandom(200_000))
    shutil.copy2(src_flac, e / "actually_flac.mp3")

    # ---- Bulk-edit partial-failure album: mixed formats, one file read-only ----
    pf = OUT / "albums/partial_failure"
    pf.mkdir()
    shutil.copy2(src_flac, pf / "01 ok.flac")
    shutil.copy2(src_mp3, pf / "02 ok.mp3")
    shutil.copy2(e / "truncated.m4a", pf / "03 truncated.m4a")
    shutil.copy2(OUT / "albums/synth_opus" / "01 - Synthetic opus.opus", pf / "04 ok.opus")
    shutil.copy2(src_m4a, pf / "05 readonly.m4a")
    # read-only is applied by the per-candidate copy step (prepare_work.sh), since
    # copy2 would propagate it into master and break rebuilding.

    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"fixtures in {OUT}: {sum(1 for p in OUT.rglob('*') if p.is_file())} files, {total/1e6:.1f} MB")


if __name__ == "__main__":
    main()
