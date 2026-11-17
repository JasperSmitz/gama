"""The write worker: runs confirmed edit jobs one at a time (plan 1.8)."""
import os
import time

from django.core.management.base import BaseCommand, CommandError

from gama.edits import services


class Command(BaseCommand):
    help = "Run recovery, then process queued edit jobs in order (polls every second). Only one worker may run."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="process at most one job, then exit")

    def handle(self, *args, once=False, **opts):
        try:
            lock_fd = services.worker_lock()  # held for the life of this process
        except services.WorkerBusy as e:
            raise CommandError(f"{e}; not starting a second one") from None
        try:
            for r in services.worker_startup():
                self.stdout.write(f"recovery: {r.get('job') or r.get('journal') or 'backup store'}: {r['status']}")
            while True:
                job = services.work_once()
                if job is not None:
                    self.stdout.write(f"job {job.id}: {job.status}")
                    if job.status == "queued":  # requeued (write lock busy): back off before retrying
                        time.sleep(1)
                if once:
                    return
                if job is None:
                    time.sleep(1)
        finally:
            os.close(lock_fd)
