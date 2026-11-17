"""Regression tests for the M7 review: worker races, heartbeats, error handling, scoping."""
import os
import time
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from gama.edits import services
from gama.edits.models import EditJob, EditPlan, JobItem
from gama.safewrite import writer as writer_mod
from tests.edits.test_workflow import (KC, _user, applier, confirm, lib, preview, select,  # noqa: F401
                                       work_one)

pytestmark = [pytest.mark.slow, pytest.mark.django_db]


def queued(client, genre="Jazz"):
    select(client, KC)
    plan = preview(client, "add", [genre])
    confirm(client, plan)
    return EditJob.objects.get(plan=plan)


# --------------------------------------------------------------------------- 1: one worker, live jobs untouched

def test_second_worker_refuses_to_start(applier):
    fd = services.worker_lock()
    try:
        with pytest.raises(CommandError, match="already running"):
            call_command("gama_worker", "--once")
    finally:
        os.close(fd)
    call_command("gama_worker", "--once")  # free again


def test_startup_leaves_a_live_running_job_alone(applier):
    job = queued(applier)
    assert services.claim(job.id)  # worker_pid = this (live) process, fresh heartbeat
    services.worker_startup()
    job.refresh_from_db()
    assert job.status == "running" and job.error == ""


def test_startup_settles_a_running_job_of_a_dead_worker(applier):
    job = queued(applier)
    services.claim(job.id)
    EditJob.objects.filter(id=job.id).update(worker_pid=2 ** 22 + 12345,  # no such process
                                             heartbeat_at=timezone.now() - timedelta(minutes=5))
    services.worker_startup()
    job.refresh_from_db()
    assert job.status == "interrupted"


def test_lock_busy_requeues_instead_of_failing(applier, corpus):
    job = queued(applier)
    env = services.make_env()
    before = {p: p.read_bytes() for p in corpus.root.rglob("*.flac")}
    with env.write_lock():  # e.g. gama_restore running
        out = services.work_once()
    assert out.status == "queued" and "will retry" in out.error
    assert {p: p.read_bytes() for p in corpus.root.rglob("*.flac")} == before
    assert services.work_once().status == "succeeded"


# --------------------------------------------------------------------------- 2: heartbeats

@pytest.mark.django_db(transaction=True)
def test_heartbeat_thread_keeps_a_long_job_fresh(applier, monkeypatch):
    job = queued(applier)
    services.claim(job.id)
    EditJob.objects.filter(id=job.id).update(heartbeat_at=timezone.now() - timedelta(minutes=5))
    with services._Heartbeat(job.id, interval=0.05):
        time.sleep(0.4)
    job.refresh_from_db()
    assert (timezone.now() - job.heartbeat_at).total_seconds() < 5


def test_queued_job_behind_a_busy_worker_is_not_stale(applier):
    running = queued(applier, "Jazz")
    waiting = queued(applier, "Blues")
    old = timezone.now() - timedelta(minutes=5)
    EditJob.objects.filter(id=waiting.id).update(created_at=old)
    services.claim(running.id)  # fresh heartbeat
    waiting.refresh_from_db()
    assert not services.heartbeat_stale(waiting)
    EditJob.objects.filter(id=running.id).update(heartbeat_at=old)
    assert services.heartbeat_stale(waiting)


# --------------------------------------------------------------------------- 3: the worker survives errors

def test_worker_survives_an_exception_in_a_job(applier, monkeypatch):
    j1 = queued(applier, "Jazz")
    j2 = queued(applier, "Blues")
    EditJob.objects.filter(id=j1.id).update(created_at=timezone.now() - timedelta(seconds=5))
    real = writer_mod.run_apply
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real(*a, **kw)
    monkeypatch.setattr(writer_mod, "run_apply", flaky)
    first = services.work_once()
    assert first.id == j1.id and first.status == "interrupted" and "worker error" in first.error
    second = services.work_once()
    # j2 was planned on the same files before j1 ran; j1 wrote nothing, so j2 applies cleanly.
    assert second.id == j2.id and second.status == "succeeded"


def test_reindex_failure_is_recorded_not_raised(applier, monkeypatch):
    queued(applier)

    def broken(*a, **kw):
        raise RuntimeError("index is busy")
    monkeypatch.setattr(services, "reindex_job", broken)
    job = services.work_once()
    assert job.status == "succeeded"
    job.refresh_from_db()
    assert "re-reading the files into the index failed" in job.error


# --------------------------------------------------------------------------- 4: refresh supersedes

def test_refresh_after_conflict_supersedes_the_old_plan(applier, corpus):
    import subprocess
    job = queued(applier)
    target = corpus.path(job.plan.items.first().relpath)
    subprocess.run(["metaflac", "--set-tag=COMMENT=x", str(target)], check=True)
    assert work_one().status == "conflict"
    applier.post(f"/edits/plans/{job.plan.id}/refresh/")
    assert EditPlan.objects.get(id=job.plan.id).status == "superseded"
    assert "out of date" in applier.get(f"/edits/plans/{job.plan.id}/").content.decode()


# --------------------------------------------------------------------------- 5: scoping

def test_plans_and_jobs_are_personal(applier, client, django_user_model):
    job = queued(applier)
    plan = job.plan
    other = _user(django_user_model, "mallory", True)
    applier.logout()
    applier.force_login(other)
    for url in (f"/edits/plans/{plan.id}/", f"/edits/jobs/{job.id}/"):
        assert applier.get(url).status_code == 404
    assert applier.post(f"/edits/plans/{plan.id}/refresh/").status_code == 404
    assert applier.post(f"/edits/plans/{plan.id}/confirm/", {"digest": plan.digest}).status_code == 404
    staff = django_user_model.objects.create_user("admin", password="x", is_staff=True)
    applier.force_login(staff)
    assert applier.get(f"/edits/jobs/{job.id}/").status_code == 200


# --------------------------------------------------------------------------- 6: sync after recovery

def test_sync_refreshes_done_and_verification(applier):
    job = queued(applier)
    job = work_one()
    EditJob.objects.filter(id=job.id).update(done=0)
    JobItem.objects.filter(job=job).update(verification=None, backup_sha="")
    services.sync_from_journal(job, job.journal_path)
    job.refresh_from_db()
    assert job.done == 3
    for ji in JobItem.objects.filter(job=job):
        assert ji.verification and ji.verification["ok"] and ji.backup_sha == ji.sha_before


def test_damaged_journal_error_is_kept(applier, settings):
    job = queued(applier)
    services.claim(job.id)
    EditJob.objects.filter(id=job.id).update(worker_pid=None, heartbeat_at=timezone.now() - timedelta(minutes=5))
    env = services.make_env()
    with open(os.path.join(env.journal_dir, f"{job.journal_id}.jsonl"), "w") as f:
        f.write('{"t": "header", "job": "%s", "items": [{"n": 0}]}\n{broken\n{}\n' % job.journal_id)
    services.worker_startup()
    job.refresh_from_db()
    assert "recovery damaged" in job.error or "journal unreadable" in job.error
