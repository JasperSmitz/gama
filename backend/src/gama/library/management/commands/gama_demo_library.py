"""Build a synthetic demo library for manual testing (fake audio, never real files)."""
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Build a synthetic demo library in DIR (must not exist or be empty). Needs ffmpeg and metaflac."

    def add_arguments(self, parser):
        parser.add_argument("dir")

    def handle(self, *args, dir, **opts):
        out = Path(dir).expanduser().resolve()
        tools = Path(settings.BASE_DIR).resolve().parents[2] / "tools"
        sys.path.insert(0, str(tools))
        try:
            from fixtures.build import build, tools_available, unsafe_output_dir
        except ImportError as e:
            raise CommandError(f"fixture builder not found in {tools}: {e}") from e
        # Safety checks come before anything touches the target (not even a listing).
        bad = unsafe_output_dir(out)
        if bad is not None:
            raise CommandError(f"{out} is inside the production or library root {bad}; refusing.")
        for r in settings.GAMA.library_roots:
            if out.is_relative_to(Path(r).resolve()):
                raise CommandError(f"{out} is inside the configured library root {r}; choose another place.")
        if out.exists() and not out.is_dir():
            raise CommandError(f"{out} exists and is not a directory; refusing.")
        if out.exists() and any(out.iterdir()):
            raise CommandError(f"{out} is not empty; refusing to write into it.")
        missing = tools_available()
        if missing:
            raise CommandError(f"missing tools: {missing}")
        manifest = build(out)
        lib = out / "library"
        self.stdout.write(f"{len(manifest)} files in {lib}")
        self.stdout.write(f"Use it with: GAMA_LIBRARY_ROOTS={lib} manage.py gama_scan")
