"""The write worker: runs confirmed edit jobs one at a time (plan 1.8)."""
import time

from django.core.management.base import BaseCommand

from gama.edits import services


class Command(BaseCommand):
    help = "Run recovery, then process queued edit jobs in order (polls every second)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="process at most one job, then exit")

    def handle(self, *args, once=False, **opts):
        for r in services.worker_startup():
            self.stdout.write(f"recovery: {r.get('job') or r.get('journal') or 'backup store'}: {r['status']}")
        while True:
            job = services.next_queued()
            if job is not None and services.claim(job.id):
                job.refresh_from_db()
                self.stdout.write(f"job {job.id}: running ({job.total} file(s))")
                job = services.run_job(job)
                self.stdout.write(f"job {job.id}: {job.status}")
                if once:
                    return
                continue
            if once:
                return
            time.sleep(1)
