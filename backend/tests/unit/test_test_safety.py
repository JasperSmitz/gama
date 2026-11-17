"""Self-tests for the test-suite safety guard and the settings hardening (review of M1).

Every path used here is under tmp_path; the real collection is never touched.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from gama.config import Config
from tests.conftest import unsafe_test_settings

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src"
STRONG_KEY = "x" * 40 + "abcdefghij0123456789"


def fake_settings(module="gama.settings_test", roots=(), data_dir=None):
    data = data_dir or Path(tempfile.gettempdir()) / "gama-test-x"
    return SimpleNamespace(SETTINGS_MODULE=module, GAMA=Config(tuple(roots), Path(data)))


def test_guard_accepts_test_settings(settings):
    assert unsafe_test_settings(settings) == []
    assert unsafe_test_settings(fake_settings()) == []


@pytest.mark.parametrize("kw,needle", [
    ({"module": "gama.settings"}, "settings module"),
    ({"roots": (Path("/srv/somewhere"),)}, "library roots"),
    ({"data_dir": Path("/var/lib/gama")}, "not under the temp dir"),
    ({"data_dir": Path(tempfile.gettempdir())}, "not under the temp dir"),
])
def test_guard_rejects_unsafe_settings(kw, needle):
    reasons = unsafe_test_settings(fake_settings(**kw))
    assert any(needle in r for r in reasons), reasons


def _pytest_env(tmp_path):
    """Environment of a developer with real-looking Gama settings exported."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GAMA_", "DJANGO_", "PYTEST_"))}
    (tmp_path / "fake_lib").mkdir()
    env.update(DJANGO_SETTINGS_MODULE="gama.settings", GAMA_DEBUG="1", GAMA_CONFIG="",
               GAMA_LIBRARY_ROOTS=str(tmp_path / "fake_lib"), GAMA_DATA_DIR=str(tmp_path / "real_data"))
    return env


SAFE_TEST = "tests/unit/test_config.py::test_test_settings_are_safe"


def test_exported_settings_module_is_overridden_by_ds(tmp_path):
    """Finding 7: addopts' --ds outranks an exported DJANGO_SETTINGS_MODULE."""
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", SAFE_TEST],
                       cwd=BACKEND, env=_pytest_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]


def test_session_aborts_with_production_settings(tmp_path):
    """Finding 7: without --ds, the env var wins, and the session guard must abort."""
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
                        SAFE_TEST], cwd=BACKEND, env=_pytest_env(tmp_path), capture_output=True, text=True,
                       timeout=120)
    out = r.stdout + r.stderr
    assert r.returncode == 3, out[-2000:]
    assert "refusing to run" in out and "gama.settings" in out
    assert not (tmp_path / "real_data").exists()


def _load_settings(tmp_path, **env_over):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GAMA_", "DJANGO_"))}
    env.update(GAMA_CONFIG="", GAMA_DATA_DIR=str(tmp_path / "d"), DJANGO_SETTINGS_MODULE="gama.settings",
               PYTHONPATH=str(SRC))
    env.update(env_over)
    return subprocess.run([sys.executable, "-c", "import django; django.setup()"], env=env,
                          capture_output=True, text=True, timeout=60)


def test_debug_refuses_extra_allowed_hosts(tmp_path):
    r = _load_settings(tmp_path, GAMA_DEBUG="1", GAMA_ALLOWED_HOSTS="gama.example.invalid")
    assert r.returncode != 0 and "GAMA_ALLOWED_HOSTS" in r.stderr
    r = _load_settings(tmp_path, GAMA_SECRET_KEY=STRONG_KEY, GAMA_ALLOWED_HOSTS="gama.example.invalid")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("key", [" ", "short", "a" * 80, " " * 49 + "abcdefghij"])
def test_weak_secret_key_refused(tmp_path, key):
    r = _load_settings(tmp_path, GAMA_SECRET_KEY=key)
    assert r.returncode != 0 and "too weak" in r.stderr


def test_strong_secret_key_accepted(tmp_path):
    r = _load_settings(tmp_path, GAMA_SECRET_KEY=STRONG_KEY)
    assert r.returncode == 0, r.stderr
