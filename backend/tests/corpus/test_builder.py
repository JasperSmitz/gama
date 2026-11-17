"""Inventory of the synthetic corpus: every fixture has the property it claims.

Checks use raw bytes and the oracle's stdlib parsers, never mutagen.
"""
import ast
import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest

import fixtures.build as build_mod
from verify.verify import (ape_parse, detect, flac_parse, id3v1_parse, id3v2_parse, id3_genres, mp4_atoms,
                           mp4_parse, ogg_parse, raw_genres)

pytestmark = pytest.mark.slow

EXPECTED_FILES = 59


def top_atoms(data: bytes) -> list[bytes]:
    return [t for t, *_ in mp4_atoms(data, 0, len(data))]


def id3_ids(data: bytes) -> list[str]:
    return [fid for fid, _ in id3v2_parse(data)["frames"]]


def tcon_body(data: bytes) -> bytes:
    return next(b for fid, b in id3v2_parse(data)["frames"] if fid == "TCON")


CHECKS = {
    "single-genre": lambda d, g: len(g) == 1,
    "multi-genre-fields": lambda d, g: len(g) >= 2 and all(";" not in x for x in g),
    "duplicate-genre": lambda d, g: len(g) != len(set(g)),
    "no-genre": lambda d, g: g == [],
    "lowercase-key": lambda d, g: any(c.startswith("genre=") for c in flac_parse(d)["comments"]),
    "replaygain": lambda d, g: any(c.startswith("REPLAYGAIN_") for c in flac_parse(d)["comments"]),
    "delimited-genre": lambda d, g: len(g) == 1 and ";" in g[0],
    "mixed-style": lambda d, g: len(g) > 1 and any(";" in x for x in g),
    "picture": lambda d, g: (any(t == 6 for t, _ in flac_parse(d)["blocks"]) if d[:4] == b"fLaC"
                             else any(c.startswith("METADATA_BLOCK_PICTURE=") for c in ogg_parse(d)["comments"])),
    "two-pictures": lambda d, g: sorted(b[3] for t, b in flac_parse(d)["blocks"] if t == 6) == [3, 4],
    "no-padding": lambda d, g: all(t != 1 for t, _ in flac_parse(d)["blocks"]),
    "id3-prefix": lambda d, g: d[:3] == b"ID3" and detect(d) == "flac" and len(flac_parse(d)["prefix"]) > 10,
    "id3v2.4": lambda d, g: d[:4] == b"ID3\x04",
    "id3v2.3": lambda d, g: d[:4] == b"ID3\x03",
    "id3v2.2": lambda d, g: d[:4] == b"ID3\x02",
    "multi-tcon": lambda d, g: len(id3_genres(id3v2_parse(d))) >= 2 and id3_ids(d).count("TCON") == 1,
    "utf16": lambda d, g: tcon_body(d)[0] == 1,
    "slash-genre": lambda d, g: g == ["Rock/Post-Punk"],
    "numeric-tcon": lambda d, g: g == ["(17)"],
    "apic": lambda d, g: "APIC" in id3_ids(d),
    "uslt": lambda d, g: "USLT" in id3_ids(d),
    "txxx": lambda d, g: "TXXX" in id3_ids(d),
    "ufid": lambda d, g: "UFID" in id3_ids(d),
    "popm": lambda d, g: "POPM" in id3_ids(d),
    "apev2": lambda d, g: ape_parse(d) is not None and "genre" in ape_parse(d)["items"],
    "id3v1": lambda d, g: id3v1_parse(d) is not None,
    # F1: a true v1-only file has no ID3v2 header at all.
    "id3v1-only": lambda d, g: d[:3] != b"ID3" and b"ID3" not in d[:4096] and id3v1_parse(d) is not None,
    "untagged": lambda d, g: d[:3] != b"ID3" and id3v1_parse(d) is None and ape_parse(d) is None,
    "moov-after-mdat": lambda d, g: top_atoms(d).index(b"moov") > top_atoms(d).index(b"mdat"),
    "faststart": lambda d, g: top_atoms(d).index(b"moov") < top_atoms(d).index(b"mdat"),
    "freeform": lambda d, g: any(k.startswith("----:com.apple.iTunes:") for k, _ in mp4_parse(d)["items"]),
    "covr": lambda d, g: any(k == "covr" for k, _ in mp4_parse(d)["items"]),
    "alac": lambda d, g: b"alac" in d and detect(d) == "mp4",
    "multi-data": lambda d, g: g == ["Rock", "Progressive Rock"],
    "gnre": lambda d, g: g == ["<gnre 18>"] and b"\xa9gen" not in d,
    "fragmented": lambda d, g: b"moof" in top_atoms(d) and mp4_parse(d)["fragmented"],
    "unsupported": lambda d, g: detect(d) == "wav",
    "zero-bytes": lambda d, g: d == b"",
    "garbage": lambda d, g: len(d) > 1000 and detect(d) is None,
    "header-only": lambda d, g: len(d) <= 4096 and d[4:8] == b"ftyp",
    "truncated": lambda d, g: len(d) > 1000,
    "misnamed": lambda d, g: detect(d) == "flac",
}
INFORMATIONAL = re.compile(r"^(disc\d|albumartist=.*|[0-9a-f-]{36}|duplicate-title|edition|inconsistent|"
                           r"multi-disc-(subdir|flat))$")


def test_file_count_and_manifest(corpus_master):
    files = sorted(str(p.relative_to(corpus_master.master)) for p in corpus_master.master.rglob("*") if p.is_file())
    assert len(files) == EXPECTED_FILES
    assert files == sorted(corpus_master.manifest)
    assert Counter(e["group"] for e in corpus_master.manifest.values()) == {"album": 29, "format": 23, "broken": 7}


def test_build_is_fast(corpus_master):
    assert corpus_master.build_seconds < 30


def test_builder_does_not_use_mutagen():
    tree = ast.parse(Path(build_mod.__file__).read_text())
    names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any("mutagen" in (x or "") for x in names | mods)


def test_every_property_holds(corpus_master):
    failures = []
    for rel, e in sorted(corpus_master.manifest.items()):
        data = (corpus_master.master / rel).read_bytes()
        g = raw_genres(corpus_master.master / rel)
        for prop in e["props"]:
            check = CHECKS.get(prop)
            if check is None:
                assert INFORMATIONAL.match(prop), f"no check for property {prop!r} ({rel})"
                continue
            try:
                ok = check(data, g)
            except Exception as ex:  # noqa: BLE001 - report which fixture broke
                ok = f"{type(ex).__name__}: {ex}"
            if ok is not True:
                failures.append((rel, prop, ok))
    assert not failures


def test_manifest_genres_match_raw(corpus_master):
    for rel, e in corpus_master.manifest.items():
        if e["genres"] is not None:
            assert raw_genres(corpus_master.master / rel) == e["genres"], rel


def test_misnamed_file_is_flac_content(corpus_master):
    rel = corpus_master.with_prop("misnamed")[0]
    assert rel.endswith(".mp3")
    assert detect((corpus_master.master / rel).read_bytes()) == "flac"


def test_supported_files_decode(corpus_master):
    for rel, e in corpus_master.manifest.items():
        if e["group"] == "broken":
            continue
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(corpus_master.master / rel),
                            "-map", "0:a:0", "-f", "null", "-"], capture_output=True)
        assert r.returncode == 0 and not r.stderr.strip(), (rel, r.stderr[-300:])


def test_album_layout(corpus_master):
    m = corpus_master.manifest
    for album in ("King Crimson/1969 - In the Court of the Crimson King",
                  "Genesis/1973 - Selling England by the Pound", "Yes/1972 - Close to the Edge"):
        assert sum(r.startswith(album + "/") for r in m) == 3
    subdir = corpus_master.with_prop("multi-disc-subdir")
    assert {Path(r).parent.name for r in subdir} == {"CD1", "CD2"}
    flat = corpus_master.with_prop("multi-disc-flat")
    assert len({Path(r).parent for r in flat}) == 1 and len(flat) == 4
    dup = corpus_master.with_prop("duplicate-title")
    assert len({Path(r).parent for r in dup}) == 2 and {Path(r).parent.name for r in dup} == {"Greatest Hits"}
    eds = corpus_master.with_prop("edition")
    mbids = {next(c for c in ogg_parse((corpus_master.master / r).read_bytes())["comments"]
                  if c.startswith("MUSICBRAINZ_ALBUMID=")) for r in eds}
    assert len(mbids) == 2
    inc = corpus_master.with_prop("inconsistent")
    artists = {next(c for c in flac_parse((corpus_master.master / r).read_bytes())["comments"]
                    if c.startswith("ALBUMARTIST=")) for r in inc}
    assert len(artists) == 2
    assert len({tuple(raw_genres(corpus_master.master / r)) for r in inc}) == 4


def test_formats_covered(corpus_master):
    kinds = {detect((corpus_master.master / r).read_bytes()) for r in corpus_master.manifest}
    assert {"flac", "mp3", "mp4", "ogg-vorbis", "ogg-opus", "wav", None} <= kinds
