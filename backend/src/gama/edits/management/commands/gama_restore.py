"""Restore the files of an edit job from its backups, without the web UI.

Works from the job's journal alone (no database needed), so it is usable when the
web UI or the database is down. Every safety rule of the engine applies: the
backup is verified first, files changed after the edit are not overwritten unless
--force (their current version is backed up first), missing files are not recreated.
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from gama.safewrite import fsutil
from gama.safewrite.env import WriteEnv
from gama.safewrite.journal import JournalError, load, summarize
from gama.safewrite.restore import RestoreItem, run_restore


class Command(BaseCommand):
    help = "Restore the files changed by an edit job (by job or journal id) from the backup store."

    def add_arguments(self, parser):
        parser.add_argument("job", help="job id (UUID, with or without dashes)")
        parser.add_argument("--items", help="comma-separated item numbers (default: every changed file)")
        parser.add_argument("--force", action="store_true", help="also restore files changed after the edit")
        parser.add_argument("--dry-run", action="store_true", help="only show what would happen")

    def handle(self, *args, job, items=None, force=False, dry_run=False, **opts):
        cfg = settings.GAMA
        env = WriteEnv(cfg.data_dir, margin_bytes=cfg.backup_margin_mb << 20, audio=cfg.verify_audio)
        jid = job.replace("-", "").lower()
        path = os.path.join(env.journal_dir, f"{jid}.jsonl")
        try:
            s = summarize(load(path))
        except FileNotFoundError:
            raise CommandError(f"no journal for job {job} in {env.journal_dir}") from None
        except JournalError as e:
            raise CommandError(f"journal is damaged: {e}") from None
        header = s["header"] or {}
        if header.get("kind") != "apply":
            raise CommandError("only edit (apply) jobs can be restored")
        wanted = None if not items else {int(x) for x in items.split(",") if x.strip()}
        ritems = []
        for hi in header.get("items", []):
            n = hi["n"]
            sha_after = s["items"].get(n, {}).get("sha_after")
            if sha_after is None or (wanted is not None and n not in wanted):
                continue
            ritems.append(RestoreItem(n, hi["root"], hi["relpath"], hi["sha_before"], sha_after, force=force))
        if not ritems:
            self.stdout.write("nothing to restore: no file of this job was changed")
            return
        if dry_run:
            for it in ritems:
                self.stdout.write(f"[{it.n}] {it.relpath}: {self._would(env, it)}")
            return
        res = run_restore(ritems, env)
        for o in res.items:
            self.stdout.write(f"[{o.n}] {o.relpath}: {o.state}{' - ' + o.error if o.error else ''}")
        self.stdout.write(f"restore job {res.job_id}: {res.status}"
                          f"{' - ' + res.error if res.error else ''}")
        self.stdout.write("Run `manage.py gama_scan` to refresh the index.")
        if res.status not in ("succeeded",):
            raise CommandError(f"restore {res.status}")

    @staticmethod
    def _would(env: WriteEnv, it: RestoreItem) -> str:
        if not env.store.verify(it.sha_before):
            return "conflict: backup missing or damaged (not touched)"
        try:
            cur = fsutil.sha256_file(it.path)
        except FileNotFoundError:
            return "conflict: file missing (never recreated)"
        if cur == it.sha_before:
            return "nothing to do (already the original)"
        if cur == it.sha_after:
            return "restore"
        return "restore (forced; current version backed up first)" if it.force else \
            "conflict: changed after the edit (not overwritten without --force)"
