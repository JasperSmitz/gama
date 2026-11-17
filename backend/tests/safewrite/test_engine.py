"""M5: the safe write engine (backups, journal, preflight, apply, restore, recovery).

Every test runs on a synthetic library in tmp_path. Regression tests for the
research review's safety bugs are marked S1..S9 in their names.
"""
import errno
import functools
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from gama.metadata.genres import apply
from gama.metadata.reader import read_file
from gama.planning.planner import FileRef, plan_files
from gama.safewrite import fsutil
from gama.safewrite.backups import BackupError, BackupStore
from gama.safewrite.env import WriteEnv
from gama.safewrite.journal import Journal, JournalError, load, summarize
from gama.safewrite.mutate import run_mutate
from gama.safewrite.recovery import recover
from gama.safewrite.restore import RestoreItem, run_restore
from gama.safewrite.writer import WriteItem, run_apply
from verify.verify import compare, compare_tree

pytestmark = pytest.mark.slow

KC = "King Crimson/1969 - In the Court of the Crimson King"
GEN = "Genesis/1973 - Selling England by the Pound"
YES = "Yes/1972 - Close to the Edge"
HERE = Path(__file__).resolve()


@pytest.fixture(autouse=True)
def _no_failpoints():
    fsutil.clear_failpoints()
    yield
    fsutil.clear_failpoints()


@pytest.fixture
def env(tmp_path):
    return WriteEnv(tmp_path / "data", audio="off")


def rels(corpus, *dirs):
    return [r for d in dirs for r in sorted(x for x in corpus.manifest if x.startswith(d + "/"))]


def items_for(corpus, relpaths, op="add", genres=("Jazz",)):
    out = []
    for n, rel in enumerate(relpaths):
        p = corpus.path(rel)
        r = read_file(p)
        res = apply(r.genre, op, list(genres))
        assert res.changed, rel
        out.append(WriteItem(n, str(corpus.root), rel, r.format, fsutil.sha256_file(p), list(r.genre.values),
                             list(res.values)))
    return out


def snapshot_bytes(corpus):
    return {p: p.read_bytes() for p in corpus.root.rglob("*") if p.is_file()}


def temps_left(corpus):
    return [p for p in corpus.root.rglob("*") if p.name.startswith(fsutil.TEMP_PREFIX)]


def assert_untouched(corpus, before):
    assert snapshot_bytes(corpus) == before
    assert temps_left(corpus) == []


def store_files(env):
    return sorted(p for p in Path(env.store.root, "sha256").rglob("*") if p.is_file())


def journal_of(res):
    return summarize(load(res.journal_path))


# --------------------------------------------------------------------------- happy path

def test_happy_path_mixed_formats_checked_by_oracle(corpus, tmp_path):
    env = WriteEnv(tmp_path / "data", audio="auto")
    items = items_for(corpus, rels(corpus, KC, GEN, YES), "add", ["Symphonic Prog"])
    res = run_apply(items, env)
    assert res.status == "succeeded", [(i.state, i.stage, i.error) for i in res.items]
    assert all(i.state == "committed" and i.verification["ok"] for i in res.items)
    assert all(i.verification["audio"] == "verified" for i in res.items)
    expected = {it.relpath: it.genres_after for it in items}
    assert compare_tree(corpus.master, corpus.root, expected) == {}  # also: no leftover temp files
    # Backups: content-addressed, read-only, hash-verified.
    for it in items:
        b = Path(env.store.path_for(it.sha_before))
        assert stat.S_IMODE(b.stat().st_mode) == 0o400 and fsutil.sha256_file(b) == it.sha_before
        assert b.read_bytes() == (corpus.master / it.relpath).read_bytes()
    j = journal_of(res)
    assert j["status"] == "succeeded" and all(v["state"] == "committed" for v in j["items"].values())
    assert all(j["items"][it.n]["sha_after"] == fsutil.sha256_file(corpus.path(it.relpath)) for it in items)


def test_attributes_preserved(corpus, env):
    rel = rels(corpus, KC)[0]
    p = corpus.path(rel)
    os.chmod(p, 0o640)
    os.chmod(corpus.master / rel, 0o640)
    try:
        os.setxattr(p, "user.gama_test", b"keep me")
        os.setxattr(corpus.master / rel, "user.gama_test", b"keep me")
        xattr = True
    except OSError as e:
        if e.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
        xattr = False
    res = run_apply(items_for(corpus, [rel]), env)
    assert res.status == "succeeded"
    st = p.stat()
    assert stat.S_IMODE(st.st_mode) == 0o640 and st.st_gid == (corpus.master / rel).stat().st_gid
    if xattr:
        assert os.getxattr(p, "user.gama_test") == b"keep me"
    # The oracle also compares mode, group and every xattr.
    assert compare(corpus.master / rel, p, "genres", list(read_file(p).genre.values), check_audio=False) == []


# --------------------------------------------------------------------------- missing permissions

def test_read_only_file_aborts_with_zero_writes(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    os.chmod(corpus.path(items[1].relpath), 0o444)
    before = snapshot_bytes(corpus)
    res = run_apply(items, env)
    assert res.status == "failed" and "not-writable" in res.items[1].error
    assert_untouched(corpus, before)
    assert store_files(env) == []  # preflight fails before Phase A


def test_read_only_directory_aborts(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    d = corpus.path(items[0].relpath).parent
    before = snapshot_bytes(corpus)
    os.chmod(d, 0o555)
    try:
        res = run_apply(items, env)
    finally:
        os.chmod(d, 0o755)
    assert res.status == "failed" and all("dir-not-writable" in i.error for i in res.items)
    assert_untouched(corpus, before)


def test_file_owned_by_another_user_aborts(corpus, tmp_path):
    env = WriteEnv(tmp_path / "data", audio="off", euid=lambda: os.geteuid() + 1)
    before = snapshot_bytes(corpus)
    res = run_apply(items_for(corpus, rels(corpus, KC)), env)
    assert res.status == "failed" and all("not-owner" in i.error for i in res.items)
    assert_untouched(corpus, before)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_symlink_and_hardlink_refused(corpus, env, kind):
    rel = rels(corpus, KC)[0]
    items = items_for(corpus, [rel])
    if kind == "symlink":
        real = corpus.root / "real.flac"
        os.replace(corpus.path(rel), real)
        os.symlink(real, corpus.path(rel))
    else:
        os.link(corpus.path(rel), corpus.root / "hard.flac")
    before = snapshot_bytes(corpus)
    res = run_apply(items, env)
    assert res.status == "failed" and kind in res.items[0].error
    assert_untouched(corpus, before)


# --------------------------------------------------------------------------- insufficient storage

def test_insufficient_backup_space_aborts_without_partial_backup(corpus, tmp_path):
    def fake(path):
        return 10 if os.fspath(path).startswith(os.fspath(tmp_path / "data")) else 1 << 40
    env = WriteEnv(tmp_path / "data", audio="off", free_space=fake)
    before = snapshot_bytes(corpus)
    res = run_apply(items_for(corpus, rels(corpus, KC)), env)
    assert res.status == "failed" and "no-space-backups" in res.items[0].error
    assert_untouched(corpus, before)
    assert store_files(env) == [] and os.listdir(env.store.tmp) == []


def test_library_space_shortage_before_file_n_fails_that_item_cleanly(corpus, tmp_path):
    state = {"committed": 0}
    lib = os.fspath(corpus.root)

    def fake(path):
        # The library runs full once, right when item 1 is checked; the rollback then has room again.
        if os.fspath(path).startswith(lib) and state["committed"] == 1:
            state["committed"] = -1
            return 0
        return 1 << 40
    fsutil.set_failpoint("after_commit", lambda ctx: state.__setitem__("committed", state["committed"] + 1))
    env = WriteEnv(tmp_path / "data", audio="off", free_space=fake)
    items = items_for(corpus, rels(corpus, KC))
    res = run_apply(items, env)
    assert res.items[1].state == "failed" and "no-space-library" in res.items[1].error
    assert res.status == "rolled_back" and res.items[2].state == "skipped"
    assert (res.items[0].state, res.items[0].restore_state) == ("committed", "restored")
    assert {p.relative_to(corpus.root) for p in temps_left(corpus)} == set()
    for it in items:
        assert fsutil.sha256_file(corpus.path(it.relpath)) == it.sha_before


# --------------------------------------------------------------------------- file changing between preview and apply

def test_change_before_apply_is_conflict_with_zero_writes(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    p = corpus.path(items[2].relpath)
    subprocess.run(["metaflac", "--set-tag=COMMENT=edited elsewhere", str(p)], check=True)
    before = snapshot_bytes(corpus)
    res = run_apply(items, env)
    assert res.status == "conflict" and res.items[2].state == "conflict"
    assert [i.state for i in res.items[:2]] == ["skipped", "skipped"]
    assert_untouched(corpus, before)


def test_S7_change_between_temp_write_and_rename(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    target = corpus.path(items[1].relpath)

    def edit(ctx):
        if ctx["n"] == 1:
            with open(target, "ab") as f:
                f.write(b"\0newer")
    fsutil.set_failpoint("before_rename_check", edit)
    res = run_apply(items, env)
    assert res.items[1].state == "conflict" and res.items[1].stage == "recheck"
    assert target.read_bytes().endswith(b"\0newer")  # the other program's bytes survive
    assert res.status == "rolled_back" and res.items[0].restore_state == "restored"
    assert fsutil.sha256_file(corpus.path(items[0].relpath)) == items[0].sha_before
    assert temps_left(corpus) == []


def test_S7_temp_is_filled_from_the_backup_not_the_live_file(corpus, env):
    items = items_for(corpus, rels(corpus, KC)[:1])
    target = corpus.path(items[0].relpath)
    seen = {}

    def peek(ctx):
        seen["temp"] = fsutil.sha256_file(ctx["temp"])
    fsutil.set_failpoint("after_temp_written", peek)
    res = run_apply(items, env)
    assert res.status == "succeeded" and seen["temp"] == items[0].sha_before
    assert target.exists()


def test_change_after_backup_before_edit_is_conflict(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    target = corpus.path(items[0].relpath)
    fsutil.set_failpoint("after_backup", lambda ctx: target.write_bytes(target.read_bytes() + b"x"))
    res = run_apply(items, env)
    assert res.status == "rolled_back" and res.items[0].state == "conflict"
    assert target.read_bytes().endswith(b"x") and all(i.state == "skipped" for i in res.items[1:])


# --------------------------------------------------------------------------- partial failure and batch policy

def _make_readonly(target):
    def fp(ctx):
        if ctx["n"] == 1:
            os.chmod(target, 0o444)
    return fp


def test_partial_failure_stop_and_rollback(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    fsutil.set_failpoint("item_start", _make_readonly(corpus.path(items[1].relpath)))
    res = run_apply(items, env)
    assert [i.state for i in res.items] == ["committed", "failed", "skipped"]
    assert [i.restore_state for i in res.items] == ["restored", None, None]
    assert res.status == "rolled_back" and "not-writable" in res.items[1].error
    for it in items:
        assert fsutil.sha256_file(corpus.path(it.relpath)) == it.sha_before
    assert temps_left(corpus) == []


def test_partial_failure_continue_on_error(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    fsutil.set_failpoint("item_start", _make_readonly(corpus.path(items[1].relpath)))
    res = run_apply(items, env, policy="continue")
    assert [i.state for i in res.items] == ["committed", "failed", "committed"]
    assert res.status == "completed_with_errors"
    assert read_file(corpus.path(items[0].relpath)).genre.values[-1] == "Jazz"
    assert fsutil.sha256_file(corpus.path(items[1].relpath)) == items[1].sha_before


def test_S3_error_after_rename_is_decided_by_hash(corpus, env):
    """An error after the rename (EIO on the directory fsync) does not make the item
    "failed": the file holds the verified edit, so it is committed, with a note."""
    items = items_for(corpus, rels(corpus, KC))
    fsutil.set_failpoint("after_rename", lambda ctx: (_ for _ in ()).throw(OSError(5, "EIO on dir fsync"))
                         if ctx["n"] == 1 else None)
    res = run_apply(items, env)
    assert res.status == "succeeded" and [i.state for i in res.items] == ["committed"] * 3
    assert "after the rename" in res.items[1].error
    s = journal_of(res)
    assert s["items"][1]["state"] == "committed" and "after the rename" in s["items"][1]["error"]


def test_S6_hanging_writer_is_killed(corpus, tmp_path):
    hook = functools.partial(run_mutate, hook="tests.safewrite.mutate_hooks:sleepy",
                             extra_sys_path=[str(HERE.parents[2])])
    env = WriteEnv(tmp_path / "data", audio="off", mutate=hook, mutate_timeout=1.5)
    items = items_for(corpus, rels(corpus, KC)[:1])
    res = run_apply(items, env)
    assert res.status == "rolled_back" and res.items[0].stage == "mutate" and "timeout" in res.items[0].error
    assert fsutil.sha256_file(corpus.path(items[0].relpath)) == items[0].sha_before
    assert temps_left(corpus) == []


def test_verify_failure_never_reaches_the_rename(corpus, tmp_path):
    def bad_mutate(path, fmt, values, before, **kw):  # writes the wrong genres
        return run_mutate(path, fmt, ["Wrong"], before, **kw)
    env = WriteEnv(tmp_path / "data", audio="off", mutate=bad_mutate)
    items = items_for(corpus, rels(corpus, KC)[:1])
    before = snapshot_bytes(corpus)
    res = run_apply(items, env)
    assert res.items[0].stage == "verify" and res.status == "rolled_back"
    assert_untouched(corpus, before)


# --------------------------------------------------------------------------- backups (S1, S9)

def test_S1_backups_reused_only_when_matching_and_never_overwritten(corpus, env):
    rel = rels(corpus, KC)[0]
    original = corpus.path(rel).read_bytes()
    r1 = run_apply(items_for(corpus, [rel], "add", ["Jazz"]), env)
    r2 = run_apply(items_for(corpus, [rel], "add", ["Blues"]), env)
    assert (r1.status, r2.status) == ("succeeded", "succeeded")
    assert r1.items[0].backup_sha != r2.items[0].backup_sha and len(store_files(env)) == 2
    # Undo job 2, then job 1: the original comes back byte-for-byte.
    for res in (r2, r1):
        it = res.items[0]
        out = run_restore([RestoreItem(0, str(corpus.root), rel, it.backup_sha, it.sha_after)], env)
        assert out.status == "succeeded" and out.items[0].state == "restored"
    assert corpus.path(rel).read_bytes() == original


def test_S1_mismatching_existing_backup_is_never_overwritten(corpus, env):
    items = items_for(corpus, rels(corpus, KC)[:1])
    planted = Path(env.store.path_for(items[0].sha_before))
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_bytes(b"not the original")
    before = snapshot_bytes(corpus)
    res = run_apply(items, env)
    assert res.status == "failed" and res.items[0].stage == "backup"
    assert planted.read_bytes() == b"not the original"
    assert_untouched(corpus, before)


def test_S9_failed_backup_copy_leaves_no_partial_file(corpus, env):
    fsutil.set_failpoint("backup_copy", "raise")
    before = snapshot_bytes(corpus)
    res = run_apply(items_for(corpus, rels(corpus, KC)), env)
    assert res.status == "failed"
    assert os.listdir(env.store.tmp) == [] and store_files(env) == []
    assert_untouched(corpus, before)


def test_backup_store_rejects_changed_source(tmp_path):
    store = BackupStore(tmp_path / "store")
    src = tmp_path / "f"
    src.write_bytes(b"abc")
    with pytest.raises(BackupError):
        store.ensure(src, "0" * 64)
    assert os.listdir(store.tmp) == []


# --------------------------------------------------------------------------- journal (S4)

def test_S4_journal_never_reused(corpus, env):
    items = items_for(corpus, rels(corpus, KC)[:1])
    assert run_apply(items, env, job_id="job1").status == "succeeded"
    j1 = Path(env.journal_dir, "job1.jsonl").read_bytes()
    items = items_for(corpus, rels(corpus, KC)[:1], "add", ["Blues"])
    res = run_apply(items, env, job_id="job1")
    assert res.status == "failed" and "already exists" in res.error
    assert Path(env.journal_dir, "job1.jsonl").read_bytes() == j1
    with pytest.raises(JournalError):
        Journal.create(env.journal_dir, "job1")


def test_journal_torn_last_line_ignored_damaged_middle_refused(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_bytes(b'{"t": "header", "items": []}\n{"t": "job", "sta')
    assert load(str(p)) == [{"t": "header", "items": []}]
    p.write_bytes(b'{"t": "header"}\n{broken\n{"t": "job", "status": "failed"}\n')
    with pytest.raises(JournalError):
        load(str(p))


def test_lock_prevents_concurrent_jobs(corpus, env):
    with env.write_lock():
        res = run_apply(items_for(corpus, rels(corpus, KC)[:1]), env)
    assert res.status == "failed" and "another" in res.error


# --------------------------------------------------------------------------- restore (S2, S5)

def _applied(corpus, env, n=3):
    items = items_for(corpus, rels(corpus, KC)[:n])
    res = run_apply(items, env)
    assert res.status == "succeeded"
    return [RestoreItem(it.n, str(corpus.root), it.relpath, it.sha_before, r.sha_after)
            for it, r in zip(items, res.items)]


def test_restore_after_success_is_byte_identical(corpus, env):
    ritems = _applied(corpus, env)
    res = run_restore(ritems, env)
    assert res.status == "succeeded" and all(o.state == "restored" for o in res.items)
    for it in ritems:
        assert corpus.path(it.relpath).read_bytes() == (corpus.master / it.relpath).read_bytes()
        assert stat.S_IMODE(corpus.path(it.relpath).stat().st_mode) == 0o644
    assert temps_left(corpus) == []
    again = run_restore(ritems, env)
    assert [o.state for o in again.items] == ["nothing_to_do"] * 3


def test_S5_restore_refuses_to_overwrite_later_edits(corpus, env):
    ritems = _applied(corpus, env, 1)
    p = corpus.path(ritems[0].relpath)
    subprocess.run(["metaflac", "--set-tag=COMMENT=later edit", str(p)], check=True)
    later = p.read_bytes()
    res = run_restore(ritems, env)
    assert res.items[0].state == "restore_conflict" and res.status == "completed_with_errors"
    assert p.read_bytes() == later


def test_S2_corrupt_backup_refused_before_touching_the_file(corpus, env):
    ritems = _applied(corpus, env, 1)
    b = Path(env.store.path_for(ritems[0].sha_before))
    os.chmod(b, 0o600)
    b.write_bytes(b.read_bytes()[:-10] + b"corrupted!")
    edited = corpus.path(ritems[0].relpath).read_bytes()
    res = run_restore(ritems, env)
    assert res.items[0].state == "restore_failed" and "backup" in res.items[0].error
    assert corpus.path(ritems[0].relpath).read_bytes() == edited


def test_force_restore_backs_up_current_bytes_first(corpus, env):
    ritems = _applied(corpus, env, 1)
    p = corpus.path(ritems[0].relpath)
    subprocess.run(["metaflac", "--set-tag=COMMENT=later edit", str(p)], check=True)
    later_sha = fsutil.sha256_file(p)
    forced = RestoreItem(**{**ritems[0].__dict__, "force": True})
    res = run_restore([forced], env)
    assert res.items[0].state == "restored" and res.items[0].forced_backup_sha == later_sha
    assert env.store.verify(later_sha)
    assert p.read_bytes() == (corpus.master / ritems[0].relpath).read_bytes()


def test_restore_missing_file_is_never_recreated(corpus, env):
    ritems = _applied(corpus, env, 1)
    corpus.path(ritems[0].relpath).unlink()
    res = run_restore(ritems, env)
    assert res.items[0].state == "restore_failed" and not corpus.path(ritems[0].relpath).exists()


# --------------------------------------------------------------------------- interrupted operations (real subprocess)

def _crash(corpus, env, failpoint, items, job_id):
    req = Path(env.data_dir) / f"{job_id}.req.json"
    req.write_text(json.dumps({"data_dir": env.data_dir, "job_id": job_id,
                               "items": [it.__dict__ for it in items]}))
    e = dict(os.environ, GAMA_ENABLE_FAILPOINTS="1", GAMA_FAILPOINTS=failpoint)
    r = subprocess.run([sys.executable, str(HERE.parent / "apply_child.py"), str(req)], env=e,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == fsutil.EXIT_CODE, (r.returncode, r.stdout, r.stderr[-2000:])


@pytest.mark.parametrize("failpoint,expected", [
    ("after_backup", ["failed", "failed", "failed"]),
    ("after_temp_written:1", ["committed", "failed", "failed"]),
    ("after_renaming_journaled:1", ["committed", "failed", "failed"]),
    ("after_rename:1", ["committed", "committed", "failed"]),
    ("after_commit:1", ["committed", "committed", "failed"]),
])
def test_interrupted_job_is_recovered_by_hash(corpus, env, failpoint, expected):
    items = items_for(corpus, rels(corpus, KC))
    _crash(corpus, env, failpoint, items, "crashjob")
    if "temp_written" in failpoint or "renaming" in failpoint:
        assert temps_left(corpus), "the crash should have left a temp file behind"
    reports = recover(env)
    assert len(reports) == 1 and reports[0]["status"] == "interrupted"
    assert temps_left(corpus) == []
    s = summarize(load(reports[0]["journal"]))
    assert s["status"] == "interrupted"
    states = [s["items"].get(i, {}).get("state") for i in range(3)]
    assert states == expected
    for it, st in zip(items, expected):
        cur = fsutil.sha256_file(corpus.path(it.relpath))
        if st == "committed":
            assert read_file(corpus.path(it.relpath)).genre.values[-1] == "Jazz"
        else:
            assert cur == it.sha_before
    assert recover(env) == []  # idempotent: the job is now terminal
    # Recovery never continues the job; the user can restore what was committed.
    committed = [it for it, st in zip(items, expected) if st == "committed"]
    if committed:
        ritems = [RestoreItem(it.n, it.root, it.relpath, it.sha_before, s["items"][it.n]["sha_after"])
                  for it in committed]
        assert run_restore(ritems, env).status == "succeeded"
    for it in items:
        assert corpus.path(it.relpath).read_bytes() == (corpus.master / it.relpath).read_bytes()


def test_recovery_runs_before_the_next_job(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    _crash(corpus, env, "after_temp_written:0", items, "crashed")
    res = run_apply(items_for(corpus, rels(corpus, KC)), env, job_id="next")
    assert len(res.recovered) == 1 and res.recovered[0]["job"] == "crashed"
    assert res.status == "succeeded" and temps_left(corpus) == []


def test_interrupted_rollback_is_recovered(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    fsutil.set_failpoint("item_start", _make_readonly(corpus.path(items[2].relpath)))
    fsutil.set_failpoint("restore_after_rename", lambda ctx: (_ for _ in ()).throw(KeyboardInterrupt())
                         if ctx["n"] == 1 else None)
    with pytest.raises(KeyboardInterrupt):  # simulates the process dying mid-rollback
        run_apply(items, env, job_id="rb")
    os.chmod(corpus.path(items[2].relpath), 0o644)
    reports = recover(env)
    assert reports and reports[0]["status"] == "interrupted"
    s = summarize(load(reports[0]["journal"]))
    assert [s["items"][n]["state"] for n in range(3)] == ["committed", "committed", "failed"]
    assert s["items"][0]["restore_state"] == "restored" and s["items"][1]["restore_state"] == "restored"
    assert temps_left(corpus) == []


# --------------------------------------------------------------------------- repeated operations

def test_repeated_operation_is_a_no_op_and_creates_no_backups(corpus, env):
    refs = [FileRef(str(corpus.root), r) for r in rels(corpus, KC)]
    p1 = plan_files(refs, "add", ["Jazz"])
    assert p1.n_change == 3
    items = [WriteItem(i.seq, i.root, i.relpath, i.format, i.sha_before, i.genres_before, i.genres_after)
             for i in p1.items if i.action == "change"]
    assert run_apply(items, env).status == "succeeded"
    n_backups = len(store_files(env))
    p2 = plan_files(refs, "add", ["Jazz"])
    assert p2.n_change == 0 and p2.n_nochange == 3
    assert len(store_files(env)) == n_backups
