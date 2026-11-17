"""M7: select -> preview -> confirm -> apply -> results, with the worker run synchronously."""
import hashlib
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.utils import timezone

from gama.config import Config
from gama.edits import services
from gama.edits.models import Backup, EditJob, EditPlan, JobItem, PlanItem
from gama.library.models import Album, LibraryRoot, Track
from gama.library.scanner import scan_root
from gama.metadata.reader import read_file
from gama.planning.planner import FileRef, PlanError, plan_files
from gama.safewrite import fsutil
from verify.verify import compare_tree

pytestmark = [pytest.mark.slow, pytest.mark.django_db]

KC = "In the Court of the Crimson King"
GEN = "Selling England by the Pound"
YES = "Close to the Edge"
HERE = Path(__file__).resolve()


@pytest.fixture(autouse=True)
def _no_failpoints():
    fsutil.clear_failpoints()
    yield
    fsutil.clear_failpoints()


@pytest.fixture
def lib(corpus, settings, tmp_path):
    settings.GAMA = Config((corpus.root.resolve(),), tmp_path / "data", verify_audio="off")
    root = LibraryRoot.objects.create(path=str(corpus.root.resolve()), name="corpus")
    scan_root(root, reader=lambda ps: [read_file(p) for p in ps])
    return root


def _user(django_user_model, name, perm):
    u = django_user_model.objects.create_user(name, password="s3cret-pass")
    if perm:
        u.user_permissions.add(Permission.objects.get(codename="apply_edits"))
    return u


@pytest.fixture
def applier(client, django_user_model, lib):
    _user(django_user_model, "applier", True)
    client.login(username="applier", password="s3cret-pass")
    return client


@pytest.fixture
def viewer(client, django_user_model, lib):
    _user(django_user_model, "viewer", False)
    client.login(username="viewer", password="s3cret-pass")
    return client


def select(client, *albums):
    ids = [Album.objects.get(album=a).id for a in albums]
    client.post("/library/basket/add/", {"album": ids})
    return ids


def preview(client, op, genres):
    data = {"operation": op}
    if op == "replace":
        data.update(genres="\n".join(genres), replace_ack="yes")
    else:
        data["genre"] = genres[0]
    r = client.post("/edits/plans/new/", data)
    assert r.status_code == 302 and "/edits/plans/" in r["Location"], r.content[:500]
    return EditPlan.objects.get(id=r["Location"].rstrip("/").split("/")[-1])


def confirm(client, plan, **extra):
    data = {"digest": plan.digest, **extra}
    return client.post(f"/edits/plans/{plan.id}/confirm/", data)


def work_one():
    job = services.next_queued()
    assert job is not None and services.claim(job.id)
    job.refresh_from_db()
    return services.run_job(job)


def hashes(root):
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(root).rglob("*") if p.is_file()}


# --------------------------------------------------------------------------- full flow

def test_select_preview_confirm_apply_results(applier, corpus):
    select(applier, KC, GEN, YES)
    assert b"Add a genre" in applier.get("/edits/").content
    plan = preview(applier, "add", ["Symphonic Rock"])
    body = applier.get(f"/edits/plans/{plan.id}/").content.decode()
    for a in (KC, GEN, YES):
        assert a in body
    for it in plan.items.all():
        assert it.relpath in body
    assert (plan.n_change, plan.n_nochange, plan.n_skip) == (6, 3, 0)
    assert "<strong>6</strong> file(s) will change" in body
    assert "Rock;Progressive Rock;Symphonic Rock" in body  # delimited file shown after the edit
    r = confirm(applier, plan)
    job = EditJob.objects.get(plan=plan)
    assert r.status_code == 302 and r["Location"] == f"/edits/jobs/{job.id}/"
    assert job.status == "queued" and job.total == 6
    assert b'http-equiv="refresh"' in applier.get(f"/edits/jobs/{job.id}/").content
    job = work_one()
    assert job.status == "succeeded" and job.done == 6
    expected = {i.relpath: i.genres_after for i in plan.items.filter(action="change")}
    assert compare_tree(corpus.master, corpus.root, expected, check_audio=False) == {}
    # The index and the results reflect the files as read back from disk.
    for ji in JobItem.objects.filter(job=job):
        disk = list(read_file(corpus.path(ji.plan_item.relpath)).genre.values)
        assert ji.state == "committed" and ji.genres_read_back == disk == ji.genres_requested
        assert Track.objects.get(file__relpath=ji.plan_item.relpath).genres == disk
        assert Backup.objects.filter(sha256=ji.plan_item.sha_before).exists()
    page = applier.get(f"/edits/jobs/{job.id}/").content.decode()
    assert "succeeded" in page and 'http-equiv="refresh"' not in page and "Symphonic Rock" in page


def test_no_change_preview_for_existing_and_absent_genres(applier):
    select(applier, KC, GEN, YES)
    plan = preview(applier, "add", ["progressive rock"])
    body = applier.get(f"/edits/plans/{plan.id}/").content.decode()
    assert plan.n_change == 0 and plan.n_nochange == 9 and "no change" in body and "Apply" not in body
    plan = preview(applier, "remove", ["Polka"])
    assert plan.n_change == 0 and "not-present" in applier.get(f"/edits/plans/{plan.id}/").content.decode()


def test_no_file_is_written_by_gets_or_previews(applier, corpus):
    before = hashes(corpus.root)
    select(applier, KC, GEN, YES)
    for url in ("/edits/", "/edits/replace/", "/library/albums/", "/library/basket/"):
        assert applier.get(url).status_code == 200
    plan = preview(applier, "replace", ["Jazz", "Fusion"])
    applier.get(f"/edits/plans/{plan.id}/")
    applier.post(f"/edits/plans/{plan.id}/refresh/")
    assert hashes(corpus.root) == before


def test_unreadable_files_of_the_album_folder_appear_as_skipped(applier, corpus, lib):
    broken = corpus.root / "King Crimson/1969 - In the Court of the Crimson King/99 broken.flac"
    broken.write_bytes(b"\0" * 100)
    scan_root(lib, reader=lambda ps: [read_file(p) for p in ps])
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    item = plan.items.get(relpath__endswith="99 broken.flac")
    assert item.action == "skip" and item.reason == "unreadable"
    assert "99 broken.flac" in applier.get(f"/edits/plans/{plan.id}/").content.decode()


# --------------------------------------------------------------------------- permission and confirmation rules

def test_confirm_requires_permission(viewer):
    select(viewer, KC)
    plan = preview(viewer, "add", ["Jazz"])  # previewing is allowed
    assert "may not apply" in viewer.get(f"/edits/plans/{plan.id}/").content.decode()
    assert confirm(viewer, plan).status_code == 403
    assert not EditJob.objects.exists()


def test_confirm_requires_login(applier, client):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    client.logout()
    r = confirm(client, plan)
    assert r.status_code == 302 and r["Location"].startswith("/accounts/login/")
    assert not EditJob.objects.exists()


def test_wrong_digest_and_double_confirm_rejected(applier):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    applier.post(f"/edits/plans/{plan.id}/confirm/", {"digest": "0" * 64})
    assert not EditJob.objects.exists()
    assert confirm(applier, plan)["Location"].startswith("/edits/jobs/")
    r = confirm(applier, plan)
    assert r["Location"] == f"/edits/plans/{plan.id}/" and EditJob.objects.count() == 1


def test_tampered_plan_rows_are_rejected(applier):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    PlanItem.objects.filter(plan=plan, seq=0).update(genres_after=["Something", "Else"])
    confirm(applier, plan)
    assert not EditJob.objects.exists()


def test_replace_needs_both_confirmations(applier):
    select(applier, KC)
    r = applier.post("/edits/plans/new/", {"operation": "replace", "genres": "Jazz"})
    assert r["Location"] == "/edits/replace/" and not EditPlan.objects.exists()
    plan = preview(applier, "replace", ["Jazz", "Fusion"])
    assert "Replace all genres of 3 file(s)" in applier.get(f"/edits/plans/{plan.id}/").content.decode()
    confirm(applier, plan)
    assert not EditJob.objects.exists()
    confirm(applier, plan, replace_ack="yes")
    assert EditJob.objects.filter(plan=plan).exists()


def test_unconfigured_roots_are_refused(applier, settings, tmp_path, corpus):
    ids = select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    settings.GAMA = Config((), tmp_path / "data", verify_audio="off")
    with pytest.raises(PlanError):
        services.create_plan(EditPlan.objects.first().created_by, ids, "add", ["Jazz"])
    confirm(applier, plan)
    assert not EditJob.objects.exists()
    with pytest.raises(PlanError, match="not configured"):
        plan_files([FileRef(str(corpus.root), plan.items.first().relpath)], "add", ["Jazz"],
                   allowed_roots=[str(tmp_path)])


# --------------------------------------------------------------------------- conflicts and failures

def test_conflict_between_preview_and_apply_offers_refresh(applier, corpus):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    confirm(applier, plan)
    target = corpus.path(plan.items.first().relpath)
    subprocess.run(["metaflac", "--remove-tag=GENRE", "--set-tag=GENRE=Edited Elsewhere", str(target)], check=True)
    before = hashes(corpus.root)
    job = work_one()
    assert job.status == "conflict" and hashes(corpus.root) == before
    page = applier.get(f"/edits/jobs/{job.id}/").content.decode()
    assert "Refresh preview" in page
    r = applier.post(f"/edits/plans/{plan.id}/refresh/")
    new = EditPlan.objects.get(id=r["Location"].rstrip("/").split("/")[-1])
    assert new.id != plan.id
    item = new.items.get(relpath=plan.items.first().relpath)
    assert item.genres_before == ["Edited Elsewhere"] and item.genres_after == ["Edited Elsewhere", "Jazz"]


def test_missing_permissions_shown_per_item(applier, corpus):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    confirm(applier, plan)
    ro = plan.items.order_by("seq")[1]
    os.chmod(corpus.path(ro.relpath), 0o444)
    try:
        job = work_one()
    finally:
        os.chmod(corpus.path(ro.relpath), 0o644)
    assert job.status == "failed"
    page = applier.get(f"/edits/jobs/{job.id}/").content.decode()
    assert "not-writable" in page


def test_partial_failure_shown_per_item_with_continue(applier, corpus):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    confirm(applier, plan, continue_on_error="yes")
    target = corpus.path(plan.items.order_by("seq")[1].relpath)
    fsutil.set_failpoint("item_start", lambda ctx: os.chmod(target, 0o444) if ctx["n"] == 1 else None)
    try:
        job = work_one()
    finally:
        os.chmod(target, 0o644)
    assert job.status == "completed_with_errors" and job.policy == "continue"
    states = list(JobItem.objects.filter(job=job).order_by("plan_item__seq").values_list("state", flat=True))
    assert states == ["committed", "failed", "committed"]
    page = applier.get(f"/edits/jobs/{job.id}/").content.decode()
    assert "not-writable" in page and page.count("committed") >= 2


def test_rollback_shown_on_job_page(applier, corpus):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    confirm(applier, plan)
    target = corpus.path(plan.items.order_by("seq")[1].relpath)
    fsutil.set_failpoint("item_start", lambda ctx: os.chmod(target, 0o444) if ctx["n"] == 1 else None)
    try:
        job = work_one()
    finally:
        os.chmod(target, 0o644)
    assert job.status == "rolled_back"
    ji = JobItem.objects.get(job=job, plan_item__seq=0)
    assert (ji.state, ji.restore_state) == ("committed", "restored")
    assert ji.genres_read_back == ji.plan_item.genres_before  # the index shows the original again
    assert "rollback: restored" in applier.get(f"/edits/jobs/{job.id}/").content.decode()


# --------------------------------------------------------------------------- worker

def test_interrupted_job_recovered_at_worker_startup(applier, corpus, settings):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    confirm(applier, plan)
    job = services.next_queued()
    assert services.claim(job.id)
    items = [{"n": i.seq, "root": i.root_path, "relpath": i.relpath, "format": i.format,
              "sha_before": i.sha_before, "genres_before": i.genres_before, "genres_after": i.genres_after}
             for i in plan.items.filter(action="change")]
    data_dir = str(settings.GAMA.data_dir)
    req = Path(data_dir) / "req.json"
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    req.write_text(json.dumps({"data_dir": data_dir, "job_id": job.journal_id, "items": items}))
    env = dict(os.environ, GAMA_ENABLE_FAILPOINTS="1", GAMA_FAILPOINTS="after_commit:1")
    r = subprocess.run([sys.executable, str(HERE.parents[1] / "safewrite" / "apply_child.py"), str(req)],
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == fsutil.EXIT_CODE, r.stderr[-1500:]
    EditJob.objects.filter(id=job.id).update(heartbeat_at=timezone.now() - timedelta(minutes=5))
    assert services.heartbeat_stale(EditJob.objects.get(id=job.id))
    services.worker_startup()
    job.refresh_from_db()
    assert job.status == "interrupted"
    states = list(JobItem.objects.filter(job=job).order_by("plan_item__seq").values_list("state", flat=True))
    assert states == ["committed", "committed", "failed"]
    for ji in JobItem.objects.filter(job=job):
        assert ji.genres_read_back == list(read_file(corpus.path(ji.plan_item.relpath)).genre.values)


def test_running_job_without_journal_is_closed_at_startup(applier):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    confirm(applier, plan)
    job = services.next_queued()
    services.claim(job.id)
    services.worker_startup()
    job.refresh_from_db()
    assert job.status == "interrupted" and "nothing was written" in job.error


def test_worker_processes_queued_jobs_in_order(applier, capsys):
    select(applier, KC)
    p1 = preview(applier, "add", ["Jazz"])
    p2 = preview(applier, "add", ["Blues"])
    j1 = EditJob.objects.get(id=confirm(applier, p1)["Location"].rstrip("/").split("/")[-1])
    j2 = EditJob.objects.get(id=confirm(applier, p2)["Location"].rstrip("/").split("/")[-1])
    EditJob.objects.filter(id=j1.id).update(created_at=timezone.now() - timedelta(seconds=5))
    call_command("gama_worker", "--once")
    j1.refresh_from_db()
    j2.refresh_from_db()
    assert j1.status == "succeeded" and j2.status == "queued"
    # p2 was planned before p1 was applied, so its files changed: the worker reports a conflict, writes nothing.
    call_command("gama_worker", "--once")
    j2.refresh_from_db()
    assert j2.status == "conflict"
    out = capsys.readouterr().out
    assert out.index(str(j1.id)) < out.index(str(j2.id))


def test_worker_ignores_jobs_that_are_not_queued(applier):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    confirm(applier, plan)
    job = EditJob.objects.get()
    EditJob.objects.filter(id=job.id).update(status="failed")
    assert services.next_queued() is None and not services.claim(job.id)
    call_command("gama_worker", "--once")
    job.refresh_from_db()
    assert job.status == "failed"


def test_stale_queue_shows_worker_not_running(applier):
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    confirm(applier, plan)
    job = EditJob.objects.get()
    EditJob.objects.filter(id=job.id).update(created_at=timezone.now() - timedelta(minutes=2))
    assert "Worker not running" in applier.get(f"/edits/jobs/{job.id}/").content.decode()


def test_gama_grant(django_user_model, capsys):
    u = django_user_model.objects.create_user("bob", password="x")
    call_command("gama_grant", "bob")
    assert django_user_model.objects.get(id=u.id).has_perm("edits.apply_edits")
    call_command("gama_grant", "bob", "--revoke")
    assert not django_user_model.objects.get(id=u.id).has_perm("edits.apply_edits")
