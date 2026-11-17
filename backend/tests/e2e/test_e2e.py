"""M8 end-to-end: real `manage.py` processes (migrate, gama_scan, gama_worker --once) on a
file database, the web UI driven in its own process, the oracle on the whole library."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from verify.verify import compare_tree

pytestmark = pytest.mark.slow

BACKEND = Path(__file__).resolve().parents[2]
ALBUMS = ["In the Court of the Crimson King", "Selling England by the Pound", "Close to the Edge", "Two Disc Set"]
TWO_DISC = "Gama Test Orchestra/Two Disc Set/"


@pytest.fixture
def e2e(corpus, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GAMA_", "DJANGO_"))}
    env.update(GAMA_DEBUG="1", GAMA_CONFIG="", GAMA_DATA_DIR=str(tmp_path / "data"),
               GAMA_LIBRARY_ROOTS=str(corpus.root), GAMA_VERIFY_AUDIO="auto", DJANGO_SETTINGS_MODULE="gama.settings")

    def run(*args, script=None):
        cmd = [sys.executable, str(script or BACKEND / "manage.py"), *args]
        r = subprocess.run(cmd, cwd=BACKEND, env=env, capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, (args, r.stdout[-2000:], r.stderr[-3000:])
        return r.stdout

    def drive(*args):
        return json.loads(run(*args, script=BACKEND / "tests" / "e2e" / "drive.py").strip().splitlines()[-1])

    (tmp_path / "data").mkdir()  # as in the README: the data dir is created by the operator
    run("migrate", "-v0")
    assert "albums" in run("gama_scan")
    return run, drive


def test_end_to_end_apply_repeat_restore(e2e, corpus):
    run, drive = e2e

    # ---- select the three example albums and a two-disc album, preview, confirm
    p = drive("apply", "Symphonic Prog", *ALBUMS)
    assert p["listed"] and (p["n_change"], p["n_nochange"], p["n_skip"]) == (13, 0, 0)
    assert sum(f.startswith(TWO_DISC) for f in p["files"]) == 4  # both discs in one selection

    # ---- the real worker process applies it
    out = run("gama_worker", "--once")
    assert f"job {p['job']}: succeeded" in out
    j = drive("job", p["job"])
    assert j["status"] == "succeeded" and j["page_ok"] and j["history_lists"]
    for it in j["items"]:
        assert it["state"] == "committed" and it["verified"] and it["audio"] == "verified"
        assert it["read_back"] == it["requested"] == it["index"]
    assert {it["relpath"] for it in j["items"] if it["relpath"].startswith(TWO_DISC)} == {
        f for f in p["files"] if f.startswith(TWO_DISC)}

    # ---- the oracle on the whole library: zero problems, only the intended files changed
    assert compare_tree(corpus.master, corpus.root, p["expected"]) == {}
    assert not list(corpus.root.rglob(".gama-tmp-*"))

    # ---- repeated operation: the second preview has nothing to do
    again = drive("preview", "Symphonic Prog", *ALBUMS)
    assert again["n_change"] == 0 and again["n_nochange"] == 13

    # ---- restore through the UI, run by the real worker: byte-identical originals, index updated
    r = drive("restore", p["job"])
    assert r["n_change"] == 13
    out = run("gama_worker", "--once")
    assert f"job {r['job']}: succeeded" in out
    rj = drive("job", r["job"])
    assert rj["kind"] == "restore" and all(it["state"] == "restored" for it in rj["items"])
    assert all(it["read_back"] == it["index"] == it["requested"] for it in rj["items"])
    assert compare_tree(corpus.master, corpus.root, check_mtime=False) == {}


def test_gama_restore_cli_end_to_end(e2e, corpus):
    run, drive = e2e
    p = drive("apply", "Krautrock", ALBUMS[0])
    run("gama_worker", "--once")
    dry = run("gama_restore", p["job"], "--dry-run")
    assert dry.count(": restore") == 3
    out = run("gama_restore", p["job"])
    assert "succeeded" in out
    assert compare_tree(corpus.master, corpus.root, check_mtime=False) == {}
