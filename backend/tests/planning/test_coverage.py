"""M3 acceptance: 100 % branch coverage of metadata/genres.py from its pure tests."""
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]


@pytest.mark.slow
def test_genres_module_full_branch_coverage(tmp_path):
    pytest.importorskip("coverage")
    env = {"COVERAGE_FILE": str(tmp_path / ".coverage"), "PATH": "/usr/bin:/bin"}
    run = [sys.executable, "-m", "coverage", "run", "--branch", "--include=src/gama/metadata/genres.py",
           "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/planning/test_genre_ops.py",
           "tests/metadata/test_genre_field.py"]
    r = subprocess.run(run, cwd=BACKEND, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:]
    rep = subprocess.run([sys.executable, "-m", "coverage", "report", "-m", "--fail-under=100"], cwd=BACKEND,
                         env=env, capture_output=True, text=True)
    assert rep.returncode == 0, rep.stdout
