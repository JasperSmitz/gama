"""Acceptance scenarios for the first bulk genre editing workflow, driven through the
normal web entry point on disposable fixtures (never a real library):

- add/remove one genre across several selected albums, other genres preserved;
- mixed genres across tracks (only the tracks that need it change);
- a no-op (nothing to write);
- releases with the same title (album IDs decide what is edited);
- a blocked file shown with its reason before anything is written;
- a stale preview (fresh preview required, nothing written);
- a mid-batch failure (stop, report changed / failed / pending, roll back);
- restore from the manifest, refusing files edited since the batch.
"""
import os
import subprocess

import pytest

from gama.edits.models import EditJob, JobItem
from gama.library.models import Album, Track
from gama.metadata.reader import read_file
from gama.safewrite import fsutil
from verify.verify import compare_tree

from .test_workflow import (GEN, KC, YES, _no_failpoints, applier, confirm, hashes, lib,  # noqa: F401
                            preview, select, work_one)

pytestmark = [pytest.mark.slow, pytest.mark.django_db]

EXAMPLES = (KC, GEN, YES)
MIXED = "Mixed Bag"


def plan_rows(plan):
    return {i.relpath: (i.action, i.genres_before, i.genres_after, i.reason) for i in plan.items.all()}


def disk_genres(corpus, relpath):
    return list(read_file(corpus.path(relpath)).genre.values)


# --------------------------------------------------------------------------- add / remove / no-op

def test_remove_then_add_progressive_rock_across_three_albums(applier, corpus):
    select(applier, *EXAMPLES)

    removed = preview(applier, "remove", ["Progressive Rock"])
    page = applier.get(f"/edits/plans/{removed.id}/").content.decode()
    # Grouped by album, in selection order, every track listed with current and proposed values.
    assert page.index(KC) < page.index(GEN) < page.index(YES)
    assert (removed.n_change, removed.n_nochange, removed.n_skip) == (9, 0, 0)
    after = {r: v[2] for r, v in plan_rows(removed).items()}
    assert all(after[r] == ["Art Rock"] for r in after if r.startswith("King Crimson/"))
    assert all(after[r] == ["Symphonic Rock"] for r in after if r.startswith("Genesis/"))
    assert all(after[r] == ["Rock"] for r in after if r.startswith("Yes/"))  # ';'-joined value keeps its style
    before_hashes = hashes(corpus.root)
    assert hashes(corpus.root) == before_hashes  # preview is read-only

    confirm(applier, removed)
    job = work_one()
    assert job.status == "succeeded"
    expected = {i.relpath: i.genres_after for i in removed.items.filter(action="change")}
    assert compare_tree(corpus.master, corpus.root, expected, check_audio=False) == {}
    for rel, g in expected.items():
        assert disk_genres(corpus, rel) == g == Track.objects.get(file__relpath=rel).genres  # index from disk

    added = preview(applier, "add", ["Progressive Rock"])
    assert (added.n_change, added.n_nochange) == (9, 0)
    confirm(applier, added)
    assert work_one().status == "succeeded"
    assert all("Progressive Rock" in disk_genres(corpus, r) or disk_genres(corpus, r) == ["Rock;Progressive Rock"]
               for r in expected)

    # No-op: adding a genre that is present (any case) writes nothing and cannot be confirmed.
    noop = preview(applier, "add", ["progressive rock"])
    assert (noop.n_change, noop.n_nochange) == (0, 9)
    snapshot = hashes(corpus.root)
    assert "Apply" not in applier.get(f"/edits/plans/{noop.id}/").content.decode()
    assert confirm(applier, noop).status_code in (302, 400)
    assert not EditJob.objects.filter(plan=noop).exists() and hashes(corpus.root) == snapshot


def test_mixed_genres_across_tracks(applier, corpus):
    applier.post("/library/basket/add/", {"album": list(Album.objects.filter(album=MIXED).values_list("id", flat=True))})
    add = plan_rows(preview(applier, "add", ["Fusion"]))
    by_track = {r.rsplit("/", 1)[1]: v for r, v in add.items()}
    assert by_track["01 Mixed 1.flac"][:3] == ("change", ["Jazz"], ["Jazz", "Fusion"])
    assert by_track["02 Mixed 2.flac"][0] == "nochange"  # already has it: skipped
    assert by_track["03 Mixed 3.flac"][:3] == ("change", [], ["Fusion"])

    rm = preview(applier, "remove", ["Fusion"])
    rows = {r.rsplit("/", 1)[1]: v for r, v in plan_rows(rm).items()}
    assert rows["02 Mixed 2.flac"][:3] == ("change", ["Jazz", "Fusion"], ["Jazz"])
    assert {k for k, v in rows.items() if v[0] == "nochange"} == {"01 Mixed 1.flac", "03 Mixed 3.flac",
                                                                   "04 Mixed 4.flac"}  # absent: skipped
    confirm(applier, rm)
    assert work_one().status == "succeeded"
    expected = {i.relpath: i.genres_after for i in rm.items.filter(action="change")}
    assert compare_tree(corpus.master, corpus.root, expected, check_audio=False) == {}


def test_album_ids_decide_between_releases_with_the_same_title(applier, corpus):
    a, b = Album.objects.filter(album="Greatest Hits").order_by("albumartist")
    applier.post("/library/basket/add/", {"album": [a.id]})
    plan = preview(applier, "add", ["Synth-Pop"])
    assert plan.n_change == 2 and all(r.startswith("Gama Duplicate A/") for r in plan_rows(plan))
    confirm(applier, plan)
    work_one()
    assert all(disk_genres(corpus, f"Gama Duplicate B/Greatest Hits/{n:02d} Hit {n}.ogg") == ["Pop"] for n in (1, 2))


# --------------------------------------------------------------------------- blocked / stale

def test_blocked_file_is_shown_with_reason_before_writing(applier, corpus, lib):
    blocked = "King Crimson/1969 - In the Court of the Crimson King/02 I Talk to the Wind.flac"
    os.link(corpus.path(blocked), corpus.root.parent / "outside-link.flac")  # replacing it would split the links
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    action, _, _, reason = plan_rows(plan)[blocked]
    assert (action, reason) == ("skip", "hardlink")
    page = applier.get(f"/edits/plans/{plan.id}/").content.decode()
    assert "hardlink" in page and "hard links" in page and plan.n_skip == 1
    before = corpus.path(blocked).read_bytes()
    confirm(applier, plan)
    assert work_one().status == "succeeded"
    assert corpus.path(blocked).read_bytes() == before


def test_stale_preview_requires_a_fresh_one(applier, corpus):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    target = plan.items.order_by("seq")[2].relpath
    subprocess.run(["metaflac", "--set-tag=COMMENT=edited elsewhere", str(corpus.path(target))], check=True)
    confirm(applier, plan)
    snapshot = hashes(corpus.root)
    job = work_one()
    assert job.status == "conflict" and hashes(corpus.root) == snapshot  # no file written, not even the others
    assert "Refresh preview" in applier.get(f"/edits/jobs/{job.id}/").content.decode()


# --------------------------------------------------------------------------- failure and restore

def test_mid_batch_failure_stops_reports_and_rolls_back(applier, corpus):
    select(applier, KC, GEN)
    plan = preview(applier, "add", ["Jazz"])
    confirm(applier, plan)
    rows = list(plan.items.filter(action="change").order_by("seq"))
    target = corpus.path(rows[2].relpath)
    fsutil.set_failpoint("item_start", lambda ctx: os.chmod(target, 0o444) if ctx["n"] == 2 else None)
    try:
        job = work_one()
    finally:
        os.chmod(target, 0o644)
    assert job.status == "rolled_back"
    items = list(JobItem.objects.filter(job=job).order_by("plan_item__seq"))
    assert [(i.state, i.restore_state) for i in items[:3]] == [("committed", "restored"), ("committed", "restored"),
                                                               ("failed", "")]
    assert all(i.state == "skipped" for i in items[3:])  # stopped: the rest was never attempted
    page = applier.get(f"/edits/jobs/{job.id}/").content.decode()
    assert "2 changed and rolled back" in page and "1 failed" in page and f"{len(items) - 3} not attempted" in page
    assert "not-writable" in page
    assert compare_tree(corpus.master, corpus.root, check_audio=False, check_mtime=False) == {}


def test_restore_from_manifest_refuses_files_edited_since(applier, corpus):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    confirm(applier, plan)
    job = work_one()
    edited = plan.items.order_by("seq")[0].relpath
    subprocess.run(["metaflac", "--set-tag=COMMENT=edited after the batch", str(corpus.path(edited))], check=True)
    edited_bytes = corpus.path(edited).read_bytes()

    r = applier.post(f"/edits/jobs/{job.id}/restore/", {"scope": "all"})
    from gama.edits.models import EditPlan
    rp = EditPlan.objects.get(id=r["Location"].rstrip("/").split("/")[-1])
    rows = plan_rows(rp)
    assert rows[edited][0] == "conflict" and (rp.n_change, rp.n_skip) == (2, 1)
    confirm(applier, rp)
    assert work_one().status == "succeeded"
    assert corpus.path(edited).read_bytes() == edited_bytes  # not overwritten
    for rel in rows:
        if rel != edited:
            assert corpus.path(rel).read_bytes() == (corpus.master / rel).read_bytes()  # byte-identical original
            assert Track.objects.get(file__relpath=rel).genres == disk_genres(corpus, rel)  # index refreshed
