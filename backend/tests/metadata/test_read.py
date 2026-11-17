"""M2: the metadata read path on the synthetic corpus.

Expected genres come from the oracle's stdlib parsers (never mutagen).
"""
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gama.metadata import policy
from gama.metadata.model import ReadResult
from gama.metadata.reader import read_file
from verify.verify import raw_genres

pytestmark = pytest.mark.slow

Z = "Format Zoo/"
B = "Broken/"

# relpath -> (ok, format, skip_reason, reason)
EXPECTED = {
    Z + "flac_single_genre.flac": (True, "flac", None, None),
    Z + "flac_multi_genre.flac": (True, "flac", None, None),
    Z + "flac_two_pictures_no_padding.flac": (True, "flac", None, None),
    Z + "flac_delimited.flac": (True, "flac", None, None),
    Z + "flac_mixed_style.flac": (True, "flac", "mixed-genre-style", None),
    Z + "flac_no_genre.flac": (True, "flac", None, None),
    Z + "flac_with_id3v2_prefix.flac": (True, "flac", "flac-id3-prefix", None),
    Z + "mp3_v24_full.mp3": (True, "mp3", None, None),
    Z + "mp3_v23_slash_genre.mp3": (True, "mp3", None, None),
    Z + "mp3_v23_numeric_genre.mp3": (True, "mp3", None, None),
    Z + "mp3_v22.mp3": (True, "mp3", "id3v2.2", None),
    Z + "mp3_id3v1_only.mp3": (True, "mp3", "id3v1-only", None),
    Z + "mp3_untagged.mp3": (True, "mp3", None, None),
    Z + "m4a_aac_moov_after_mdat.m4a": (True, "mp4", None, None),
    Z + "m4a_aac_faststart.m4a": (True, "mp4", None, None),
    Z + "m4a_alac.m4a": (True, "mp4", None, None),
    Z + "m4a_delimited.m4a": (True, "mp4", None, None),
    Z + "m4a_multi_data.m4a": (True, "mp4", None, None),
    Z + "m4a_gnre.m4a": (True, "mp4", None, None),
    Z + "m4a_fragmented.m4a": (True, "mp4", "mp4-fragmented", None),
    Z + "ogg_vorbis_multi_genre.ogg": (True, "ogg-vorbis", None, None),
    Z + "opus_multi_genre.opus": (True, "ogg-opus", None, None),
    Z + "unsupported.wav": (False, "wav", "unsupported-format", "unsupported-format"),
    # Truncated FLAC/MP3 keep their tags and stay readable; truncated MP4 is refused.
    B + "truncated.flac": (True, "flac", None, None),
    B + "truncated.mp3": (True, "mp3", None, None),
    B + "truncated.m4a": (False, "mp4", "unreadable", "unreadable"),
    B + "m4a_header_only.m4a": (True, "mp4", "mp4-truncated", None),
    B + "zero_bytes.mp3": (False, None, "unreadable", "empty-file"),
    B + "garbage.flac": (False, None, "unreadable", "unreadable"),
    B + "actually_flac.mp3": (True, "flac", "extension-mismatch", None),
}


@pytest.fixture(scope="module")
def results(corpus_master):
    return {rel: read_file(corpus_master.master / rel) for rel in corpus_master.manifest}


def test_every_file_reads_or_has_a_reason(results):
    for rel, r in results.items():
        assert isinstance(r, ReadResult)
        if r.ok:
            assert r.tags is not None and r.genre is not None, rel
            assert r.skip_reason is None or r.skip_reason in policy.SKIP, rel
        else:
            assert r.reason and r.skip_reason in policy.SKIP, rel
        assert all(w in policy.WARNINGS for w in r.warnings), (rel, r.warnings)


@pytest.mark.parametrize("rel", sorted(EXPECTED))
def test_classification(results, rel):
    r = results[rel]
    assert (r.ok, r.format, r.skip_reason, r.reason) == EXPECTED[rel], (r.error, r.warnings)


def test_album_tracks_are_writable(corpus_master, results):
    for rel, e in corpus_master.manifest.items():
        if e["group"] == "album":
            assert results[rel].writable, (rel, results[rel].skip_reason)


def test_genre_values_match_oracle(results, corpus_master):
    for rel, r in results.items():
        if not r.ok:
            continue
        expected = raw_genres(corpus_master.master / rel)
        if rel.endswith("m4a_gnre.m4a"):
            assert expected == ["<gnre 18>"]
            continue  # checked by name below
        if expected is None:
            # The oracle cannot parse it (e.g. atoms past EOF); we may read it but must not write it.
            assert r.skip_reason is not None, rel
            continue
        assert list(r.genre.values) == expected, rel


@pytest.mark.parametrize("rel,values", [
    (Z + "flac_multi_genre.flac", ["Rock", "Blues", "Rock"]),
    (Z + "mp3_v24_full.mp3", ["Post-Punk", "New Wave", "Gothic Rock"]),
    (Z + "m4a_multi_data.m4a", ["Rock", "Progressive Rock"]),
    (Z + "ogg_vorbis_multi_genre.ogg", ["Electronic", "Ambient"]),
    (Z + "opus_multi_genre.opus", ["Electronic", "Ambient"]),
])
def test_multi_genre_native(results, rel, values):
    g = results[rel].genre
    assert (g.style, list(g.values), list(g.components)) == ("native", values, values)


@pytest.mark.parametrize("rel", [Z + "m4a_delimited.m4a", Z + "flac_delimited.flac",
                                 "Yes/1972 - Close to the Edge/01 Close to the Edge.m4a"])
def test_delimited(results, rel):
    g = results[rel].genre
    assert g.style == "delimited" and g.delimiter == ";"
    assert g.values == ("Rock;Progressive Rock",) and g.components == ("Rock", "Progressive Rock")
    assert results[rel].writable


def test_mixed_is_skipped(results):
    r = results[Z + "flac_mixed_style.flac"]
    assert r.genre.style == "mixed" and not r.writable


def test_v23_slash_is_one_value(results):
    r = results[Z + "mp3_v23_slash_genre.mp3"]
    assert r.genre.values == ("Rock/Post-Punk",) and r.genre.components == ("Rock/Post-Punk",)
    assert "id3v2.3-upgrade" in r.warnings and r.writable


def test_numeric_tcon_resolved(results):
    r = results[Z + "mp3_v23_numeric_genre.mp3"]
    assert r.genre.values == ("(17)",) and r.genre.components == ("Rock",)
    assert "numeric-genre-reference" in r.warnings


def test_gnre_read_by_name(results):
    r = results[Z + "m4a_gnre.m4a"]
    assert r.genre.components == ("Rock",)
    assert r.tags.native["has_gnre"] and "mp4-gnre" in r.warnings and r.writable


def test_id3v1_is_not_loaded(results):
    r = results[Z + "mp3_id3v1_only.mp3"]
    assert r.genre.style == "empty" and r.tags.fields["title"] is None
    assert r.skip_reason == "id3v1-only"
    full = results[Z + "mp3_v24_full.mp3"]
    assert full.tags.fields["title"] == "Format"  # the v2 title, not the v1 one
    assert "id3v1-present" in full.warnings


def test_untagged_mp3_warns(results):
    r = results[Z + "mp3_untagged.mp3"]
    assert r.writable and r.genre.style == "empty" and "no-id3-tag" in r.warnings


def test_apev2_genre_warning(results):
    assert "apev2-genre" in results[Z + "mp3_v24_full.mp3"].warnings
    assert "apev2-genre" not in results[Z + "mp3_v23_slash_genre.mp3"].warnings


def test_duplicate_warning(results):
    assert "duplicate-genres" in results[Z + "flac_multi_genre.flac"].warnings
    assert "duplicate-genres" in results["Gama Inconsistent/Mixed Bag/04 Mixed 4.flac"].warnings


def test_misnamed_detected_by_content(results):
    r = results[B + "actually_flac.mp3"]
    assert r.format == "flac" and r.detected_by == "content" and r.genre.values == ("Rock",)
    assert r.skip_reason == "extension-mismatch"


def test_vorbis_key_spelling_recorded(results):
    assert results[Z + "flac_multi_genre.flac"].tags.native["genre_keys"] == ["genre"]
    assert results[Z + "flac_single_genre.flac"].tags.native["genre_keys"] == ["GENRE"]


def test_fields_and_pictures(results):
    r = results["King Crimson/1969 - In the Court of the Crimson King/01 21st Century Schizoid Man.flac"]
    assert r.tags.fields["album"] == "In the Court of the Crimson King"
    assert r.tags.fields["albumartist"] == "King Crimson"
    assert [p.type for p in r.tags.pictures] == [3]
    two = results[Z + "flac_two_pictures_no_padding.flac"]
    assert sorted((p.type, p.mime) for p in two.tags.pictures) == [(3, "image/jpeg"), (4, "image/png")]
    ed = results["Gama Edition Artist/Anniversary (1999 Original)/01 Song 1.opus"]
    assert ed.tags.fields["mb_albumid"] == "11111111-1111-4111-8111-111111111111"
    assert results[Z + "m4a_alac.m4a"].tags.fields["mb_albumid"] == "33333333-3333-4333-8333-333333333333"
    flat = results["Gama Test Orchestra/Flat Double Album/2-01 Flat 2.1.mp3"]
    assert flat.tags.fields["discnumber"] == "2/2"
    assert len(results[Z + "mp3_v24_full.mp3"].tags.pictures) == 2
    assert len(results[Z + "m4a_aac_moov_after_mdat.m4a"].tags.pictures) == 1
    assert len(results[Z + "ogg_vorbis_multi_genre.ogg"].tags.pictures) == 1


def test_results_round_trip_through_json(results):
    for rel, r in results.items():
        d = json.loads(json.dumps(r.to_dict()))
        assert ReadResult.from_dict(d).to_dict() == r.to_dict(), rel


def test_reading_does_not_modify_files(corpus):
    before = {p: p.read_bytes() for p in corpus.root.rglob("*") if p.is_file()}
    for p in before:
        read_file(p)
    assert {p: p.read_bytes() for p in before} == before


# --------------------------------------------------------------------------- filesystem policy

def test_symlink_skipped(corpus):
    src = corpus.path(Z + "flac_single_genre.flac")
    link = corpus.root / "link.flac"
    link.symlink_to(src)
    r = read_file(link)
    assert r.ok and r.skip_reason == "symlink" and r.genre.values == ("Rock",)


def test_hardlink_skipped(corpus):
    src = corpus.path(Z + "flac_single_genre.flac")
    os.link(src, corpus.root / "hard.flac")
    assert read_file(src).skip_reason == "hardlink"


def test_not_owner_skipped(corpus):
    p = corpus.path(Z + "flac_single_genre.flac")
    assert read_file(p).writable
    assert read_file(p, euid=os.geteuid() + 1).skip_reason == "not-owner"


def test_extension_mismatch(corpus):
    p = corpus.path(Z + "flac_single_genre.flac")
    q = p.with_suffix(".ogg")
    shutil.copyfile(p, q)
    r = read_file(q)
    assert r.format == "flac" and r.skip_reason == "extension-mismatch"


def test_missing_file(tmp_path):
    r = read_file(tmp_path / "nope.flac")
    assert not r.ok and r.reason == "missing"


def test_directory_is_not_regular(tmp_path):
    r = read_file(tmp_path)
    assert not r.ok and r.reason == "not-regular-file"


def test_unreadable_permissions(corpus):
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    p = corpus.path(Z + "flac_single_genre.flac")
    p.chmod(0)
    try:
        r = read_file(p)
    finally:
        p.chmod(0o644)
    assert not r.ok and r.reason == "unreadable" and "Permission" in r.error


# --------------------------------------------------------------------------- robustness

FUZZ_SOURCES = [Z + "flac_two_pictures_no_padding.flac", Z + "mp3_v24_full.mp3", Z + "m4a_aac_faststart.m4a",
                Z + "m4a_aac_moov_after_mdat.m4a", Z + "ogg_vorbis_multi_genre.ogg", Z + "opus_multi_genre.opus"]


def test_no_exception_escapes_read(corpus_master, tmp_path):
    rng = random.Random(42)
    n = 0
    for rel in FUZZ_SOURCES:
        data = (corpus_master.master / rel).read_bytes()
        variants = [data[:k] for k in (4, 11, 40, 200, 1000, 5000)]
        for _ in range(12):
            b = bytearray(data)
            for _ in range(rng.randint(1, 8)):
                b[rng.randrange(min(len(b), 6000))] = rng.randrange(256)
            variants.append(bytes(b))
        for v in variants:
            p = tmp_path / f"fuzz{n}{Path(rel).suffix}"
            p.write_bytes(v)
            n += 1
            r = read_file(p)  # must not raise
            assert isinstance(r, ReadResult)
            if not r.ok:
                assert r.reason and r.skip_reason


def test_metadata_package_has_no_django():
    src = Path(__file__).resolve().parents[2] / "src"
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import gama.metadata.reader, gama.metadata.readproc, gama.metadata.policy\n"
            "import gama.metadata.genres, gama.metadata.snapshot, gama.metadata.detect\n"
            "assert not [m for m in sys.modules if m.split('.')[0] == 'django'], 'django imported'\n") % str(src)
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
