"""Glue between the web UI and the framework-free planner / safe write engine.

Nothing here writes to library files itself: plans only read; jobs hand the
work to ``safewrite.run_apply``, which runs in the worker process.
"""
from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from gama.library.models import Album, LibraryRoot, MediaFile, configured_root_paths, visible_albums
from gama.library.scanner import reindex_paths
from gama.planning.planner import FilePlan, FileRef, PlanError, digest, plan_files
from gama.safewrite.env import WriteEnv
from gama.safewrite.journal import TERMINAL_JOB, JournalError, load, summarize

from .models import Backup, EditJob, EditPlan, JobItem, PlanItem

HEARTBEAT_STALE_SECONDS = 60


class ConfirmError(RuntimeError):
    pass


# --------------------------------------------------------------------------- plans

def _album_label(a: Album) -> str:
    return f"{a.album or '(no album tag)'} — {a.albumartist or '(no album artist)'} [{a.album_dir}]"


def refs_for_albums(albums: list[Album]) -> tuple[list[FileRef], list[str]]:
    """Every file of the selected albums: their tracks, plus files of the album folders
    that could not be read (so the preview shows them as skipped). Also returns the
    index's known paths for those folders (for the "rescan suggested" warning)."""
    refs: list[FileRef] = []
    seen: set[tuple[str, str]] = set()
    known: list[str] = []
    for a in albums:
        label = _album_label(a)
        files = list(MediaFile.objects.filter(track__album=a).order_by("relpath")) + list(a.unreadable_files())
        for mf in files:
            key = (a.root.path, mf.relpath)
            if key in seen:
                continue
            seen.add(key)
            refs.append(FileRef(a.root.path, mf.relpath, str(a.id), label))
        known += [os.path.join(a.root.path, rel) for rel in
                  MediaFile.objects.filter(root=a.root, album_dir=a.album_dir).values_list("relpath", flat=True)]
    return refs, known


def create_plan(user, album_ids: list[int], operation: str, genres: list[str], *, reader=None) -> EditPlan:
    """Read every file fresh, plan the operation and store the plan. Raises PlanError."""
    by_id = {a.id: a for a in visible_albums().select_related("root").filter(id__in=album_ids)}
    albums = [by_id[i] for i in album_ids if i in by_id]
    if not albums:
        raise PlanError("no albums selected")
    refs, known = refs_for_albums(albums)
    if not refs:
        raise PlanError("the selected albums have no files")
    plan = plan_files(refs, operation, genres, reader=reader, known_paths=known,
                      allowed_roots=configured_root_paths())
    with transaction.atomic():
        ep = EditPlan.objects.create(created_by=user, operation=operation, genres=list(plan.genres),
                                     album_ids=[a.id for a in albums], album_labels=[_album_label(a) for a in albums],
                                     digest=plan.digest, warnings=list(plan.warnings), n_change=plan.n_change,
                                     n_nochange=plan.n_nochange, n_skip=plan.n_skip)
        PlanItem.objects.bulk_create([_plan_item(ep, fp, by_id) for fp in plan.items])
    return ep


def _plan_item(ep: EditPlan, fp: FilePlan, albums: dict) -> PlanItem:
    d = asdict(fp)
    album = albums.get(int(fp.album_key)) if fp.album_key.isdigit() else None
    return PlanItem(plan=ep, seq=d["seq"], album=album, album_key=d["album_key"], album_label=d["album_label"],
                    root_path=d["root"], relpath=d["relpath"], format=d["format"], size=d["size"],
                    mtime_ns=d["mtime_ns"], inode=d["inode"], sha_before=d["sha_before"], style=d["style"],
                    genres_before=d["genres_before"], genres_after=d["genres_after"],
                    components_before=d["components_before"], components_after=d["components_after"],
                    action=d["action"], reason=d["reason"], warnings=d["warnings"], error=d["error"])


def file_plans(ep: EditPlan) -> list[FilePlan]:
    return [FilePlan(seq=i.seq, root=i.root_path, relpath=i.relpath, album_key=i.album_key,
                     album_label=i.album_label, action=i.action, reason=i.reason, format=i.format, size=i.size,
                     mtime_ns=i.mtime_ns, inode=i.inode, sha_before=i.sha_before, style=i.style,
                     genres_before=i.genres_before, components_before=i.components_before,
                     genres_after=i.genres_after, components_after=i.components_after,
                     warnings=list(i.warnings), error=i.error) for i in ep.items.all()]


def recompute_digest(ep: EditPlan) -> str:
    """The digest of exactly what is stored (and therefore shown)."""
    return digest(ep.operation, ep.genres, file_plans(ep), ep.warnings)


def refresh_plan(ep: EditPlan, user, *, reader=None) -> EditPlan:
    """A new plan for the same albums and request; the old one is superseded."""
    new = create_plan(user, list(ep.album_ids), ep.operation, list(ep.genres), reader=reader)
    EditPlan.objects.filter(id=ep.id, status="draft").update(status="superseded")
    return new


def confirm_plan(ep: EditPlan, user, posted_digest: str, *, policy: str = "stop_rollback",
                 replace_confirmed: bool = False) -> EditJob:
    """Create the job for a plan. Requires the apply permission (checked by the view),
    the digest the user saw, and an explicit extra confirmation for replace."""
    if ep.status != "draft":
        raise ConfirmError(f"this plan is {ep.status}; it can be confirmed only once")
    if not posted_digest or posted_digest != ep.digest or recompute_digest(ep) != ep.digest:
        raise ConfirmError("the preview you confirmed does not match this plan; refresh the preview")
    if ep.operation == "replace" and not replace_confirmed:
        raise ConfirmError("replacing genres needs the extra confirmation checkbox")
    if ep.n_change == 0:
        raise ConfirmError("nothing to change")
    stray = {i.root_path for i in ep.items.all()} - set(configured_root_paths())
    if stray:
        raise ConfirmError(f"library roots no longer configured: {sorted(stray)}")
    try:
        with transaction.atomic():
            n = EditPlan.objects.filter(id=ep.id, status="draft").update(status="confirmed")
            if n != 1:
                raise ConfirmError("this plan was confirmed already")
            job = EditJob.objects.create(plan=ep, kind="apply", created_by=user, policy=policy,
                                         total=ep.n_change)
            JobItem.objects.bulk_create([
                JobItem(job=job, plan_item=i, sha_before=i.sha_before or "", genres_requested=i.genres_after or [])
                for i in ep.items.filter(action="change")])
    except IntegrityError:
        raise ConfirmError("this plan was confirmed already") from None
    return job


# --------------------------------------------------------------------------- jobs

def make_env() -> WriteEnv:
    cfg = settings.GAMA
    return WriteEnv(cfg.data_dir, margin_bytes=cfg.backup_margin_mb << 20, audio=cfg.verify_audio)


def claim(job_id) -> bool:
    """Atomically move a queued job to running (only one worker can win)."""
    return EditJob.objects.filter(id=job_id, status="queued").update(
        status="running", worker_pid=os.getpid(), started_at=timezone.now(), heartbeat_at=timezone.now()) == 1


def next_queued() -> EditJob | None:
    return EditJob.objects.filter(status="queued").order_by("created_at").first()


class _Mirror:
    """Journal observer: mirrors each durable journal record into the DB (progress)."""

    def __init__(self, job: EditJob, env: WriteEnv):
        self.job = job
        self.env = env
        self.by_seq = {ji.plan_item.seq: ji for ji in job.items.select_related("plan_item")}

    def __call__(self, rec: dict) -> None:
        now = timezone.now()
        if rec.get("t") == "item" and rec.get("n") in self.by_seq:
            ji = self.by_seq[rec["n"]]
            if rec.get("state"):
                ji.state = rec["state"]
            if rec.get("restore_state"):
                ji.restore_state = rec["restore_state"]
            ji.stage = rec.get("stage") or ji.stage
            if rec.get("error"):
                ji.error = rec["error"]
            if rec.get("sha_after"):
                ji.sha_after = rec["sha_after"]
            if rec.get("verification"):
                ji.verification = rec["verification"]
            if rec.get("state") == "backed_up":
                ji.backup_sha = ji.sha_before
                path = self.env.store.path_for(ji.sha_before)
                Backup.objects.update_or_create(sha256=ji.sha_before, defaults={
                    "size": os.path.getsize(path), "store_relpath": rec.get("backup") or "",
                    "last_verified_at": now})
            ji.save()
            done = sum(1 for x in self.by_seq.values()
                       if x.state in ("committed", "failed", "conflict", "skipped", "missing"))
            EditJob.objects.filter(id=self.job.id).update(done=done, heartbeat_at=now,
                                                          current=ji.plan_item.relpath)
        else:
            EditJob.objects.filter(id=self.job.id).update(heartbeat_at=now)


def run_job(job: EditJob, *, env: WriteEnv | None = None, reader=None) -> EditJob:
    """Run a claimed (running) apply job synchronously, then re-index the touched files."""
    from gama.metadata.readproc import read_files
    from gama.safewrite.writer import WriteItem, run_apply

    env = env or make_env()
    items = list(job.plan.items.filter(action="change"))
    stray = {i.root_path for i in items} - set(configured_root_paths())
    if stray:
        return _finish(job, "failed", f"library roots no longer configured: {sorted(stray)}")
    # The full pre-edit snapshot for the history (the file must still equal sha_before;
    # the engine re-checks that before writing anything).
    snaps = (reader or read_files)([os.path.join(i.root_path, i.relpath) for i in items])
    for i, r in zip(items, snaps):
        JobItem.objects.filter(plan_item=i).update(snapshot_before=r.tags.snapshot if r.ok and r.tags else None)
    witems = [WriteItem(i.seq, i.root_path, i.relpath, i.format, i.sha_before, list(i.genres_before or []),
                        list(i.genres_after or [])) for i in items]
    res = run_apply(witems, env, job_id=job.journal_id, policy=job.policy, observer=_Mirror(job, env))
    sync_recovered(res.recovered, reader=reader)
    for r in res.items:
        JobItem.objects.filter(job=job, plan_item__seq=r.n).update(
            state=r.state, restore_state=r.restore_state or "", stage=r.stage or "", error=r.error or "",
            sha_after=r.sha_after or "", backup_sha=r.backup_sha or "", verification=r.verification)
    EditJob.objects.filter(id=job.id).update(journal_path=res.journal_path or "")
    status = "interrupted" if res.status == "aborted" else res.status
    job = _finish(job, status, res.error or "")
    reindex_job(job, reader=reader)
    return job


def _finish(job: EditJob, status: str, error: str) -> EditJob:
    EditJob.objects.filter(id=job.id).update(status=status, error=error[:4000], finished_at=timezone.now(),
                                             heartbeat_at=timezone.now())
    job.refresh_from_db()
    return job


def reindex_job(job: EditJob, *, reader=None) -> None:
    """Re-read every planned file from disk (whatever happened) and store what is really
    there: the index and JobItem.genres_read_back reflect the files, not the plan."""
    by_root: dict[str, list[PlanItem]] = {}
    for i in job.plan.items.filter(action__in=("change", "restore")):
        by_root.setdefault(i.root_path, []).append(i)
    for root_path, items in by_root.items():
        root = LibraryRoot.objects.filter(path=root_path).first()
        if root is None:
            continue
        results = reindex_paths(root, [i.relpath for i in items], reader=reader)
        for i in items:
            r = results.get(i.relpath)
            JobItem.objects.filter(plan_item=i).update(
                genres_read_back=list(r.genre.values) if r is not None and r.ok and r.genre else None)


def sync_recovered(reports: list[dict], *, reader=None) -> None:
    """Mirror recovery outcomes (journal + hashes) into the jobs they belong to, and
    re-read their files so the index shows what is really on disk."""
    for rep in reports:
        if rep.get("status") != "interrupted" or not rep.get("job"):
            continue
        job = EditJob.objects.filter(id=_uuid(rep["job"])).first()
        if job is None:
            continue
        sync_from_journal(job, rep["journal"])
        reindex_job(job, reader=reader)


def _uuid(hex_id: str):
    import uuid
    try:
        return uuid.UUID(hex=hex_id)
    except ValueError:
        return None


def sync_from_journal(job: EditJob, journal_path: str) -> None:
    """Set the job and item states from its journal (after recovery has resolved it)."""
    try:
        s = summarize(load(journal_path))
    except (OSError, JournalError) as e:
        EditJob.objects.filter(id=job.id).update(error=f"journal unreadable: {e}")
        return
    for ji in job.items.select_related("plan_item"):
        it = s["items"].get(ji.plan_item.seq)
        if it:
            ji.state = it.get("state", ji.state)
            ji.restore_state = it.get("restore_state") or ji.restore_state
            ji.stage = it.get("stage") or ji.stage
            ji.error = it.get("error") or ji.error
            ji.sha_after = it.get("sha_after") or ji.sha_after
            ji.save()
    status = s["status"] if s["status"] in TERMINAL_JOB else job.status
    EditJob.objects.filter(id=job.id).update(status=status, journal_path=journal_path,
                                             finished_at=job.finished_at or timezone.now())


def worker_startup(env: WriteEnv | None = None, *, reader=None) -> list[dict]:
    """Run recovery, then settle jobs a dead worker left as 'running'."""
    from gama.safewrite.recovery import recover

    env = env or make_env()
    reports = recover(env)
    sync_recovered(reports, reader=reader)
    for job in EditJob.objects.filter(status="running"):
        path = Path(env.journal_dir) / f"{job.journal_id}.jsonl"
        if path.exists():
            sync_from_journal(job, str(path))
            job.refresh_from_db()
            if job.status == "running":  # journal still open: recovery is pending (e.g. root not mounted)
                EditJob.objects.filter(id=job.id).update(error="recovery pending; see the worker log")
                continue
        else:
            _finish(job, "interrupted", "the worker stopped before the job started writing; nothing was written")
        reindex_job(job, reader=reader)
    return reports


def heartbeat_stale(job: EditJob) -> bool:
    """True when no worker seems to be handling an unfinished job ("worker not running")."""
    if job.status == "queued":
        ref = job.created_at
    elif job.status == "running":
        ref = job.heartbeat_at
    else:
        return False
    return ref is None or (timezone.now() - ref).total_seconds() > HEARTBEAT_STALE_SECONDS
