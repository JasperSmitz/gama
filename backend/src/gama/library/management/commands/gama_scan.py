"""Index the configured library roots (read-only; never writes to the library)."""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from gama.library.scanner import ScanError, ensure_roots, scan_root


class Command(BaseCommand):
    help = "Scan the configured library roots (or one of them) and update the index."

    def add_arguments(self, parser):
        parser.add_argument("--root", help="Scan only this configured root")
        parser.add_argument("--allow-empty", action="store_true",
                            help="Accept a root without audio files and drop its index (default: refuse)")

    def handle(self, *args, root=None, allow_empty=False, **opts):
        roots = [str(r) for r in settings.GAMA.library_roots]
        if not roots:
            raise CommandError("No library roots configured (library_roots in gama.toml or GAMA_LIBRARY_ROOTS).")
        if root:
            want = os.path.realpath(root)
            roots = [r for r in roots if os.path.realpath(r) == want]
            if not roots:
                raise CommandError(f"{root} is not a configured library root.")
        failed = []
        for r in ensure_roots(roots):
            try:
                s = scan_root(r, allow_empty=allow_empty)
            except ScanError as e:
                self.stderr.write(f"{r.path}: scan aborted, index unchanged: {e}")
                failed.append(r.path)
                continue
            self.stdout.write(f"{r.path}: {s.added} added, {s.updated} updated, {s.removed} removed, "
                              f"{s.unchanged} unchanged, {s.albums} albums")
            for d in s.unlisted_dirs:
                self.stderr.write(f"  could not list {d}; its files were kept in the index as they were")
            for p in s.skipped_paths:
                self.stderr.write(f"  not indexed (file name is not valid Unicode): {p}")
        if failed:
            raise CommandError(f"scan failed for: {failed}")
