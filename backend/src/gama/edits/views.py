from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from gama.library import basket
from gama.planning.planner import NOTES, PLAN_SKIPS, PlanError
from gama.metadata.policy import SKIP, WARNINGS

from . import services
from .models import EditJob, EditPlan

PERM = "edits.apply_edits"


def _explain():
    out = {}
    out.update(SKIP)
    out.update(WARNINGS)
    out.update(NOTES)
    out.update(PLAN_SKIPS)
    out.update({"backup-bad": "The backup is missing or damaged; the file is not touched",
                "missing": "The file is not there any more; it is never recreated",
                "unreadable": "The file cannot be read",
                "changed-since-edit": "The file changed after Gama's edit; it is not overwritten unless forced"})
    return out


def operation(request):
    albums = basket.albums(request)
    return render(request, "edits/operation.html", {"albums": albums})


def replace(request):
    albums = basket.albums(request)
    return render(request, "edits/replace.html", {"albums": albums})


@require_POST
def create_plan(request):
    op = request.POST.get("operation", "")
    if op in ("add", "remove"):
        genres = [request.POST.get("genre", "")]
    elif op == "replace":
        if request.POST.get("replace_ack") != "yes":
            messages.error(request, "Tick the box to confirm that you want to replace all genres.")
            return redirect("edits:replace")
        genres = [g for g in request.POST.get("genres", "").splitlines() if g.strip()]
    else:
        messages.error(request, "Choose an operation.")
        return redirect("edits:operation")
    ids = basket.ids(request)
    try:
        ep = services.create_plan(request.user, ids, op, genres)
    except PlanError as e:
        messages.error(request, f"Cannot build the preview: {e}")
        return redirect("edits:replace" if op == "replace" else "edits:operation")
    return redirect("edits:plan", plan_id=ep.id)


def _plans(user):
    """Plans and jobs are personal; staff and superusers see everyone's."""
    qs = EditPlan.objects.all()
    return qs if (user.is_staff or user.is_superuser) else qs.filter(created_by=user)


def _jobs(user):
    qs = EditJob.objects.all()
    return qs if (user.is_staff or user.is_superuser) else qs.filter(created_by=user)


def plan(request, plan_id):
    ep = get_object_or_404(_plans(request.user), id=plan_id)
    groups: dict[str, dict] = {}
    for it in ep.items.all():
        g = groups.setdefault(it.album_key, {"label": it.album_label, "items": []})
        g["items"].append(it)
    job = getattr(ep, "job", None) if ep.status == "confirmed" else None
    return render(request, "edits/plan.html", {
        "plan": ep, "groups": list(groups.values()), "job": job, "explain": _explain(),
        "can_apply": request.user.has_perm(PERM)})


@require_POST
def confirm(request, plan_id):
    if not request.user.has_perm(PERM):
        raise PermissionDenied("You may preview edits but not apply them.")
    ep = get_object_or_404(_plans(request.user), id=plan_id)
    policy = "continue" if request.POST.get("continue_on_error") == "yes" else "stop_rollback"
    try:
        job = services.confirm_plan(ep, request.user, request.POST.get("digest", ""), policy=policy,
                                    replace_confirmed=request.POST.get("replace_ack") == "yes")
    except services.ConfirmError as e:
        messages.error(request, str(e))
        return redirect("edits:plan", plan_id=ep.id)
    return redirect("edits:job", job_id=job.id)


@require_POST
def refresh(request, plan_id):
    ep = get_object_or_404(_plans(request.user), id=plan_id)
    try:
        new = services.refresh_plan(ep, request.user)
    except PlanError as e:
        messages.error(request, f"Cannot refresh the preview: {e}")
        return redirect("edits:plan", plan_id=ep.id)
    messages.info(request, "A new preview was built from the files as they are now.")
    return redirect("edits:plan", plan_id=new.id)


def job(request, job_id):
    from .models import Backup
    j = get_object_or_404(_jobs(request.user).select_related("plan", "created_by"), id=job_id)
    items = list(j.items.select_related("plan_item"))
    backups = {b.sha256: b for b in Backup.objects.filter(sha256__in=[i.backup_sha for i in items if i.backup_sha])}
    for ji in items:
        ji.backup = backups.get(ji.backup_sha)
    restorable = set(services.restorable_items(j).values_list("id", flat=True))
    return render(request, "edits/job.html", {
        "job": j, "items": items, "outcome": _outcome(j, items), "stale": services.heartbeat_stale(j), "explain": _explain(),
        "auto_refresh": j.status in ("queued", "running") and not j.recovery_pending,
        "restorable": restorable, "can_apply": request.user.has_perm(PERM),
        "restore_jobs": _jobs(request.user).filter(plan__source_job=j).order_by("created_at"),
        "data_dir": str(services.settings.GAMA.data_dir)})


def _outcome(j, items) -> list[str]:
    """One line per outcome for a finished job: what is changed on disk now, what was
    rolled back, what failed and what was never attempted."""
    if not j.finished or j.status == "conflict":
        return []
    done = "restored" if j.kind == "restore" else "changed"
    counts = {}
    for ji in items:
        undone = ji.restore_state in ("restored", "nothing_to_do")
        if ji.state in ("committed", "restored"):
            key = f"{done} and rolled back" if undone else (
                f"{done} (rollback failed: {ji.restore_state})" if ji.restore_state else done)
        elif ji.state == "failed":
            key = "failed (rolled back)" if ji.restore_state == "restored" else "failed"
        elif ji.state in ("planned", "skipped"):
            key = "not attempted"
        else:
            key = ji.state
        counts[key] = counts.get(key, 0) + 1
    return [f"{n} {k}" for k, n in counts.items()]


def history(request):
    from django.core.paginator import Paginator
    jobs = _jobs(request.user).select_related("plan", "created_by").order_by("-created_at")
    page = Paginator(jobs, 50).get_page(request.GET.get("page"))
    # Pending recoveries concern the whole library, so everyone is told about them.
    pending = EditJob.objects.filter(recovery_pending=True)
    return render(request, "edits/history.html", {
        "page": page, "pending": pending, "store_size": services.backup_store_size()})


@require_POST
def restore(request, job_id):
    j = get_object_or_404(_jobs(request.user), id=job_id)
    ids = None
    if request.POST.get("scope") == "selected":
        ids = [int(v) for v in request.POST.getlist("item") if v.isdigit()]
        if not ids:
            messages.error(request, "Tick at least one file to restore.")
            return redirect("edits:job", job_id=j.id)
    try:
        ep = services.create_restore_plan(request.user, j, ids, force=request.POST.get("force") == "yes")
    except PlanError as e:
        messages.error(request, f"Cannot build the restore preview: {e}")
        return redirect("edits:job", job_id=j.id)
    return redirect("edits:plan", plan_id=ep.id)
