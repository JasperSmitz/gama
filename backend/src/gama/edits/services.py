"""Glue between the web UI and the framework-free planner / safe write engine.

Nothing here writes to library files itself: plans only read; jobs hand the
work to ``safewrite.run_apply``, which runs in the worker process.
"""
from __future__ import annotations

import os
from dataclasses import asdict
from datetime import timedelta
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
    if ep.operation == "restore":
        return restore_digest(ep)
    return digest(ep.operation, ep.genres, file_plans(ep), ep.warnings)


def restore_digest(ep: EditPlan) -> str:
    import hashlib
    import json
    rows = [{"seq": i.seq, "root": i.root_path, "relpath": i.relpath, "action": i.action, "reason": i.reason,
             "sha_before": i.sha_before, "sha_after_expected": i.sha_after_expected, "force": i.force,
             "genres_before": i.genres_before, "genres_after": i.genres_after, "warnings": list(i.warnings)}
            for i in ep.items.all()]
    blob = json.dumps({"op": "restore", "source_job": str(ep.source_job_id), "warnings": ep.warnings, "items": rows},
                      sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


RESTORABLE_STATES = ("committed", "missing", "failed", "conflict")


def restorable_items(job: EditJob):
    """Items of an apply job whose file Gama may have changed (a sha_after was journaled)."""
    return job.items.select_related("plan_item").exclude(sha_after="").filter(state__in=RESTORABLE_STATES)


def create_restore_plan(user, job: EditJob, item_ids: list[int] | None = None, *, force: bool = False,
                        reader=None) -> EditPlan:
    """Restore preview: for each selected file, check the backup and the current file.

    restore  - the file still holds Gama's edit (or force: a later version, backed up first)
    nothing  - the file is already the original
    conflict - the file changed after the edit, is missing, or the backup is bad: not touched
    Nothing is written; the restore runs only after confirmation, in the worker.
    """
    from gama.metadata.readproc import read_files
    from gama.safewrite import fsutil

    if job.kind != "apply":
        raise PlanError("only edit jobs can be restored")
    items = list(restorable_items(job))
    if item_ids is not None:
        items = [ji for ji in items if ji.id in set(item_ids)]
    if not items:
        raise PlanError("nothing to restore: no file of this job was changed")
    stray = {ji.plan_item.root_path for ji in items} - set(configured_root_paths())
    if stray:
        raise PlanError(f"files under library roots that are not configured: {sorted(stray)}")
    env = make_env()
    paths = [os.path.join(ji.plan_item.root_path, ji.plan_item.relpath) for ji in items]
    reads = (reader or read_files)(paths)
    rows = []
    for seq, (ji, path, rr) in enumerate(zip(items, paths, reads)):
        pi = ji.plan_item
        cur = None
        try:
            cur = fsutil.sha256_file(path)
        except FileNotFoundError:
            pass
        except OSError:
            cur = "unreadable"
        warnings: list[str] = []
        if not env.store.verify(pi.sha_before):
            action, reason = "conflict", "backup-bad"
        elif cur is None:
            action, reason = "conflict", "missing"
        elif cur == "unreadable":
            action, reason = "conflict", "unreadable"
        elif cur == pi.sha_before:
            action, reason = "nothing", None
        elif cur == ji.sha_after:
            action, reason = "restore", None
        elif force:
            action, reason = "restore", None
            warnings.append("forced: the later version is backed up before it is replaced")
        else:
            action, reason = "conflict", "changed-since-edit"
        rows.append(PlanItem(seq=seq, album=pi.album, album_key=pi.album_key, album_label=pi.album_label,
                             root_path=pi.root_path, relpath=pi.relpath, format=pi.format,
                             sha_before=pi.sha_before, sha_after_expected=ji.sha_after,
                             genres_before=list(rr.genre.values) if rr.ok and rr.genre else None,
                             genres_after=pi.genres_before, style=rr.genre.style if rr.ok and rr.genre else None,
                             action=action, reason=reason, warnings=warnings,
                             force=force and action == "restore" and cur != ji.sha_after, source_item=ji))
    with transaction.atomic():
        ep = EditPlan.objects.create(created_by=user, operation="restore", genres=[], album_ids=job.plan.album_ids,
                                     album_labels=job.plan.album_labels, source_job=job, digest="",
                                     n_change=sum(r.action == "restore" for r in rows),
                                     n_nochange=sum(r.action == "nothing" for r in rows),
                                     n_skip=sum(r.action == "conflict" for r in rows))
        for r in rows:
            r.plan = ep
        PlanItem.objects.bulk_create(rows)
        ep.digest = restore_digest(ep)
        ep.save(update_fields=["digest"])
    return ep


def refresh_plan(ep: EditPlan, user, *, reader=None) -> EditPlan:
    """A new plan for the same albums and request. The old one is superseded when it is
    a draft, or when its job ended in a conflict (plan 1.5.4)."""
    if ep.operation == "restore":
        sources = [i.source_item_id for i in ep.items.all() if i.source_item_id]
        new = create_restore_plan(user, ep.source_job, sources, force=any(i.force for i in ep.items.all()),
                                  reader=reader)
    else:
        new = create_plan(user, list(ep.album_ids), ep.operation, list(ep.genres), reader=reader)
    job = EditJob.objects.filter(plan=ep).first()
    if ep.status == "draft" or (job is not None and job.status == "conflict"):
        EditPlan.objects.filter(id=ep.id).update(status="superseded")
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
            kind = "restore" if ep.operation == "restore" else "apply"
            work = "restore" if kind == "restore" else "change"
            job = EditJob.objects.create(plan=ep, kind=kind, created_by=user, policy=policy,
                                         total=ep.n_change)
            JobItem.objects.bulk_create([
                JobItem(job=job, plan_item=i, sha_before=i.sha_before or "", genres_requested=i.genres_after or [])
                for i in ep.items.filter(action=work)])
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
                       if x.state in ("committed", "failed", "conflict", "skipped", "missing", "restored",
                                      "restore_failed", "restore_conflict", "nothing_to_do"))
            EditJob.objects.filter(id=self.job.id).update(done=done, heartbeat_at=now,
                                                          current=ji.plan_item.relpath)
        else:
            EditJob.objects.filter(id=self.job.id).update(heartbeat_at=now)


HEARTBEAT_INTERVAL = 10.0


class _Heartbeat:
    """Keeps job.heartbeat_at fresh while a job runs, also in phases that write no
    journal record (snapshot reads, preflight hashing, backup verification)."""

    def __init__(self, job_id, interval: float = HEARTBEAT_INTERVAL):
        import threading
        self.job_id, self.interval = job_id, interval
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="gama-heartbeat")

    def _run(self) -> None:
        from django.db import connection
        try:
            while not self.stop.wait(self.interval):
                try:
                    EditJob.objects.filter(id=self.job_id, status="running").update(heartbeat_at=timezone.now())
                except Exception:  # noqa: BLE001 - a busy database must not stop the job
                    pass
        finally:
            connection.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=30)


def run_job(job: EditJob, *, env: WriteEnv | None = None, reader=None) -> EditJob:
    """Run a claimed (running) job synchronously, then re-index the touched files."""
    with _Heartbeat(job.id):
        return _run_job(job, env=env, reader=reader)


def _run_job(job: EditJob, *, env: WriteEnv | None = None, reader=None) -> EditJob:
    from gama.metadata.readproc import read_files
    from gama.safewrite.writer import WriteItem, run_apply

    env = env or make_env()
    if job.kind == "restore":
        return _run_restore_job(job, env, reader)
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
    if res.lock_busy:
        return requeue(job, "another write or restore job was running (for example gama_restore); will retry")
    sync_recovered(res.recovered, reader=reader)
    for r in res.items:
        JobItem.objects.filter(job=job, plan_item__seq=r.n).update(
            state=r.state, restore_state=r.restore_state or "", stage=r.stage or "", error=r.error or "",
            sha_after=r.sha_after or "", backup_sha=r.backup_sha or "", verification=r.verification)
    EditJob.objects.filter(id=job.id).update(journal_path=res.journal_path or "")
    job = _finish_result(job, res.status, res.error or "")
    safe_reindex(job, reader=reader)
    return job


def _finish_result(job: EditJob, status: str, error: str) -> EditJob:
    if status == "aborted":
        # The journal stays open; the next recovery resolves every file by hash.
        EditJob.objects.filter(id=job.id).update(
            recovery_pending=True,
            recovery_note="The job stopped because its journal could not be written (disk full?). "
                          "Restart the worker after fixing that; recovery then checks every file by its content.")
        return _finish(job, "interrupted", error)
    return _finish(job, status, error)


def _run_restore_job(job: EditJob, env: WriteEnv, reader) -> EditJob:
    from gama.safewrite.restore import RestoreItem, run_restore

    items = list(job.plan.items.filter(action="restore"))
    stray = {i.root_path for i in items} - set(configured_root_paths())
    if stray:
        return _finish(job, "failed", f"library roots no longer configured: {sorted(stray)}")
    ritems = [RestoreItem(i.seq, i.root_path, i.relpath, i.sha_before, i.sha_after_expected, force=i.force)
              for i in items]
    res = run_restore(ritems, env, job_id=job.journal_id, observer=_Mirror(job, env))
    if res.lock_busy:
        return requeue(job, "another write or restore job was running; will retry")
    sync_recovered(res.recovered, reader=reader)
    for o in res.items:
        JobItem.objects.filter(job=job, plan_item__seq=o.n).update(state=o.state, error=o.error or "")
    EditJob.objects.filter(id=job.id).update(journal_path=res.journal_path or "")
    job = _finish_result(job, res.status, res.error or "")
    safe_reindex(job, reader=reader)
    return job


def requeue(job: EditJob, why: str) -> EditJob:
    """Put a job back in the queue (nothing was written)."""
    EditJob.objects.filter(id=job.id).update(status="queued", worker_pid=None, started_at=None,
                                             error=why[:4000])
    job.refresh_from_db()
    return job


def safe_reindex(job: EditJob, *, reader=None) -> None:
    """Re-index after a job; a failure is recorded on the job, never raised."""
    try:
        reindex_job(job, reader=reader)
    except Exception as e:  # noqa: BLE001
        _append_error(job, f"re-reading the files into the index failed ({type(e).__name__}: {e}); "
                           "run manage.py gama_scan")


def _append_error(job: EditJob, msg: str) -> None:
    cur = EditJob.objects.filter(id=job.id).values_list("error", flat=True).first() or ""
    EditJob.objects.filter(id=job.id).update(error=(cur + ("\n" if cur else "") + msg)[:4000])


def handle_job_error(job: EditJob, exc: BaseException, env: WriteEnv | None = None) -> EditJob:
    """The worker hit an unexpected error while running ``job``: make its record honest.

    With a finished journal the job is synced from it. With an open journal (or none)
    the job is marked interrupted; the next recovery (worker start or next job)
    resolves its files by hash and re-syncs it.
    """
    env = env or make_env()
    msg = f"worker error: {type(exc).__name__}: {exc}"
    path = Path(env.journal_dir) / f"{job.journal_id}.jsonl"
    try:
        if path.exists():
            sync_from_journal(job, str(path))
            job.refresh_from_db()
        if job.status in ("running", "queued"):
            _finish(job, "interrupted", "")
            msg += "; the files will be checked by content at the next recovery"
        _append_error(job, msg)
        job.refresh_from_db()
    except Exception:  # noqa: BLE001 - the database itself may be the problem; recovery will settle it
        pass
    return job


def work_once(env: WriteEnv | None = None, *, reader=None) -> EditJob | None:
    """Claim and run the oldest queued job. Never raises; returns the job (or None)."""
    job = next_queued()
    if job is None or not claim(job.id):
        return None
    job.refresh_from_db()
    try:
        return run_job(job, env=env, reader=reader)
    except Exception as e:  # noqa: BLE001 - the worker loop must survive
        return handle_job_error(job, e, env)


class WorkerBusy(RuntimeError):
    pass


def worker_lock(env: WriteEnv | None = None):
    """Process-lifetime lock: only one gama_worker may run. Returns the open fd (keep it)."""
    import fcntl
    env = env or make_env()
    fd = os.open(os.path.join(env.lock_dir, "worker.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise WorkerBusy("another gama_worker is already running") from None
    return fd


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
        if not rep.get("job"):
            continue
        job = EditJob.objects.filter(id=_uuid(rep["job"])).first()
        if job is None:
            continue
        if rep.get("status") in ("pending", "damaged"):
            EditJob.objects.filter(id=job.id).update(recovery_pending=True,
                                                     recovery_note=rep.get("error") or rep["status"])
            continue
        if rep.get("status") != "interrupted":
            continue
        sync_from_journal(job, rep["journal"])
        EditJob.objects.filter(id=job.id).update(recovery_pending=False, recovery_note="")
        reindex_job(job, reader=reader)


def _uuid(hex_id: str):
    import uuid
    try:
        return uuid.UUID(hex=hex_id)
    except ValueError:
        return None


DONE_STATES = ("committed", "failed", "conflict", "skipped", "missing")


def sync_from_journal(job: EditJob, journal_path: str) -> None:
    """Set the job and item states from its journal (after recovery has resolved it)."""
    try:
        s = summarize(load(journal_path))
    except (OSError, JournalError) as e:
        _append_error(job, f"journal unreadable: {e}")
        return
    done = 0
    for ji in job.items.select_related("plan_item"):
        it = s["items"].get(ji.plan_item.seq)
        if it:
            ji.state = it.get("state", ji.state)
            ji.restore_state = it.get("restore_state") or ji.restore_state
            ji.stage = it.get("stage") or ji.stage
            ji.error = it.get("error") or ji.error
            ji.sha_after = it.get("sha_after") or ji.sha_after
            if it.get("verification"):
                ji.verification = it["verification"]
            if it.get("backup"):
                ji.backup_sha = ji.sha_before  # the store is content-addressed
            ji.save()
        done += ji.state in DONE_STATES
    status = s["status"] if s["status"] in TERMINAL_JOB else job.status
    EditJob.objects.filter(id=job.id).update(status=status, journal_path=journal_path, done=done,
                                             finished_at=job.finished_at or timezone.now())


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def worker_startup(env: WriteEnv | None = None, *, reader=None) -> list[dict]:
    """Run recovery, then settle jobs a dead worker left as 'running'.

    The caller holds the worker lock (one worker at a time). As a second line of
    defence, a running job whose worker process is alive with a fresh heartbeat is
    left alone.
    """
    from gama.safewrite.env import LockBusy
    from gama.safewrite.recovery import recover

    env = env or make_env()
    try:
        reports = recover(env)
    except LockBusy:
        return []  # a write job is running right now (e.g. gama_restore); recovery runs before every job anyway
    sync_recovered(reports, reader=reader)
    for rep in reports:  # mirror pending / damaged recoveries onto their jobs
        if rep.get("status") in ("pending", "damaged") and rep.get("job"):
            job = EditJob.objects.filter(id=_uuid(rep["job"])).first()
            if job is not None:
                _append_error(job, f"recovery {rep['status']}: {rep.get('error', '')}")
    for job in EditJob.objects.filter(status="running"):
        if _alive(job.worker_pid) and not heartbeat_stale(job):
            continue
        path = Path(env.journal_dir) / f"{job.journal_id}.jsonl"
        if path.exists():
            sync_from_journal(job, str(path))
            job.refresh_from_db()
            if job.status == "running":  # journal still open: recovery is pending (e.g. root not mounted)
                EditJob.objects.filter(id=job.id).update(
                    status="interrupted", recovery_pending=True,
                    recovery_note=job.recovery_note or "recovery could not finish; see the worker log")
                continue
        else:
            _finish(job, "interrupted", "the worker stopped before the job started writing; nothing was written")
        safe_reindex(job, reader=reader)
    return reports


def heartbeat_stale(job: EditJob) -> bool:
    """True when no worker seems to be handling an unfinished job ("worker not running")."""
    if job.status == "queued":
        # A queued job waits behind a running one: the worker is alive if that job's heartbeat is fresh.
        cutoff = timezone.now() - timedelta(seconds=HEARTBEAT_STALE_SECONDS)
        if EditJob.objects.filter(status="running", heartbeat_at__gte=cutoff).exists():
            return False
        ref = job.created_at
    elif job.status == "running":
        ref = job.heartbeat_at
    else:
        return False
    return ref is None or (timezone.now() - ref).total_seconds() > HEARTBEAT_STALE_SECONDS


def backup_store_size() -> int:
    from django.db.models import Sum
    return Backup.objects.aggregate(n=Sum("size"))["n"] or 0
