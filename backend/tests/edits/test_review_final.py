"""Regression tests for the final review: forced restores bound to the previewed version,
recorded and undoable; restore form and CLI edge cases; CLI without a database."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from gama.edits import services
from gama.edits.models import Backup, EditJob, EditPlan, JobItem, PlanItem
from gama.safewrite.fsutil import sha256_file
from tests.edits.test_history_restore import applied, restore_preview
from tests.edits.test_workflow import KC, applier, confirm, lib, work_one  # noqa: F401
from verify.verify import compare_tree

pytestmark = [pytest.mark.slow, pytest.mark.django_db]


def edit_later(path, text):
    subprocess.run(["metaflac", f"--set-tag=COMMENT={text}", str(path)], check=True)
    return path.read_bytes()


def first_item(job):
    return JobItem.objects.filter(job=job).order_by("plan_item__seq")[0]


# --------------------------------------------------------------------------- 1: force is bound to the previewed version

def test_forced_restore_does_not_overwrite_a_version_newer_than_the_preview(applier, corpus):
    job = applied(applier, KC)
    p = corpus.path(first_item(job).plan_item.relpath)
    edit_later(p, "version X")
    rplan = restore_preview(applier, job, force="yes")
    item = rplan.items.get(relpath=first_item(job).plan_item.relpath)
    assert item.force and item.sha_current_seen == sha256_file(p)
    y = edit_later(p, "version Y")  # changes again after the preview
    confirm(applier, rplan)
    rjob = work_one()
    assert rjob.status == "completed_with_errors"
    ji = JobItem.objects.get(job=rjob, plan_item=item)
    assert ji.state == "restore_conflict" and "since the restore preview" in ji.error
    assert p.read_bytes() == y


def test_seen_hash_is_part_of_the_restore_digest(applier, corpus):
    job = applied(applier, KC)
    edit_later(corpus.path(first_item(job).plan_item.relpath), "version X")
    rplan = restore_preview(applier, job, force="yes")
    PlanItem.objects.filter(plan=rplan, force=True).update(sha_current_seen="0" * 64)
    confirm(applier, rplan)
    assert not EditJob.objects.filter(plan=rplan).exists()


# --------------------------------------------------------------------------- 2: the replaced version is recorded and can be brought back

def test_forced_backup_is_recorded_shown_and_undoable(applier, corpus, settings):
    job = applied(applier, KC)
    rel = first_item(job).plan_item.relpath
    p = corpus.path(rel)
    x = edit_later(p, "version X")
    x_sha = sha256_file(p)
    rplan = restore_preview(applier, job, force="yes")
    confirm(applier, rplan)
    rjob = work_one()
    assert rjob.status == "succeeded"
    ji = JobItem.objects.get(job=rjob, plan_item__relpath=rel)
    assert ji.forced_backup_sha == x_sha
    b = Backup.objects.get(sha256=x_sha)
    assert b.size == len(x)
    page = applier.get(f"/edits/jobs/{rjob.id}/").content.decode()
    assert f"backups/sha256/{x_sha[:2]}/{x_sha}" in page and "Undo this restore" in page
    # Undo the forced restore: version X comes back, through a preview.
    undo = restore_preview(applier, rjob)
    assert undo.n_change == 1 and undo.items.get().sha_before == x_sha
    confirm(applier, undo)
    assert work_one().status == "succeeded"
    assert p.read_bytes() == x


def test_restore_job_without_forced_items_offers_nothing_to_undo(applier):
    job = applied(applier, KC)
    rplan = restore_preview(applier, job)
    confirm(applier, rplan)
    rjob = work_one()
    assert "Undo this restore" not in applier.get(f"/edits/jobs/{rjob.id}/").content.decode()
    r = applier.post(f"/edits/jobs/{rjob.id}/restore/", {"scope": "all"}, follow=True)
    assert "nothing to undo" in r.content.decode()


# --------------------------------------------------------------------------- 3: empty selection

def test_selected_scope_without_ticks_explains(applier):
    job = applied(applier, KC)
    before = EditPlan.objects.count()
    r = applier.post(f"/edits/jobs/{job.id}/restore/", {"scope": "selected"}, follow=True)
    body = r.content.decode()
    assert "Tick at least one file" in body and "no file of this job was changed" not in body
    assert EditPlan.objects.count() == before
    with pytest.raises(services.PlanError, match="tick at least one"):
        services.create_restore_plan(job.created_by, job, [])


# --------------------------------------------------------------------------- 5: CLI errors are clean

def test_cli_bad_items_and_unreadable_file(applier, corpus, capsys):
    job = applied(applier, KC)
    with pytest.raises(CommandError, match="comma-separated numbers"):
        call_command("gama_restore", str(job.id), "--items", "1,x")
    with pytest.raises(CommandError, match="64 hex"):
        call_command("gama_restore", str(job.id), "--force", "--expect-sha", "0:abc")
    p = corpus.path(first_item(job).plan_item.relpath)
    os.chmod(p, 0o200)
    try:
        call_command("gama_restore", str(job.id), "--dry-run")
    finally:
        os.chmod(p, 0o644)
    assert "cannot read the file" in capsys.readouterr().out


def test_cli_dry_run_shows_the_exact_force_command(applier, corpus, capsys):
    job = applied(applier, KC)
    p = corpus.path(first_item(job).plan_item.relpath)
    edit_later(p, "later")
    call_command("gama_restore", str(job.id), "--dry-run")
    assert f"--force --expect-sha 0:{sha256_file(p)}" in capsys.readouterr().out


def test_cli_undoes_a_forced_restore(applier, corpus, capsys):
    job = applied(applier, KC)
    p = corpus.path(first_item(job).plan_item.relpath)
    x = edit_later(p, "version X")
    call_command("gama_restore", str(job.id), "--items", "0", "--force", "--expect-sha", f"0:{sha256_file(p)}")
    out = capsys.readouterr().out
    rjob_id = out.split("restore job ")[1].split(":")[0]
    assert "replaced version kept as backup" in out and p.read_bytes() != x
    call_command("gama_restore", rjob_id)
    assert p.read_bytes() == x


# --------------------------------------------------------------------------- 7: CLI works without the database

def test_gama_restore_works_with_the_database_unavailable(applier, corpus, settings):
    job = applied(applier, KC)
    data_dir = Path(settings.GAMA.data_dir)
    (data_dir / "gama.sqlite3").mkdir()  # any attempt to open the database now fails
    backend = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GAMA_", "DJANGO_"))}
    env.update(GAMA_DEBUG="1", GAMA_CONFIG="", GAMA_DATA_DIR=str(data_dir), GAMA_LIBRARY_ROOTS=str(corpus.root),
               DJANGO_SETTINGS_MODULE="gama.settings")
    check = subprocess.run([sys.executable, str(backend / "manage.py"), "migrate", "--check"], cwd=backend,
                           env=env, capture_output=True, text=True)
    assert check.returncode != 0 and "unable to open database" in check.stderr  # the DB really is unavailable
    r = subprocess.run([sys.executable, str(backend / "manage.py"), "gama_restore", job.journal_id], cwd=backend,
                       env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "succeeded" in r.stdout
    assert compare_tree(corpus.master, corpus.root, check_mtime=False) == {}
