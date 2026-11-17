"""Regression tests for the M5 review (F1-F7): exception paths must stay inside the
designed state machine, decide by hash, and never block unrelated jobs."""
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from gama.safewrite import fsutil
from gama.safewrite.env import WriteEnv
from gama.safewrite.journal import Journal, JournalWriteError, load, summarize
from gama.safewrite.recovery import recover
from gama.safewrite.restore import RestoreItem, run_restore
from gama.safewrite.writer import WriteItem, run_apply
from tests.safewrite.test_engine import KC, items_for, rels, temps_left

pytestmark = pytest.mark.slow
HERE = Path(__file__).resolve()


@pytest.fixture(autouse=True)
def _no_failpoints():
    fsutil.clear_failpoints()
    yield
    fsutil.clear_failpoints()


@pytest.fixture
def env(tmp_path):
    return WriteEnv(tmp_path / "data", audio="off")


def raise_oserror(code=errno.EIO):
    def fp(ctx):
        raise OSError(code, os.strerror(code))
    return fp


def only(n, fp):
    return lambda ctx: fp(ctx) if ctx.get("n") == n else None


def originals_back(corpus, items):
    return all(fsutil.sha256_file(corpus.path(i.relpath)) == i.sha_before for i in items)


def journal_state(res):
    s = summarize(load(res.journal_path))
    return s["status"], {n: v["state"] for n, v in s["items"].items()}


# --------------------------------------------------------------------------- F1

def test_P1_unreadable_file_mid_batch_rolls_back(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    fsutil.set_failpoint("item_start", only(1, lambda ctx: os.chmod(ctx["path"], 0o200)))
    res = run_apply(items, env)
    os.chmod(corpus.path(items[1].relpath), 0o644)
    assert res.status == "rolled_back", (res.error, [(i.state, i.error) for i in res.items])
    assert [i.state for i in res.items] == ["committed", "failed", "skipped"]
    assert res.items[0].restore_state == "restored" and res.items[1].stage == "preflight"
    assert journal_state(res) == ("rolled_back", {0: "committed", 1: "failed", 2: "skipped"})
    assert summarize(load(res.journal_path))["items"][0]["restore_state"] == "restored"
    assert originals_back(corpus, items) and temps_left(corpus) == []


def test_P1b_eio_while_hashing_is_an_item_failure(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    real = fsutil.sha256_file
    target = os.fspath(corpus.path(items[1].relpath))

    def flaky(path):
        if os.fspath(path) == target:
            raise OSError(errno.EIO, "Input/output error")
        return real(path)
    fsutil.set_failpoint("item_start", only(1, lambda ctx: setattr(fsutil, "sha256_file", flaky)))
    try:
        res = run_apply(items, env)
    finally:
        fsutil.sha256_file = real
    assert res.status == "rolled_back" and res.items[1].state == "failed"
    assert originals_back(corpus, items)


def test_P2_directory_read_only_before_rename(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    d = corpus.path(items[0].relpath).parent
    fsutil.set_failpoint("before_rename_check", only(1, lambda ctx: os.chmod(d, 0o555)))
    try:
        res = run_apply(items, env)
    finally:
        os.chmod(d, 0o755)
    assert res.items[1].state == "failed" and res.items[1].stage == "rename"
    assert "could not be removed" in res.items[1].error
    # The rollback of item 0 needs the same directory, so it fails too: honest failed_partial.
    assert res.status == "failed_partial" and res.items[0].state == "committed"
    assert temps_left(corpus), "the temp could not be removed while the directory was read-only"
    status, _ = journal_state(res)
    assert status == "failed_partial"
    # Once the directory is writable again, recovery removes the leaked temp ...
    reports = recover(env)
    assert any(r["status"] == "cleaned" for r in reports) and temps_left(corpus) == []
    assert recover(env) == []
    # ... and the committed file can be restored.
    r0 = res.items[0]
    out = run_restore([RestoreItem(0, items[0].root, items[0].relpath, items[0].sha_before, r0.sha_after)], env)
    assert out.status == "succeeded" and originals_back(corpus, items)


def _fail_journal_at(state, n, partial=False):
    def fp(ctx):
        rec = ctx["record"]
        if rec.get("t") == "item" and rec.get("state") == state and rec.get("n") == n:
            if partial:
                os.write(ctx["fd"], ctx["line"][:20])  # short write, then the disk is full
            raise OSError(errno.ENOSPC, "No space left on device")
    return fp


@pytest.mark.parametrize("partial", [False, True])
def test_P3_journal_write_failure_in_phase_b(corpus, env, partial):
    items = items_for(corpus, rels(corpus, KC))
    fsutil.set_failpoint("journal_write", _fail_journal_at("renaming", 1, partial))
    res = run_apply(items, env, job_id="j")
    fsutil.clear_failpoints()
    assert res.status == "aborted"
    # States reflect the disk, not the loop: item 0 committed, 1 and 2 untouched.
    assert [i.state for i in res.items] == ["committed", "failed", "failed"]
    assert temps_left(corpus) == []
    recs = load(res.journal_path)  # no torn line: still readable
    assert summarize(recs)["status"] is None  # left open for recovery
    reports = recover(env)
    assert [r["status"] for r in reports] == ["interrupted"]
    assert summarize(load(res.journal_path))["status"] == "interrupted"
    # Nothing is blocked afterwards: a restore of the committed file works.
    out = run_restore([RestoreItem(0, items[0].root, items[0].relpath, items[0].sha_before,
                                   res.items[0].sha_after)], env)
    assert out.status == "succeeded" and originals_back(corpus, items)


def test_journal_cut_back_and_poisoned_after_failed_write(tmp_path):
    j = Journal.create(str(tmp_path), "x")
    j.write({"t": "header", "items": []})
    good = Path(j.path).read_bytes()

    def short(ctx):
        os.write(ctx["fd"], ctx["line"][:7])
        raise OSError(errno.ENOSPC, "full")
    fsutil.set_failpoint("journal_write", short)
    with pytest.raises(JournalWriteError):
        j.item(0, "renaming")
    fsutil.clear_failpoints()
    assert Path(j.path).read_bytes() == good
    with pytest.raises(JournalWriteError, match="unusable"):
        j.job("succeeded")
    j.close()


def test_unterminated_last_line_is_ignored_even_if_it_parses(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_bytes(b'{"t": "header", "items": []}\n{"t": "job", "status": "succeeded"}')
    assert load(str(p)) == [{"t": "header", "items": []}]


# --------------------------------------------------------------------------- F2

def test_P6_restore_survives_errors_after_the_rename(corpus, env):
    items = items_for(corpus, rels(corpus, KC)[:1])
    res = run_apply(items, env)
    fsutil.set_failpoint("restore_after_rename", raise_oserror())
    out = run_restore([RestoreItem(0, items[0].root, items[0].relpath, items[0].sha_before,
                                   res.items[0].sha_after)], env)
    assert out.status == "succeeded" and out.items[0].state == "restored" and "after the rename" in out.items[0].error
    assert originals_back(corpus, items)


def test_P6b_restore_job_with_broken_journal_returns_a_result(corpus, env):
    items = items_for(corpus, rels(corpus, KC)[:1])
    res = run_apply(items, env)
    fsutil.set_failpoint("journal_write", lambda ctx: (_ for _ in ()).throw(OSError(errno.EIO, "EIO"))
                         if ctx["record"].get("state") == "restoring" else None)
    out = run_restore([RestoreItem(0, items[0].root, items[0].relpath, items[0].sha_before,
                                   res.items[0].sha_after)], env)
    assert out.status == "aborted" and [o.state for o in out.items] == ["restore_failed"]
    fsutil.clear_failpoints()
    # Nothing was touched (the "restoring" record could not be written), and recovery closes the job.
    assert fsutil.sha256_file(corpus.path(items[0].relpath)) == res.items[0].sha_after
    assert any(r["status"] == "interrupted" for r in recover(env))


def test_P7_rollback_continues_after_an_error_in_one_restore(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    fsutil.set_failpoint("item_start", only(2, lambda ctx: os.chmod(ctx["path"], 0o444)))
    fsutil.set_failpoint("restore_after_rename", only(0, raise_oserror()))
    res = run_apply(items, env)
    os.chmod(corpus.path(items[2].relpath), 0o644)
    assert res.status == "rolled_back"
    assert [i.state for i in res.items] == ["committed", "committed", "failed"]
    assert [i.restore_state for i in res.items] == ["restored", "restored", None]
    assert journal_state(res) == ("rolled_back", {0: "committed", 1: "committed", 2: "failed"})
    assert originals_back(corpus, items)


# --------------------------------------------------------------------------- F4

def _crash(env, failpoint, items, job_id):
    req = Path(env.data_dir) / f"{job_id}.req.json"
    req.write_text(json.dumps({"data_dir": env.data_dir, "job_id": job_id, "items": [i.__dict__ for i in items]}))
    e = dict(os.environ, GAMA_ENABLE_FAILPOINTS="1", GAMA_FAILPOINTS=failpoint)
    r = subprocess.run([sys.executable, str(HERE.parent / "apply_child.py"), str(req)], env=e,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == fsutil.EXIT_CODE, r.stderr[-2000:]


@pytest.fixture
def lib2(corpus, tmp_path):
    """A second library root with its own album, for jobs that must not be blocked."""
    d = tmp_path / "lib2"
    shutil.copytree(corpus.root / KC, d / "Album")
    for p in d.rglob("*"):
        if p.is_file():
            p.chmod(0o644)
    return d


def _items_in(root, n0=0):
    from gama.metadata.genres import apply
    from gama.metadata.reader import read_file
    out = []
    for n, p in enumerate(sorted(Path(root).rglob("*.flac")), n0):
        r = read_file(p)
        res = apply(r.genre, "add", ["Jazz"])
        out.append(WriteItem(n, str(root), str(p.relative_to(root)), r.format, fsutil.sha256_file(p),
                             list(r.genre.values), list(res.values)))
    return out


def test_P4_library_not_mounted_defers_recovery(corpus, env, lib2):
    items = items_for(corpus, rels(corpus, KC))
    _crash(env, "after_commit:1", items, "crashed")
    offline = corpus.root.with_name(corpus.root.name + "-unmounted")
    os.rename(corpus.root, offline)
    try:
        reports = recover(env)
        assert [r["status"] for r in reports] == ["pending"]
        assert summarize(load(reports[0]["journal"]))["status"] is None  # nothing decided
        # Unrelated jobs still run.
        other = run_apply(_items_in(lib2), env)
        assert other.status == "succeeded"
    finally:
        os.rename(offline, corpus.root)
    # Retrying the same files is refused until recovery has succeeded, which happens at the start of this job.
    reports = recover(env)
    st = summarize(load(reports[0]["journal"]))
    assert reports[0]["status"] == "interrupted"
    assert [st["items"][n]["state"] for n in range(3)] == ["committed", "committed", "failed"]
    assert temps_left(corpus) == []


def test_P5_unreadable_file_defers_and_blocks_only_its_files(corpus, env, lib2):
    items = items_for(corpus, rels(corpus, KC))
    _crash(env, "after_commit:1", items, "crashed")
    p2 = corpus.path(items[2].relpath)
    os.chmod(p2, 0o200)
    try:
        blocked = run_apply([WriteItem(0, items[2].root, items[2].relpath, "flac", items[2].sha_before,
                                       items[2].genres_before, items[2].genres_after)], env)
        assert blocked.status == "failed" and "recovery" in blocked.error
        assert run_apply(_items_in(lib2), env).status == "succeeded"
        lib2_items = _items_in(lib2)
        restore = run_restore([RestoreItem(0, i.root, i.relpath, i.sha_before, None) for i in lib2_items], env)
        assert restore.status in ("succeeded", "completed_with_errors")
    finally:
        os.chmod(p2, 0o644)
    reports = recover(env)
    assert any(r["status"] == "interrupted" for r in reports)


# --------------------------------------------------------------------------- damaged journals

def test_damaged_journal_blocks_only_its_files(corpus, env, lib2):
    items = items_for(corpus, rels(corpus, KC))
    header = {"t": "header", "job": "bad", "kind": "apply",
              "items": [{"n": 0, "root": items[0].root, "relpath": items[0].relpath, "sha_before": items[0].sha_before}]}
    Path(env.journal_dir, "bad.jsonl").write_bytes(
        (json.dumps(header) + "\n{broken record\n" + json.dumps({"t": "job", "status": "x"}) + "\n").encode())
    reports = recover(env)
    assert [r["status"] for r in reports] == ["damaged"]
    refused = run_apply(items[:1], env)
    assert refused.status == "failed" and "recovery" in refused.error
    assert run_apply(items[1:], env).status == "succeeded"


def test_journal_with_unreadable_header_refuses_applies_but_not_restores(corpus, env, lib2):
    lib2_items = _items_in(lib2)
    done = run_apply(lib2_items, env)
    Path(env.journal_dir, "garbage.jsonl").write_bytes(b"\x00\x01 not json\n{also bad\n")
    refused = run_apply(items_for(corpus, rels(corpus, KC)), env)
    assert refused.status == "failed" and "unreadable journal" in refused.error
    out = run_restore([RestoreItem(i.n, i.root, i.relpath, i.sha_before, r.sha_after)
                       for i, r in zip(lib2_items, done.items)], env)
    assert out.status == "succeeded"


# --------------------------------------------------------------------------- F5, F6

def test_F6_crash_during_backup_copy_leaves_part_that_recovery_sweeps(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    _crash(env, "backup_copy", items, "crashed")
    parts = list(Path(env.store.tmp).glob("*.part"))
    assert parts
    reports = recover(env)
    assert any(r["status"] == "swept" for r in reports)
    assert list(Path(env.store.tmp).glob("*.part")) == []
    assert originals_back(corpus, items)


def test_F5_new_directories_are_fsynced_up_the_chain(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(fsutil, "fsync_dir", lambda p: synced.append(os.fspath(p)))
    target = tmp_path / "a" / "b" / "c"
    fsutil.makedirs_durable(target)
    assert target.is_dir()
    assert synced == [str(tmp_path), str(tmp_path / "a"), str(tmp_path / "a" / "b")]
    synced.clear()
    fsutil.makedirs_durable(target)  # already there: nothing to sync
    assert synced == []


def test_F5_temp_fsynced_after_attribute_copy(corpus, env, monkeypatch):
    calls = []
    real_copy, real_sync = fsutil.copy_attrs, fsutil.fsync_file
    monkeypatch.setattr(fsutil, "copy_attrs", lambda a, b: (calls.append(("attrs", os.fspath(b))), real_copy(a, b))[1])
    monkeypatch.setattr(fsutil, "fsync_file", lambda p: (calls.append(("sync", os.fspath(p))), real_sync(p))[1])
    items = items_for(corpus, rels(corpus, KC)[:1])
    assert run_apply(items, env).status == "succeeded"
    i = calls.index(next(c for c in calls if c[0] == "attrs"))
    assert calls[i + 1] == ("sync", calls[i][1])


def test_F7_results_match_the_journal_after_an_abort(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    fsutil.set_failpoint("journal_write", _fail_journal_at("temp_created", 2))
    res = run_apply(items, env)
    fsutil.clear_failpoints()
    assert res.status == "aborted"
    assert [i.state for i in res.items] == ["committed", "committed", "failed"]
    assert all(stat.S_ISREG(corpus.path(i.relpath).stat().st_mode) for i in items)
