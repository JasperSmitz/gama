"""safewrite.mutate (child writer) and safewrite.verify (temp-vs-backup check).

verify must agree with the oracle on every planted defect it covers. File
mode, xattrs and leftover temp files are M5's job and not covered here.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fixtures.build import id3_apic_body, mp4_text_item
from gama.metadata.genres import apply
from gama.metadata.reader import read_file
from gama.safewrite import verify as verify_mod
from gama.safewrite.mutate import run_mutate
from gama.safewrite.verify import verify_temp
from tests.conformance.test_oracle import (mp4_replace_items, rewrite_id3, set_ogg_genres, set_tcon)
from verify.verify import compare

pytestmark = pytest.mark.slow

Z = "Format Zoo/"
BACKEND = str(Path(__file__).resolve().parents[2])


def mutated(corpus, rel, op="add", genres=("Jazz",)):
    """Write a legitimate change to the work copy; return the expected values."""
    p = corpus.path(rel)
    r = read_file(p)
    res = apply(r.genre, op, list(genres))
    m = run_mutate(p, r.format, res.values, r.genre.values)
    assert m.ok, m.error
    return list(res.values)


def both_reject(corpus, rel, expected):
    oracle = compare(corpus.master / rel, corpus.path(rel), "genres", expected)
    v = verify_temp(corpus.master / rel, corpus.path(rel), expected)
    assert oracle, "oracle accepted the defect"
    assert not v.ok and v.problems, "verify accepted the defect"
    return v


# --------------------------------------------------------------------------- verify accepts legit, rejects defects

def test_verify_accepts_legit_write(corpus):
    exp = mutated(corpus, Z + "mp3_v24_full.mp3")
    v = verify_temp(corpus.master / (Z + "mp3_v24_full.mp3"), corpus.path(Z + "mp3_v24_full.mp3"), exp)
    assert v.ok and v.audio == "verified" and v.genres_read_back == exp


def test_verify_rejects_wrong_genres(corpus):
    rel = Z + "mp3_v24_full.mp3"
    exp = mutated(corpus, rel)
    v = both_reject(corpus, rel, exp + ["Extra"])
    assert any("genres read back" in p for p in v.problems)


def test_verify_rejects_unchanged_file_when_change_expected(corpus):
    rel = Z + "flac_single_genre.flac"
    both_reject(corpus, rel, ["Rock", "Jazz"])


def test_verify_rejects_dropped_frame(corpus):
    rel = Z + "mp3_v24_full.mp3"
    exp = mutated(corpus, rel)
    rewrite_id3(corpus.path(rel), lambda fr: [(f, b) for f, b in fr if f != "UFID"])
    v = both_reject(corpus, rel, exp)
    assert any("frames" in p for p in v.problems)


def test_verify_rejects_apic_type_change(corpus):
    rel = Z + "mp3_v24_full.mp3"
    exp = mutated(corpus, rel)

    def fn(frames):
        out = []
        for f, b in frames:
            if f == "APIC" and b"image/png" in b[:12]:
                mime_end = b.index(b"\0", 1)
                desc_end = b.index(b"\0", mime_end + 2)
                b = id3_apic_body(b[desc_end + 1:], "image/png", 5, "back", 3)
            out.append((f, b))
        return out
    rewrite_id3(corpus.path(rel), fn)
    both_reject(corpus, rel, exp)


def test_verify_rejects_id3v1_and_ape_changes(corpus):
    rel = Z + "mp3_v24_full.mp3"
    exp = mutated(corpus, rel)
    p = corpus.path(rel)
    data = bytearray(p.read_bytes())
    data[-125:-120] = b"XXXXX"
    p.write_bytes(bytes(data))
    assert any("raw" in x for x in both_reject(corpus, rel, exp).problems)
    data[-125:-120] = corpus.master.joinpath(rel).read_bytes()[-125:-120]
    p.write_bytes(bytes(data).replace(b"120,210", b"120,211"))
    assert any("raw" in x for x in both_reject(corpus, rel, exp).problems)


def test_verify_rejects_joined_ogg_genres(corpus):
    rel = Z + "ogg_vorbis_multi_genre.ogg"
    set_ogg_genres(corpus.path(rel), ["Electronic", "Ambient", "Jazz"], single_field=True)
    both_reject(corpus, rel, ["Electronic", "Ambient", "Jazz"])


def test_verify_rejects_dropped_flac_picture(corpus):
    rel = Z + "flac_two_pictures_no_padding.flac"
    exp = mutated(corpus, rel)
    subprocess.run(["metaflac", "--remove", "--block-type=PICTURE", str(corpus.path(rel))], check=True)
    both_reject(corpus, rel, exp)


def test_verify_rejects_vorbis_key_case_change(corpus):
    rel = Z + "flac_multi_genre.flac"
    exp = mutated(corpus, rel)
    subprocess.run(["metaflac", "--remove-tag=CUSTOM_UNRELATED", "--set-tag=custom_unrelated=keep me ✓ ünïcødé",
                    str(corpus.path(rel))], check=True)
    both_reject(corpus, rel, exp)


def test_verify_rejects_dropped_freeform(corpus):
    rel = Z + "m4a_aac_moov_after_mdat.m4a"
    exp = mutated(corpus, rel)
    mp4_replace_items(corpus.path(rel), lambda k: k != b"----", [])
    both_reject(corpus, rel, exp)


def test_verify_rejects_leftover_gnre(corpus):
    rel = Z + "m4a_gnre.m4a"
    mp4_replace_items(corpus.path(rel), lambda k: True, [mp4_text_item(b"\xa9gen", ["Rock", "Jazz"])])
    both_reject(corpus, rel, ["Rock", "Jazz"])


@pytest.mark.parametrize("rel,offset", [(Z + "mp3_v24_full.mp3", -3000), (Z + "m4a_aac_moov_after_mdat.m4a", 300),
                                        (Z + "flac_single_genre.flac", -500)])
def test_verify_rejects_audio_damage(corpus, rel, offset):
    exp = mutated(corpus, rel)
    p = corpus.path(rel)
    data = bytearray(p.read_bytes())
    data[offset] ^= 0x55
    p.write_bytes(bytes(data))
    v = both_reject(corpus, rel, exp)
    assert v.audio == "failed" or any("mdat" in x for x in v.problems)


def test_verify_rejects_unreadable_temp(corpus):
    rel = Z + "flac_single_genre.flac"
    corpus.path(rel).write_bytes(b"\0" * 100)
    v = verify_temp(corpus.master / rel, corpus.path(rel), ["Rock"])
    assert not v.ok and "unreadable" in v.problems[0]


def test_verify_audio_modes(corpus, monkeypatch):
    rel = Z + "flac_single_genre.flac"
    exp = mutated(corpus, rel)
    assert verify_temp(corpus.master / rel, corpus.path(rel), exp, audio="off").audio == "not_checked"
    monkeypatch.setattr(verify_mod.shutil, "which", lambda name: None)
    v = verify_temp(corpus.master / rel, corpus.path(rel), exp)
    assert v.ok and v.audio == "not_checked"


def test_snapshot_compare_requires_exact_upgrade_conversion():
    before = {"format": "mp3", "stream": {}, "pictures": [], "raw": {},
              "tags": {"id3_version": "2.3", "frames": [["TIT2", "a"], ["TYER", "y"], ["TDAT", "d"]],
                       "unknown_frames": [], "raw_frame_ids": ["TDAT", "TIT2", "TYER"],
                       "converted": {"TYER": ["1972"], "TDAT": ["1503"]}}}
    good = {**before, "tags": {"id3_version": "2.4", "frames": [["TDRC", "x"], ["TIT2", "a"]], "unknown_frames": [],
                               "raw_frame_ids": ["TDRC", "TIT2"], "converted": {"TDRC": ["1972-03-15"]}}}
    assert verify_mod.compare_snapshots(before, good)[0] == []
    wrong_date = {**good, "tags": {**good["tags"], "converted": {"TDRC": ["1972"]}}}
    assert verify_mod.compare_snapshots(before, wrong_date)[0]
    lost = {**good, "tags": {**good["tags"], "frames": [["TDRC", "x"]], "raw_frame_ids": ["TDRC"]}}
    assert verify_mod.compare_snapshots(before, lost)[0]
    downgrade = {**before, "tags": {**before["tags"], "id3_version": "2.2"}}
    assert verify_mod.compare_snapshots(before, downgrade)[0]


# --------------------------------------------------------------------------- mutate child

def test_mutate_refuses_when_genres_changed_since_planning(corpus):
    rel = Z + "flac_single_genre.flac"
    before = corpus.path(rel).read_bytes()
    m = run_mutate(corpus.path(rel), "flac", ["Rock", "Jazz"], ["Blues"])
    assert not m.ok and "planned from" in m.error
    assert corpus.path(rel).read_bytes() == before


@pytest.mark.parametrize("rel,fmt", [(Z + "flac_mixed_style.flac", "flac"), (Z + "mp3_v22.mp3", "mp3"),
                                     (Z + "flac_single_genre.flac", "mp3")])
def test_mutate_refuses_policy_or_format_mismatch(corpus, rel, fmt):
    r = read_file(corpus.path(rel))
    before = corpus.path(rel).read_bytes()
    m = run_mutate(corpus.path(rel), fmt, ["X"], list(r.genre.values))
    assert not m.ok
    assert corpus.path(rel).read_bytes() == before


def test_mutate_timeout_kills_child(corpus):
    import time
    t0 = time.monotonic()
    m = run_mutate(corpus.path(Z + "flac_single_genre.flac"), "flac", ["X"], ["Rock"], timeout=1.5,
                   hook="tests.safewrite.mutate_hooks:sleepy", extra_sys_path=[BACKEND])
    assert not m.ok and m.error.startswith("timeout") and time.monotonic() - t0 < 15


def test_mutate_crash_is_reported_and_verify_rejects_the_temp(corpus):
    rel = Z + "flac_single_genre.flac"
    m = run_mutate(corpus.path(rel), "flac", ["X"], ["Rock"], hook="tests.safewrite.mutate_hooks:crashy",
                   extra_sys_path=[BACKEND])
    assert not m.ok and "exit 7" in m.error
    assert not verify_temp(corpus.master / rel, corpus.path(rel), ["X"]).ok


def test_mutate_start_failure_is_contained(corpus, monkeypatch):
    from gama.safewrite import mutate as mutate_mod

    def boom(*a, **kw):
        raise OSError(12, "Cannot allocate memory")
    monkeypatch.setattr(mutate_mod.subprocess, "Popen", boom)
    m = run_mutate(corpus.path(Z + "flac_single_genre.flac"), "flac", ["X"], ["Rock"])
    assert not m.ok and "cannot start writer" in m.error


def test_parent_never_imports_mutagen(corpus, tmp_path):
    src = Path(BACKEND) / "src"
    target = tmp_path / "x.flac"
    shutil.copyfile(corpus.path(Z + "flac_single_genre.flac"), target)
    code = (f"import sys; sys.path.insert(0, {str(src)!r})\n"
            "from gama.safewrite.mutate import run_mutate\nfrom gama.safewrite.verify import verify_temp\n"
            f"m = run_mutate({str(target)!r}, 'flac', ['Rock', 'Jazz'], ['Rock'])\nassert m.ok, m.error\n"
            "assert 'mutagen' not in sys.modules and not any(k.startswith('django') for k in sys.modules)\n")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
