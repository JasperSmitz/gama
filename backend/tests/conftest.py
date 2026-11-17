"""Shared test setup.

Safety: the session aborts unless Django runs with ``gama.settings_test``
(an exported ``DJANGO_SETTINGS_MODULE`` would otherwise win), no library root
is configured, and the data dir is under the system temp dir; and also if any
configured path resolves to (or under) the production collection. Every test
library is built synthetically under pytest's tmp dirs.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

PRODUCTION_ROOTS = (Path("/storage/music"),)


def production_paths(paths) -> list[Path]:
    """Those of ``paths`` that resolve to or under a production root."""
    bad = []
    for p in paths:
        rp = Path(p).expanduser().resolve(strict=False)
        for prod in PRODUCTION_ROOTS:
            prod_r = prod.resolve(strict=False)
            if rp == prod_r or rp.is_relative_to(prod_r) or rp == prod or rp.is_relative_to(prod):
                bad.append(Path(p))
    return bad


TEST_SETTINGS_MODULE = "gama.settings_test"


def unsafe_test_settings(settings) -> list[str]:
    """Reasons the Django settings are not safe to run the test suite with (empty = safe)."""
    reasons = []
    module = getattr(settings, "SETTINGS_MODULE", None)
    if module != TEST_SETTINGS_MODULE:
        reasons.append(f"settings module is {module!r}, not {TEST_SETTINGS_MODULE!r} "
                       "(unset DJANGO_SETTINGS_MODULE or pass --ds)")
    cfg = settings.GAMA
    if tuple(cfg.library_roots) != ():
        reasons.append(f"library roots are configured: {list(cfg.library_roots)}")
    tmp = Path(tempfile.gettempdir()).resolve()
    data = Path(cfg.data_dir).resolve(strict=False)
    if data == tmp or not data.is_relative_to(tmp):
        reasons.append(f"data_dir {cfg.data_dir} is not under the temp dir {tmp}")
    bad = production_paths([*cfg.library_roots, cfg.data_dir])
    if bad:
        reasons.append(f"configuration points at the production collection: {bad}")
    return reasons


def pytest_sessionstart(session):
    from django.conf import settings

    reasons = unsafe_test_settings(settings)
    if reasons:
        pytest.exit("refusing to run: " + "; ".join(reasons), returncode=3)


def guard(path: Path) -> Path:
    if production_paths([path]):
        pytest.fail(f"test path {path} is inside the production collection")
    return path


@dataclass
class Corpus:
    root: Path          # library root (a fresh per-test copy for `corpus`)
    master: Path        # pristine session copy; never modify
    manifest: dict      # relpath -> {"format", "group", "genres", "props"}
    build_seconds: float

    def path(self, rel: str) -> Path:
        return self.root / rel

    def with_prop(self, prop: str) -> list[str]:
        return [r for r, e in self.manifest.items() if prop in e["props"]]


@pytest.fixture(scope="session")
def corpus_master(tmp_path_factory) -> Corpus:
    from fixtures.build import build, tools_available

    missing = tools_available()
    if missing:
        pytest.skip(f"fixture builder needs {missing}")
    out = guard(tmp_path_factory.mktemp("corpus"))
    t0 = time.monotonic()
    manifest = build(out)
    elapsed = time.monotonic() - t0
    lib = out / "library"
    # Make the master read-only so a test that writes to it by mistake fails loudly.
    for p in lib.rglob("*"):
        if p.is_file():
            p.chmod(0o444)
    assert json.loads((out / "manifest.json").read_text()) == manifest
    return Corpus(lib, lib, manifest, elapsed)


@pytest.fixture
def corpus(corpus_master, tmp_path) -> Corpus:
    """A private, writable copy of the corpus for one test."""
    dest = guard(tmp_path / "library")
    shutil.copytree(corpus_master.master, dest)
    for p in dest.rglob("*"):
        if p.is_file():
            p.chmod(0o644)
    return Corpus(dest, _writable_master(corpus_master, tmp_path), corpus_master.manifest,
                  corpus_master.build_seconds)


def _writable_master(cm: Corpus, tmp_path: Path) -> Path:
    """A master copy with the same mode bits as the work copy, for oracle comparisons."""
    dest = tmp_path / "master"
    shutil.copytree(cm.master, dest)
    for p in dest.rglob("*"):
        if p.is_file():
            p.chmod(0o644)
    return dest
