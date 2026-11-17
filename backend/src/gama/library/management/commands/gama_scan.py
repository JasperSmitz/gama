"""Index the configured library roots (read-only; never writes to the library)."""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from gama.library.scanner import ensure_roots, scan_root


class Command(BaseCommand):
    help = "Scan the configured library roots (or one of them) and update the index."

    def add_arguments(self, parser):
        parser.add_argument("--root", help="Scan only this configured root")

    def handle(self, *args, root=None, **opts):
        roots = [str(r) for r in settings.GAMA.library_roots]
        if not roots:
            raise CommandError("No library roots configured (library_roots in gama.toml or GAMA_LIBRARY_ROOTS).")
        if root:
            want = os.path.realpath(root)
            roots = [r for r in roots if os.path.realpath(r) == want]
            if not roots:
                raise CommandError(f"{root} is not a configured library root.")
        for r in ensure_roots(roots):
            if not os.path.isdir(r.path):
                self.stderr.write(f"skipping {r.path}: not a directory")
                continue
            s = scan_root(r)
            self.stdout.write(f"{r.path}: {s.added} added, {s.updated} updated, {s.removed} removed, "
                              f"{s.unchanged} unchanged, {s.albums} albums")
            for p in s.skipped_paths:
                self.stderr.write(f"  not indexed (file name is not valid Unicode): {p}")
