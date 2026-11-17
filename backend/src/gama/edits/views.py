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


def plan(request, plan_id):
    ep = get_object_or_404(EditPlan, id=plan_id)
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
    ep = get_object_or_404(EditPlan, id=plan_id)
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
    ep = get_object_or_404(EditPlan, id=plan_id)
    try:
        new = services.refresh_plan(ep, request.user)
    except PlanError as e:
        messages.error(request, f"Cannot refresh the preview: {e}")
        return redirect("edits:plan", plan_id=ep.id)
    messages.info(request, "A new preview was built from the files as they are now.")
    return redirect("edits:plan", plan_id=new.id)


def job(request, job_id):
    j = get_object_or_404(EditJob.objects.select_related("plan"), id=job_id)
    items = j.items.select_related("plan_item")
    return render(request, "edits/job.html", {
        "job": j, "items": items, "stale": services.heartbeat_stale(j), "explain": _explain(),
        "auto_refresh": j.status in ("queued", "running")})
