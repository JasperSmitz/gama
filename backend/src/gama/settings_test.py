"""Settings for the test suite.

Forces a configuration that cannot reach any real collection or real Gama data:
no library roots (tests create their own under ``tmp_path``), a data dir under
the system temp dir, and no config file. pytest-django loads this module before
any conftest runs, so environment overrides must happen here.
"""
import os
import tempfile

os.environ["GAMA_CONFIG"] = ""
os.environ["GAMA_LIBRARY_ROOTS"] = ""
os.environ["GAMA_DATA_DIR"] = os.path.join(tempfile.gettempdir(), f"gama-test-{os.getpid()}")
os.environ["GAMA_DEBUG"] = "0"
os.environ["GAMA_SECRET_KEY"] = "gama-test-only-secret-not-for-production-0123456789abcdef"
os.environ["GAMA_ALLOWED_HOSTS"] = ""

from gama.settings import *  # noqa: E402,F401,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # fast tests only
