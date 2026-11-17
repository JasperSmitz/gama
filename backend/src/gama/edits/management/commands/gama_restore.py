"""Restore files from backups, without the web UI.

Works from the job's journal alone (no database needed), so it is usable when the
web UI or the database is down. Every safety rule of the engine applies: the
backup is verified first, files that changed are not overwritten unless forced,
missing files are never recreated.

* An edit (apply) job: brings back the original of every changed file.
* A restore job: brings back the later versions that a forced restore replaced
  (they were backed up before being replaced).

Forcing is bound to an exact version: ``--force`` needs ``--expect-sha N:SHA`` for
each file to force, with the current hash shown by ``--dry-run``. A file that is
anything else by then is left alone.
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from gama.safewrite import fsutil
from gama.safewrite.env import WriteEnv
from gama.safewrite.journal import JournalError, load, summarize
from gama.safewrite.restore import RestoreItem, run_restore


class Command(BaseCommand):
    help = "Restore the files of an edit job (originals) or a restore job (versions replaced by force) from backups."
    requires_system_checks = []  # must work while the database is unavailable

    def add_arguments(self, parser):
        parser.add_argument("job", help="job id (UUID, with or without dashes)")
        parser.add_argument("--items", help="comma-separated item numbers (default: all)")
        parser.add_argument("--force", action="store_true",
                            help="also replace files that changed; each one needs --expect-sha")
        parser.add_argument("--expect-sha", action="append", default=[], metavar="N:SHA",
                            help="with --force: the current sha256 of item N (as shown by --dry-run)")
        parser.add_argument("--dry-run", action="store_true", help="only show what would happen")

    def handle(self, *args, job, items=None, force=False, expect_sha=(), dry_run=False, **opts):
        cfg = settings.GAMA
        env = WriteEnv(cfg.data_dir, margin_bytes=cfg.backup_margin_mb << 20, audio=cfg.verify_audio)
        wanted = self._parse_items(items)
        expected = self._parse_expect(expect_sha)
        if force and not expected and not dry_run:
            raise CommandError("--force needs --expect-sha N:SHA for each file to replace "
                               "(run with --dry-run to see the current hashes)")
        jid = job.replace("-", "").lower()
        path = os.path.join(env.journal_dir, f"{jid}.jsonl")
        try:
            s = summarize(load(path))
        except FileNotFoundError:
            raise CommandError(f"no journal for job {job} in {env.journal_dir}") from None
        except (OSError, JournalError) as e:
            raise CommandError(f"cannot read the journal: {e}") from None
        header = s["header"] or {}
        kind = header.get("kind")
        ritems = []
        for hi in header.get("items", []):
            n = hi["n"]
            st = s["items"].get(n, {})
            if wanted is not None and n not in wanted:
                continue
            if kind == "apply":
                if st.get("sha_after") is None:
                    continue
                target, current = hi["sha_before"], st["sha_after"]
            elif kind == "restore":
                if not st.get("forced_backup") or st.get("state") != "restored":
                    continue
                target, current = st["forced_backup"], hi["sha_before"]
            else:
                raise CommandError(f"unknown journal kind {kind!r}")
            exp = expected.get(n)
            ritems.append(RestoreItem(n, hi["root"], hi["relpath"], target, current,
                                      force=force and exp is not None, sha_expected_current=exp))
        if not ritems:
            self.stdout.write("nothing to restore")
            return
        if dry_run:
            for it in ritems:
                self.stdout.write(f"[{it.n}] {it.relpath}: {self._would(env, it, force)}")
            return
        res = run_restore(ritems, env)
        for o in res.items:
            self.stdout.write(f"[{o.n}] {o.relpath}: {o.state}{' - ' + o.error if o.error else ''}"
                              f"{f' (replaced version kept as backup {o.forced_backup_sha})' if o.forced_backup_sha else ''}")
        self.stdout.write(f"restore job {res.job_id}: {res.status}{' - ' + res.error if res.error else ''}")
        self.stdout.write("Run `manage.py gama_scan` to refresh the index.")
        if res.status != "succeeded":
            raise CommandError(f"restore {res.status}")

    @staticmethod
    def _parse_items(items):
        if not items:
            return None
        try:
            return {int(x) for x in items.split(",") if x.strip()}
        except ValueError:
            raise CommandError(f"--items must be comma-separated numbers, got {items!r}") from None

    @staticmethod
    def _parse_expect(values):
        out = {}
        for v in values:
            n, _, sha = v.partition(":")
            if not n.strip().isdigit() or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                raise CommandError(f"--expect-sha must look like N:<64 hex digits>, got {v!r}")
            out[int(n)] = sha
        return out

    @staticmethod
    def _would(env: WriteEnv, it: RestoreItem, force: bool) -> str:
        if not env.store.verify(it.sha_before):
            return "conflict: backup missing or damaged (not touched)"
        try:
            cur = fsutil.sha256_file(it.path)
        except FileNotFoundError:
            return "conflict: file missing (never recreated)"
        except OSError as e:
            return f"conflict: cannot read the file ({e.strerror or e})"
        if cur == it.sha_before:
            return "nothing to do (already that version)"
        if cur == it.sha_after:
            return "restore"
        if force and it.sha_expected_current == cur:
            return "restore (forced; the current version is backed up first)"
        return (f"conflict: changed (current sha256 {cur}); to replace exactly this version use "
                f"--force --expect-sha {it.n}:{cur}")
