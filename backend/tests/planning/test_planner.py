"""Planner (pure Python) on the synthetic corpus. Nothing here writes through Gama."""
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fixtures.build import ogg_set_comments
from gama.metadata.reader import read_file
from gama.planning.planner import FileRef, PlanError, digest, plan_files
from verify.verify import ogg_parse

pytestmark = pytest.mark.slow

KC = "King Crimson/1969 - In the Court of the Crimson King"
GEN = "Genesis/1973 - Selling England by the Pound"
YES = "Yes/1972 - Close to the Edge"
INC = "Gama Inconsistent/Mixed Bag"


def inproc(paths):
    return [read_file(p) for p in paths]


def refs(corpus, *dirs):
    out = []
    for d in dirs:
        for rel in sorted(r for r in corpus.manifest if r.startswith(d + "/")):
            out.append(FileRef(str(corpus.root), rel, album_key=d, album_label=d.split("/")[-1]))
    return out


def plan(corpus, op, genres, *dirs, **kw):
    kw.setdefault("reader", inproc)
    return plan_files(refs(corpus, *dirs), op, genres, **kw)


def actions(p):
    return {i.relpath: i.action for i in p.items}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_example_albums_add_existing_genre(corpus):
    p = plan(corpus, "add", ["Progressive Rock"], KC, GEN, YES)
    assert set(actions(p).values()) == {"nochange"}
    assert (p.n_change, p.n_nochange, p.n_skip) == (0, 9, 0)
    assert all("already-present" in i.warnings for i in p.items)


def test_example_albums_add_new_genre(corpus):
    p = plan(corpus, "add", ["Symphonic Rock"], KC, GEN, YES)
    a = actions(p)
    assert [a[r] for r in a if r.startswith(KC)] == ["change"] * 3
    assert [a[r] for r in a if r.startswith(GEN)] == ["nochange"] * 3
    yes = [i for i in p.items if i.relpath.startswith(YES)]
    assert all(i.genres_after == ["Rock;Progressive Rock;Symphonic Rock"] for i in yes)
    assert all(i.style == "delimited" and i.components_after[-1] == "Symphonic Rock" for i in yes)
    kc = next(i for i in p.items if i.relpath.startswith(KC))
    assert kc.genres_before == ["Progressive Rock", "Art Rock"]
    assert kc.genres_after == ["Progressive Rock", "Art Rock", "Symphonic Rock"]
    assert (p.n_change, p.n_nochange) == (6, 3)


def test_items_carry_fingerprint(corpus):
    p = plan(corpus, "add", ["Jazz"], KC)
    for i in p.items:
        path = corpus.root / i.relpath
        st = os.stat(path)
        assert i.sha_before == sha(path)
        assert (i.size, i.mtime_ns, i.inode) == (st.st_size, st.st_mtime_ns, st.st_ino)
        assert i.format == "flac" and i.root == str(corpus.root)
    assert [i.seq for i in p.items] == [0, 1, 2]


def test_remove_nonexistent_genre(corpus):
    p = plan(corpus, "remove", ["Polka"], KC, GEN, YES)
    assert p.n_change == 0 and all("not-present" in i.warnings for i in p.items)


def test_remove_from_delimited_album(corpus):
    p = plan(corpus, "remove", ["rock"], YES)
    assert all(i.action == "change" and i.genres_after == ["Progressive Rock"] for i in p.items)


def test_inconsistent_album(corpus):
    p = plan(corpus, "add", ["Jazz"], INC)
    by = {Path(i.relpath).name: i for i in p.items}
    assert by["01 Mixed 1.flac"].action == "nochange"
    assert by["02 Mixed 2.flac"].action == "nochange"
    assert by["03 Mixed 3.flac"].action == "change" and by["03 Mixed 3.flac"].genres_after == ["Jazz"]
    m4 = by["04 Mixed 4.flac"]
    assert m4.action == "nochange" and m4.genres_before == ["jazz", "Jazz"] and "duplicate-genres" in m4.warnings
    groups = p.by_album()
    assert list(groups) == [INC] and [i.seq for i in groups[INC]] == [0, 1, 2, 3]
    r = plan(corpus, "remove", ["JAZZ"], INC)
    assert [i.genres_after for i in r.items] == [[], ["Fusion"], [], []]
    assert [i.action for i in r.items] == ["change", "change", "nochange", "change"]


def test_grouping_across_albums_keeps_selection_order(corpus):
    p = plan(corpus, "add", ["Jazz"], YES, KC)
    assert list(p.by_album()) == [YES, KC]
    assert all(len(v) == 3 for v in p.by_album().values())


def test_multi_disc_album(corpus):
    root = str(corpus.root)
    rels = sorted(r for r in corpus.manifest if r.startswith("Gama Test Orchestra/Two Disc Set/"))
    p = plan_files([FileRef(root, r, "two-disc") for r in rels], "add", ["Baroque"], reader=inproc)
    assert p.n_change == 4 and {Path(i.relpath).parent.name for i in p.items} == {"CD1", "CD2"}


def test_unsupported_and_corrupt_files_are_skipped_with_reasons(corpus):
    p = plan(corpus, "add", ["Jazz"], "Format Zoo", "Broken")
    expected = {r: read_file(corpus.root / r) for r in corpus.manifest if r.startswith(("Format Zoo/", "Broken/"))}
    for i in p.items:
        rr = expected[i.relpath]
        if rr.writable:
            assert i.action in ("change", "nochange"), i.relpath
        else:
            assert i.action == "skip" and i.reason == (rr.skip_reason or rr.reason), i.relpath
            assert i.genres_after is None and i.components_after is None
    assert p.n_change + p.n_nochange + p.n_skip == len(p.items) == len(expected)
    skipped = {Path(i.relpath).name: i.reason for i in p.items if i.action == "skip"}
    assert skipped["unsupported.wav"] == "unsupported-format"
    assert skipped["garbage.flac"] == "unreadable"
    assert skipped["flac_mixed_style.flac"] == "mixed-genre-style"
    assert skipped["actually_flac.mp3"] == "extension-mismatch"
    assert skipped["zero_bytes.mp3"] == "unreadable"
    assert p.n_skip >= 9


def test_warnings_carried_into_items(corpus):
    p = plan(corpus, "add", ["Jazz"], "Format Zoo")
    by = {Path(i.relpath).name: i for i in p.items}
    assert "id3v2.3-upgrade" in by["mp3_v23_slash_genre.mp3"].warnings
    assert "apev2-genre" in by["mp3_v24_full.mp3"].warnings
    assert "no-id3-tag" in by["mp3_untagged.mp3"].warnings
    assert by["mp3_untagged.mp3"].action == "change"


def test_numeric_reference_replace_warns(corpus):
    root = str(corpus.root)
    p = plan_files([FileRef(root, "Format Zoo/mp3_v23_numeric_genre.mp3")], "replace", ["Rock", "Pop"], reader=inproc)
    i = p.items[0]
    assert i.genres_before == ["(17)"] and i.components_before == ["Rock"]
    assert i.genres_after == ["Rock", "Pop"]
    assert "numeric-reference-stored-as-name" in i.warnings and "numeric-genre-reference" in i.warnings


def test_planning_never_writes(corpus):
    before = {p: sha(p) for p in corpus.root.rglob("*") if p.is_file()}
    plan_files([FileRef(str(corpus.root), r) for r in sorted(corpus.manifest)], "replace", ["Jazz"])
    plan(corpus, "remove", ["Rock"], KC, YES)
    assert {p: sha(p) for p in corpus.root.rglob("*") if p.is_file()} == before


def test_default_reader_uses_child_processes(corpus):
    p = plan_files(refs(corpus, KC), "add", ["Jazz"])  # readproc
    q = plan(corpus, "add", ["Jazz"], KC)               # in-process
    assert p.to_dict() == q.to_dict()


def test_plan_is_json_serialisable(corpus):
    p = plan(corpus, "add", ["Jazz"], KC, "Broken")
    d = json.loads(json.dumps(p.to_dict()))
    assert d["digest"] == p.digest and len(d["items"]) == len(p.items)


# --------------------------------------------------------------------------- digest

def test_digest_is_deterministic(corpus):
    assert plan(corpus, "add", ["Jazz"], KC).digest == plan(corpus, "add", ["Jazz"], KC).digest


def test_digest_changes_with_request(corpus):
    a = plan(corpus, "add", ["Jazz"], KC).digest
    assert plan(corpus, "add", ["Fusion"], KC).digest != a
    assert plan(corpus, "remove", ["Jazz"], KC).digest != a


def test_digest_changes_when_a_file_changes(corpus):
    a = plan(corpus, "add", ["Jazz"], KC)
    f = corpus.root / a.items[1].relpath
    subprocess.run(["metaflac", "--set-tag=COMMENT=edited elsewhere", str(f)], check=True)
    b = plan(corpus, "add", ["Jazz"], KC)
    assert b.digest != a.digest and b.items[1].sha_before != a.items[1].sha_before
    assert [x.sha_before for x in b.items][::2] == [x.sha_before for x in a.items][::2]


@pytest.mark.parametrize("fieldname,value", [("action", "skip"), ("sha_before", "0" * 64), ("genres_after", ["X"]),
                                             ("relpath", "other.flac"), ("warnings", ["x"]), ("mtime_ns", 1)])
def test_digest_covers_every_item_field(corpus, fieldname, value):
    p = plan(corpus, "add", ["Jazz"], KC)
    items = [dataclasses.replace(i) for i in p.items]
    setattr(items[2], fieldname, value)
    assert digest(p.operation, p.genres, items, p.warnings) != p.digest


# --------------------------------------------------------------------------- safety checks

def test_file_changed_while_planning(corpus):
    target = refs(corpus, KC)[0].path

    def reader(paths):
        res = inproc(paths)
        with open(target, "ab") as f:  # someone else writes after we read
            f.write(b"\0")
        return res
    p = plan(corpus, "add", ["Jazz"], KC, reader=reader)
    assert p.items[0].action == "skip" and p.items[0].reason == "changed-while-planning"
    assert [i.action for i in p.items[1:]] == ["change", "change"]


def test_outside_root_is_skipped(corpus, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "x.flac").write_bytes((corpus.root / refs(corpus, KC)[0].relpath).read_bytes())
    lib = corpus.root / "King Crimson"
    p = plan_files([FileRef(str(lib), "../../elsewhere/x.flac")], "add", ["Jazz"], reader=inproc)
    assert p.items[0].action == "skip" and p.items[0].reason == "outside-root"
    (lib / "escape").symlink_to(other)
    p = plan_files([FileRef(str(lib), "escape/x.flac")], "add", ["Jazz"], reader=inproc)
    assert p.items[0].reason == "outside-root"


def test_unindexed_audio_warning(corpus):
    rs = refs(corpus, KC)
    p = plan_files(rs[:2], "add", ["Jazz"], reader=inproc)
    assert len(p.warnings) == 1 and p.warnings[0].startswith("unindexed-audio:") and "1 audio file" in p.warnings[0]
    assert plan_files(rs, "add", ["Jazz"], reader=inproc).warnings == []
    d = corpus.root / KC
    (d / ".gama-tmp-job-0-x.flac").write_bytes(b"")
    (d / "cover.jpg").write_bytes(b"")
    assert plan_files(rs, "add", ["Jazz"], reader=inproc).warnings == []


@pytest.mark.parametrize("op,genres", [("add", ["Rock;Pop"]), ("add", []), ("replace", []), ("move", ["x"])])
def test_bad_request(corpus, op, genres):
    with pytest.raises(PlanError):
        plan(corpus, op, genres, KC)


def test_empty_duplicate_and_too_large_selection(corpus):
    with pytest.raises(PlanError):
        plan_files([], "add", ["Jazz"], reader=inproc)
    rs = refs(corpus, KC)
    with pytest.raises(PlanError, match="twice"):
        plan_files(rs + rs[:1], "add", ["Jazz"], reader=inproc)
    with pytest.raises(PlanError, match="at most 2"):
        plan_files(rs, "add", ["Jazz"], reader=inproc, max_files=2)


def test_reader_mismatch_is_refused(corpus):
    with pytest.raises(PlanError, match="do not match"):
        plan(corpus, "add", ["Jazz"], KC, reader=lambda paths: inproc(paths[::-1]))


# --------------------------------------------------------------------------- repeated operations on disk

def test_repeated_operation_plans_zero_changes(corpus):
    """Write each planned result with raw tools, re-plan: nothing left to do."""
    for op, genres in (("add", ["Symphonic Rock"]), ("remove", ["art rock"]), ("replace", ["Jazz", "Fusion"])):
        p = plan(corpus, op, genres, KC, "Gama Duplicate A/Greatest Hits")
        for i in p.items:
            if i.action != "change":
                continue
            path = corpus.root / i.relpath
            if i.format == "flac":
                subprocess.run(["metaflac", "--remove-tag=GENRE", *[f"--set-tag=GENRE={g}" for g in i.genres_after],
                                str(path)], check=True)
            else:
                cs = [c for c in ogg_parse(path.read_bytes())["comments"] if not c.upper().startswith("GENRE=")]
                ogg_set_comments(path, cs + [f"GENRE={g}" for g in i.genres_after])
        again = plan(corpus, op, genres, KC, "Gama Duplicate A/Greatest Hits")
        assert again.n_change == 0, (op, [(i.relpath, i.genres_before, i.genres_after) for i in again.items])
        assert [i.genres_before for i in again.items] == [
            i.genres_after if i.action == "change" else i.genres_before for i in p.items]


def test_planning_package_has_no_django():
    src = Path(__file__).resolve().parents[2] / "src"
    code = ("import sys; sys.path.insert(0, %r)\nimport gama.planning.planner\n"
            "assert not [m for m in sys.modules if m.split('.')[0] == 'django']\n") % str(src)
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
