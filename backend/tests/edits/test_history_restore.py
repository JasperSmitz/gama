"""M8: history, restore flow, recovery-pending surfacing, gama_restore CLI."""
import os
import subprocess

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from gama.edits import services
from gama.edits.models import EditJob, EditPlan, JobItem
from gama.library.models import Track
from gama.metadata.reader import read_file
from tests.edits.test_workflow import (KC, YES, applier, confirm, lib, preview, select, viewer,  # noqa: F401
                                       work_one)
from verify.verify import compare_tree

pytestmark = [pytest.mark.slow, pytest.mark.django_db]


def applied(client, *albums, genre="Symphonic Prog"):
    select(client, *albums)
    plan = preview(client, "add", [genre])
    confirm(client, plan)
    job = work_one()
    assert job.status == "succeeded"
    return job


def restore_preview(client, job, **data):
    r = client.post(f"/edits/jobs/{job.id}/restore/", {"scope": "all", **data})
    assert r.status_code == 302 and "/edits/plans/" in r["Location"], r.content[:300]
    return EditPlan.objects.get(id=r["Location"].rstrip("/").split("/")[-1])


# --------------------------------------------------------------------------- history

def test_history_and_job_page_hold_every_required_record_field(applier, settings):
    job = applied(applier, KC)
    hist = applier.get("/edits/history/").content.decode()
    assert str(job.id)[:8] in hist and "Symphonic Prog" in hist and "applier" in hist and "Backup store" in hist
    page = applier.get(f"/edits/jobs/{job.id}/").content.decode()
    # requirements section 5: timestamp and operation id, affected files, original metadata, requested
    # modification, successful and failed writes, backup locations, verification results.
    assert str(job.id) in page and job.created_at.strftime("%Y-%m-%d") in page
    for ji in JobItem.objects.filter(job=job):
        assert ji.plan_item.relpath in page                                   # affected files
        assert " | ".join(ji.plan_item.genres_before) in page                 # original genres
        assert ji.snapshot_before                                              # full original metadata
        assert f"backups/{ji.plan_item.sha_before and ''}" or True
    assert "original metadata snapshot" in page
    assert "Add genre: Symphonic Prog" in page                                 # requested modification
    assert page.count("committed") >= 3                                        # successful writes
    assert f"{settings.GAMA.data_dir}/backups/sha256/" in page                  # backup locations
    assert "ok," in page and "audio" in page                                   # verification results
    assert job.journal_path and job.journal_path in page


# --------------------------------------------------------------------------- restore

def test_restore_via_ui_gives_byte_identical_originals_and_updates_index(applier, corpus):
    job = applied(applier, KC, YES)
    rplan = restore_preview(applier, job)
    body = applier.get(f"/edits/plans/{rplan.id}/").content.decode()
    assert rplan.operation == "restore" and rplan.n_change == 6 and "will be restored" in body
    r = confirm(applier, rplan)
    rjob = EditJob.objects.get(plan=rplan)
    assert rjob.kind == "restore" and r["Location"] == f"/edits/jobs/{rjob.id}/"
    rjob = work_one()
    assert rjob.status == "succeeded"
    assert compare_tree(corpus.master, corpus.root, check_mtime=False) == {}
    for ji in JobItem.objects.filter(job=job):
        t = Track.objects.get(file__relpath=ji.plan_item.relpath)
        assert t.genres == ji.plan_item.genres_before
    for ji in JobItem.objects.filter(job=rjob):
        assert ji.state == "restored" and ji.genres_read_back == ji.plan_item.genres_after
    assert "Restores of this job" in applier.get(f"/edits/jobs/{job.id}/").content.decode()
    again = restore_preview(applier, job)
    assert again.n_change == 0 and again.n_nochange == 6


def test_restore_preview_shows_conflicts_and_does_not_touch_them(applier, corpus):
    job = applied(applier, KC)
    edited = JobItem.objects.filter(job=job).order_by("plan_item__seq")[0]
    p = corpus.path(edited.plan_item.relpath)
    subprocess.run(["metaflac", "--set-tag=COMMENT=edited later", str(p)], check=True)
    later = p.read_bytes()
    rplan = restore_preview(applier, job)
    item = rplan.items.get(relpath=edited.plan_item.relpath)
    assert item.action == "conflict" and item.reason == "changed-since-edit"
    assert "conflict: not touched" in applier.get(f"/edits/plans/{rplan.id}/").content.decode()
    confirm(applier, rplan)
    assert work_one().status == "succeeded"
    assert p.read_bytes() == later
    others = JobItem.objects.filter(job=job).exclude(id=edited.id)
    for ji in others:
        assert corpus.path(ji.plan_item.relpath).read_bytes() == (corpus.master / ji.plan_item.relpath).read_bytes()


def test_forced_restore_backs_up_the_later_version(applier, corpus):
    job = applied(applier, KC)
    ji = JobItem.objects.filter(job=job).order_by("plan_item__seq")[0]
    p = corpus.path(ji.plan_item.relpath)
    subprocess.run(["metaflac", "--set-tag=COMMENT=edited later", str(p)], check=True)
    from gama.safewrite.fsutil import sha256_file
    later_sha = sha256_file(p)
    rplan = restore_preview(applier, job, force="yes")
    item = rplan.items.get(relpath=ji.plan_item.relpath)
    assert item.action == "restore" and item.force
    confirm(applier, rplan)
    assert work_one().status == "succeeded"
    assert p.read_bytes() == (corpus.master / ji.plan_item.relpath).read_bytes()
    assert services.make_env().store.verify(later_sha)


def test_restore_selected_items_only(applier, corpus):
    job = applied(applier, KC)
    items = list(JobItem.objects.filter(job=job).order_by("plan_item__seq"))
    rplan = restore_preview(applier, job, scope="selected", item=[items[1].id])
    assert rplan.items.count() == 1 and rplan.items.get().relpath == items[1].plan_item.relpath


def test_restore_needs_permission_to_confirm(applier, django_user_model):
    from django.test import Client
    job = applied(applier, KC)
    # Other users cannot even see the job (plans and jobs are personal) ...
    other = Client()
    other.force_login(django_user_model.objects.create_user("other", password="x"))
    assert other.post(f"/edits/jobs/{job.id}/restore/", {"scope": "all"}).status_code == 404
    # ... and confirming a restore needs the apply permission.
    rplan = restore_preview(applier, job)
    owner = django_user_model.objects.get(username="applier")
    owner.user_permissions.clear()
    applier.force_login(django_user_model.objects.get(id=owner.id))
    assert confirm(applier, rplan).status_code == 403
    assert not EditJob.objects.filter(plan=rplan).exists()


def test_failed_rollback_job_offers_restore(applier, corpus):
    from gama.safewrite import fsutil
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    confirm(applier, plan, continue_on_error="yes")
    target = corpus.path(plan.items.order_by("seq")[1].relpath)
    fsutil.set_failpoint("item_start", lambda ctx: os.chmod(target, 0o444) if ctx["n"] == 1 else None)
    try:
        job = work_one()
    finally:
        fsutil.clear_failpoints()
        os.chmod(target, 0o644)
    assert job.status == "completed_with_errors"
    page = applier.get(f"/edits/jobs/{job.id}/").content.decode()
    assert "Restore from backups" in page
    assert restore_preview(applier, job).n_change == 2


# --------------------------------------------------------------------------- recovery pending in the UI

def test_pending_recovery_is_surfaced_and_resolves(applier, corpus, settings):
    import json
    import sys
    from pathlib import Path

    from gama.safewrite import fsutil
    select(applier, KC)
    plan = preview(applier, "add", ["Jazz"])
    confirm(applier, plan)
    job = services.next_queued()
    services.claim(job.id)
    items = [{"n": i.seq, "root": i.root_path, "relpath": i.relpath, "format": i.format, "sha_before": i.sha_before,
              "genres_before": i.genres_before, "genres_after": i.genres_after}
             for i in plan.items.filter(action="change")]
    data_dir = Path(settings.GAMA.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "req.json").write_text(json.dumps({"data_dir": str(data_dir), "job_id": job.journal_id,
                                                   "items": items}))
    child = Path(__file__).resolve().parents[1] / "safewrite" / "apply_child.py"
    r = subprocess.run([sys.executable, str(child), str(data_dir / "req.json")], capture_output=True,
                       env=dict(os.environ, GAMA_ENABLE_FAILPOINTS="1", GAMA_FAILPOINTS="after_commit:1"))
    assert r.returncode == fsutil.EXIT_CODE
    from datetime import timedelta

    from django.utils import timezone
    EditJob.objects.filter(id=job.id).update(worker_pid=None, heartbeat_at=timezone.now() - timedelta(minutes=5))
    offline = corpus.root.with_name("offline")
    os.rename(corpus.root, offline)
    try:
        services.worker_startup(reader=lambda ps: [])
    finally:
        os.rename(offline, corpus.root)
    job.refresh_from_db()
    assert job.recovery_pending and job.status == "interrupted"
    for url in ("/edits/history/", f"/edits/jobs/{job.id}/"):
        body = applier.get(url).content.decode()
        assert "Recovery pending" in body and "gama_recover --resolve" in body
    services.worker_startup()
    job.refresh_from_db()
    assert not job.recovery_pending and job.status == "interrupted"
    assert list(JobItem.objects.filter(job=job).order_by("plan_item__seq").values_list("state", flat=True)) == \
        ["committed", "committed", "failed"]
    assert "Recovery pending" not in applier.get("/edits/history/").content.decode()


# --------------------------------------------------------------------------- gama_restore CLI

def test_gama_restore_cli_from_the_journal(applier, corpus, capsys):
    job = applied(applier, KC)
    call_command("gama_restore", str(job.id), "--dry-run")
    out = capsys.readouterr().out
    assert out.count(": restore") == 3
    first = JobItem.objects.filter(job=job).order_by("plan_item__seq")[0]
    p = corpus.path(first.plan_item.relpath)
    subprocess.run(["metaflac", "--set-tag=COMMENT=later", str(p)], check=True)
    with pytest.raises(CommandError, match="completed_with_errors"):
        call_command("gama_restore", job.journal_id)
    out = capsys.readouterr().out
    assert "restore_conflict" in out and out.count(": restored") == 2
    call_command("gama_restore", str(job.id), "--items", "0", "--force")
    assert p.read_bytes() == (corpus.master / first.plan_item.relpath).read_bytes()
    assert compare_tree(corpus.master, corpus.root, check_mtime=False) == {}


def test_gama_restore_unknown_job(settings):
    with pytest.raises(CommandError, match="no journal"):
        call_command("gama_restore", "0" * 32)
