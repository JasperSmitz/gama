"""M4 conformance: format writers checked by the independent oracle.

Every writable corpus file x add/remove/replace is planned with genres.apply,
mutated in the child (safewrite.mutate) on a copy, and must pass both the
oracle (tools/verify, no mutagen) and safewrite.verify with zero problems.
"""
import os
import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest

from gama.metadata.genres import apply
from gama.metadata.reader import read_file
from gama.safewrite.mutate import run_mutate
from gama.safewrite.verify import verify_temp
from verify.verify import ape_parse, compare, id3v1_parse, id3v2_parse, mp4_parse, ogg_parse, raw_genres

pytestmark = pytest.mark.slow

Z = "Format Zoo/"
NEW = "Gama Test Genre ✓"


def requests_for(g):
    first = g.components[0] if g.components else "Not There"
    return {"add": [NEW], "remove": [first], "replace": ["Jazz", "Fusion"]}


@pytest.fixture(scope="module")
def writable(corpus_master):
    out = {}
    for rel in sorted(corpus_master.manifest):
        r = read_file(corpus_master.master / rel)
        if r.writable:
            out[rel] = r
    return out


def test_corpus_has_writable_files_of_every_format(writable):
    assert {r.format for r in writable.values()} == {"flac", "mp3", "mp4", "ogg-vorbis", "ogg-opus"}
    assert len(writable) >= 40


@pytest.mark.parametrize("op", ["add", "remove", "replace"])
def test_conformance_matrix(corpus, writable, tmp_path, op):
    def one(item):
        n, (rel, r) = item
        res = apply(r.genre, op, requests_for(r.genre)[op])
        master = corpus.master / rel  # writable 0644 copy, same mode as the work file
        work = tmp_path / f"{n:03d}{os.path.splitext(rel)[1]}"
        shutil.copyfile(master, work)
        os.chmod(work, os.stat(master).st_mode & 0o7777)
        if not res.changed:
            return rel, "nochange", []
        m = run_mutate(work, r.format, res.values, r.genre.values)
        if not m.ok:
            return rel, "changed", [f"mutate failed: {m.error}"]
        problems = [f"oracle: {p}" for p in compare(master, work, "genres", list(res.values))]
        v = verify_temp(master, work, res.values)
        problems += [f"verify: {p}" for p in v.problems]
        if v.audio != "verified":
            problems.append(f"verify audio: {v.audio}")
        if v.genres_read_back != list(res.values):
            problems.append(f"read back {v.genres_read_back}")
        return rel, "changed", problems

    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as ex:
        results = list(ex.map(one, enumerate(writable.items())))
    failures = {rel: p for rel, _, p in results if p}
    assert failures == {}
    assert sum(1 for _, kind, _ in results if kind == "changed") >= 25


# --------------------------------------------------------------------------- targeted preservation checks

def write(corpus, rel, op, genres):
    p = corpus.path(rel)
    r = read_file(p)
    res = apply(r.genre, op, genres)
    assert res.changed
    m = run_mutate(p, r.format, res.values, r.genre.values)
    assert m.ok, m.error
    probs = compare(corpus.master / rel, p, "genres", list(res.values))
    assert probs == [], probs
    v = verify_temp(corpus.master / rel, p, res.values)
    assert v.ok, v.problems
    return r, read_file(p), res, v


@pytest.mark.parametrize("rel", [Z + "flac_two_pictures_no_padding.flac", Z + "mp3_v24_full.mp3",
                                 Z + "m4a_aac_moov_after_mdat.m4a", Z + "ogg_vorbis_multi_genre.ogg",
                                 Z + "opus_multi_genre.opus"])
def test_pictures_preserved(corpus, rel):
    before, after, _, _ = write(corpus, rel, "add", [NEW])
    assert before.tags.pictures and after.tags.pictures == before.tags.pictures


def test_id3_frames_trailer_and_ape_preserved(corpus):
    rel = Z + "mp3_v24_full.mp3"
    data0 = corpus.path(rel).read_bytes()
    write(corpus, rel, "replace", ["Jazz"])
    data1 = corpus.path(rel).read_bytes()
    ids = [f for f, _ in id3v2_parse(data1)["frames"]]
    for fid in ("USLT", "UFID", "POPM", "TXXX", "APIC"):
        assert fid in ids
    assert ids.count("TXXX") == 2 and ids.count("APIC") == 2
    assert data1[-128:] == data0[-128:]                      # ID3v1 byte-identical
    assert id3v1_parse(data1)["genre"] == id3v1_parse(data0)["genre"]
    assert ape_parse(data1)["raw"] == ape_parse(data0)["raw"]  # APEv2 byte-identical


def test_id3v23_upgraded_with_only_date_conversion(corpus):
    rel = Z + "mp3_v23_slash_genre.mp3"
    before, after, _, v = write(corpus, rel, "add", ["Jazz"])
    t0 = id3v2_parse(corpus.master.joinpath(rel).read_bytes())
    t1 = id3v2_parse(corpus.path(rel).read_bytes())
    assert (t0["version"], t1["version"]) == (3, 4)
    ids0 = sorted(f for f, _ in t0["frames"] if f != "TCON")
    ids1 = sorted(f for f, _ in t1["frames"] if f != "TCON")
    assert sorted(set(ids0) - set(ids1)) == ["TDAT", "TYER"] and sorted(set(ids1) - set(ids0)) == ["TDRC"]
    assert after.tags.snapshot["tags"]["converted"]["TDRC"] == ["1972-03-15"]
    assert raw_genres(corpus.path(rel)) == ["Rock/Post-Punk", "Jazz"]
    assert "ID3v2.3 upgraded to v2.4" in v.notes


def test_numeric_reference_kept_as_stored(corpus):
    write(corpus, Z + "mp3_v23_numeric_genre.mp3", "add", ["Pop"])
    assert raw_genres(corpus.path(Z + "mp3_v23_numeric_genre.mp3")) == ["(17)", "Pop"]


def test_untagged_mp3_gets_tag_with_only_tcon(corpus):
    rel = Z + "mp3_untagged.mp3"
    _, _, _, v = write(corpus, rel, "add", ["Jazz"])
    data = corpus.path(rel).read_bytes()
    t = id3v2_parse(data)
    assert t["version"] == 4 and [f for f, _ in t["frames"]] == ["TCON"]
    assert id3v1_parse(data) is None
    assert "new ID3v2.4 tag created" in v.notes


def test_vorbis_key_case_and_order_preserved(corpus):
    rel = Z + "flac_multi_genre.flac"
    before = read_file(corpus.path(rel)).tags.snapshot["tags"]["comments"]
    write(corpus, rel, "remove", ["blues"])
    after = read_file(corpus.path(rel))
    assert after.tags.snapshot["tags"]["comments"] == before
    assert after.tags.native["genre_keys"] == ["genre"]
    assert after.genre.values == ("Rock", "Rock")
    comments = [k for k, _ in after.tags.snapshot["tags"]["comments"]]
    assert "REPLAYGAIN_TRACK_GAIN" in comments and "CUSTOM_UNRELATED" in comments


def test_genre_fields_stay_in_place(corpus):
    from verify.verify import flac_parse
    rel = Z + "flac_single_genre.flac"
    before = [c.split("=")[0] for c in flac_parse(corpus.path(rel).read_bytes())["comments"]]
    write(corpus, rel, "add", ["Jazz"])
    after = [c.split("=")[0] for c in flac_parse(corpus.path(rel).read_bytes())["comments"]]
    i = before.index("GENRE")
    assert after[:i] == before[:i] and after[i:i + 2] == ["GENRE", "GENRE"] and after[i + 2:] == before[i + 1:]


@pytest.mark.parametrize("rel", [Z + "ogg_vorbis_multi_genre.ogg", Z + "opus_multi_genre.opus"])
def test_ogg_genres_are_separate_fields_and_tail_kept(corpus, rel):
    tail0 = ogg_parse(corpus.path(rel).read_bytes())["comment_tail"]
    write(corpus, rel, "add", ["Jazz"])
    p = ogg_parse(corpus.path(rel).read_bytes())
    assert [c for c in p["comments"] if c.upper().startswith("GENRE=")] == [
        "GENRE=Electronic", "GENRE=Ambient", "GENRE=Jazz"]
    assert p["comment_tail"] == tail0


def test_delimited_m4a_stays_single_delimited_value(corpus):
    rel = Z + "m4a_delimited.m4a"
    write(corpus, rel, "add", ["Jazz"])
    assert raw_genres(corpus.path(rel)) == ["Rock;Progressive Rock;Jazz"]


def test_multi_data_m4a_stays_multi_data(corpus):
    rel = Z + "m4a_multi_data.m4a"
    write(corpus, rel, "add", ["Jazz"])
    assert raw_genres(corpus.path(rel)) == ["Rock", "Progressive Rock", "Jazz"]


def test_gnre_becomes_gen(corpus):
    rel = Z + "m4a_gnre.m4a"
    write(corpus, rel, "add", ["Jazz"])
    assert raw_genres(corpus.path(rel)) == ["Rock", "Jazz"]
    assert b"gnre" not in corpus.path(rel).read_bytes()


def test_freeform_atoms_preserved(corpus):
    rel = Z + "m4a_aac_moov_after_mdat.m4a"
    keys0 = sorted(k for k, _ in mp4_parse(corpus.path(rel).read_bytes())["items"])
    write(corpus, rel, "replace", ["Blues"])
    keys1 = sorted(k for k, _ in mp4_parse(corpus.path(rel).read_bytes())["items"])
    assert keys0 == keys1 and any(k.startswith("----") for k in keys1)


@pytest.mark.parametrize("rel", [Z + "m4a_aac_faststart.m4a", Z + "m4a_alac.m4a", Z + "flac_single_genre.flac"])
def test_audio_packets_unchanged(corpus, rel):
    _, _, _, v = write(corpus, rel, "add", ["A genre long enough to grow the tag " * 6])
    assert v.audio == "verified"


def test_remove_last_genre_deletes_field(corpus):
    rel = Z + "flac_single_genre.flac"
    write(corpus, rel, "remove", ["rock"])
    assert raw_genres(corpus.path(rel)) == []
    assert read_file(corpus.path(rel)).genre.style == "empty"


def test_v24_tag_is_not_upgraded_again(corpus, tmp_path):
    """NOTE 11: update_to_v24 runs only for 2.3 tags; v2.3-style frames in a 2.4 tag survive."""
    from fixtures.build import id3_text_body, id3v2_tag
    audio = corpus.path(Z + "mp3_untagged.mp3").read_bytes()
    rel = "v24_with_tyer.mp3"
    frames = [("TIT2", id3_text_body(["T"], 3)), ("TYER", id3_text_body(["1972"], 3)),
              ("TDAT", id3_text_body(["1503"], 3)), ("TCON", id3_text_body(["Rock"], 3))]
    for root in (corpus.root, corpus.master):
        (root / rel).write_bytes(id3v2_tag(frames, 4) + audio)
    write(corpus, rel, "add", ["Jazz"])
    ids = [f for f, _ in id3v2_parse(corpus.path(rel).read_bytes())["frames"]]
    assert "TYER" in ids and "TDAT" in ids and "TDRC" not in ids
