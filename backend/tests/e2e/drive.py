"""Drive the web UI in its own process, against the same file database as a real
gama_worker subprocess (e2e test helper; never imported by the test process).

Usage: drive.py apply GENRE TITLE... | preview GENRE TITLE... | restore JOB_ID | job JOB_ID
Prints one JSON object.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "gama.settings")

import django  # noqa: E402

django.setup()
from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.auth.models import Permission  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
from gama.edits.models import EditJob, EditPlan, JobItem  # noqa: E402
from gama.library.models import Album, Track  # noqa: E402


def client() -> Client:
    user, _ = get_user_model().objects.get_or_create(username="e2e")
    user.user_permissions.add(Permission.objects.get(codename="apply_edits"))
    c = Client()
    c.force_login(user)
    return c


def select(c, titles):
    ids = [Album.objects.get(album=t).id for t in titles]
    assert c.post("/library/basket/add/", {"album": ids}).status_code == 302
    return ids


def plan_from(r):
    assert r.status_code == 302 and "/edits/plans/" in r["Location"], r.content[:500]
    return EditPlan.objects.get(id=r["Location"].rstrip("/").split("/")[-1])


def main(cmd, *args):
    c = client()
    if cmd in ("apply", "preview"):
        genre, titles = args[0], args[1:]
        select(c, titles)
        plan = plan_from(c.post("/edits/plans/new/", {"operation": "add", "genre": genre}))
        page = c.get(f"/edits/plans/{plan.id}/").content.decode()
        out = {"plan": str(plan.id), "n_change": plan.n_change, "n_nochange": plan.n_nochange,
               "n_skip": plan.n_skip, "files": [i.relpath for i in plan.items.all()],
               "listed": all(i.relpath in page for i in plan.items.all()),
               "expected": {i.relpath: i.genres_after for i in plan.items.filter(action="change")}}
        if cmd == "apply":
            r = c.post(f"/edits/plans/{plan.id}/confirm/", {"digest": plan.digest})
            out["job"] = r["Location"].rstrip("/").split("/")[-1]
        return out
    if cmd == "restore":
        job = EditJob.objects.get(id=args[0])
        plan = plan_from(c.post(f"/edits/jobs/{job.id}/restore/", {"scope": "all"}))
        r = c.post(f"/edits/plans/{plan.id}/confirm/", {"digest": plan.digest})
        return {"plan": str(plan.id), "n_change": plan.n_change, "job": r["Location"].rstrip("/").split("/")[-1]}
    if cmd == "job":
        job = EditJob.objects.get(id=args[0])
        page = c.get(f"/edits/jobs/{job.id}/").content.decode()
        items = [{"relpath": ji.plan_item.relpath, "state": ji.state, "requested": ji.genres_requested,
                  "read_back": ji.genres_read_back, "verified": bool(ji.verification and ji.verification["ok"]),
                  "audio": (ji.verification or {}).get("audio"),
                  "index": Track.objects.get(file__relpath=ji.plan_item.relpath).genres}
                 for ji in JobItem.objects.filter(job=job).select_related("plan_item")]
        return {"status": job.status, "kind": job.kind, "items": items, "page_ok": "Read back from disk" in page,
                "history_lists": str(job.id)[:8] in c.get("/edits/history/").content.decode()}
    raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    print(json.dumps(main(*sys.argv[1:])))
