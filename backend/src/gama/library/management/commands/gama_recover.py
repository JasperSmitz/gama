"""Run crash recovery, or explicitly resolve a job whose recovery stays pending."""
from django.core.management.base import BaseCommand, CommandError

from gama.safewrite.env import WriteEnv
from gama.safewrite.recovery import recover, resolve_pending


class Command(BaseCommand):
    help = ("Run crash recovery and report unfinished jobs. With --resolve JOB, resolve a job whose recovery is "
            "pending (e.g. a retired library root or a renamed album folder): every file is re-checked; files "
            "that are not there are only recorded as missing with --confirm-missing.")

    def add_arguments(self, parser):
        parser.add_argument("--resolve", metavar="JOB_ID", help="journal id of the pending job")
        parser.add_argument("--confirm-missing", action="store_true",
                            help="record files that are not there as missing and close the job")

    def handle(self, *args, resolve=None, confirm_missing=False, **opts):
        from django.conf import settings
        cfg = settings.GAMA
        env = WriteEnv(cfg.data_dir, margin_bytes=cfg.backup_margin_mb << 20, audio=cfg.verify_audio)
        if resolve:
            try:
                r = resolve_pending(env, resolve, confirm_missing=confirm_missing)
            except FileNotFoundError:
                raise CommandError(f"no journal for job {resolve}") from None
            self.stdout.write(f"{r['job']}: {r['status']}")
            for p in r.get("unavailable", []):
                self.stdout.write(f"  not available: {p}")
            if r["status"] == "pending":
                self.stdout.write("  nothing was changed; re-run with --confirm-missing to close the job")
            return
        reports = recover(env)
        if not reports:
            self.stdout.write("nothing to recover")
        for r in reports:
            self.stdout.write(f"{r.get('job') or r.get('journal') or 'backup store'}: {r['status']}"
                              f"{' - ' + r['error'] if r.get('error') else ''}")
