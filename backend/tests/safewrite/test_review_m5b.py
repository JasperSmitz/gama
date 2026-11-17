"""Regression tests for the M5 re-review (N1-N7)."""
import json
import os
import shutil
from pathlib import Path

import pytest

from gama.safewrite import fsutil
from gama.safewrite import writer as writer_mod
from gama.safewrite.env import WriteEnv
from gama.safewrite.journal import load, summarize
from gama.safewrite.recovery import recover, resolve_pending
from gama.safewrite.restore import RestoreItem, run_restore
from gama.safewrite.writer import run_apply
from tests.safewrite.test_engine import KC, items_for, rels, temps_left
from tests.safewrite.test_review_m5 import _crash, _items_in

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _no_failpoints():
    fsutil.clear_failpoints()
    yield
    fsutil.clear_failpoints()


@pytest.fixture
def env(tmp_path):
    return WriteEnv(tmp_path / "data", audio="off")


@pytest.fixture
def flat(corpus, tmp_path):
    """Loose tracks directly in a library root (no album folder)."""
    root = tmp_path / "flat"
    root.mkdir()
    for p in sorted((corpus.root / KC).glob("*.flac")):
        shutil.copyfile(p, root / p.name)
    return root


def states(journal):
    s = summarize(load(journal))
    return s["status"], {n: (v.get("state"), v.get("restore_state")) for n, v in s["items"].items()}


# --------------------------------------------------------------------------- N1

def test_N1_empty_mountpoint_defers_instead_of_failing(env, flat):
    items = _items_in(flat)
    _crash(env, "after_commit:1", items, "crashed")
    hidden = flat.with_name("flat-disk")
    os.rename(flat, hidden)
    flat.mkdir()  # the empty mountpoint: same path, different directory
    try:
        reports = recover(env)
        assert [r["status"] for r in reports] == ["pending"] and "mountpoint" in reports[0]["error"]
        assert summarize(load(reports[0]["journal"]))["status"] is None
    finally:
        flat.rmdir()
        os.rename(hidden, flat)
    reports = recover(env)
    st, items_state = states(reports[0]["journal"])
    assert st == "interrupted"
    assert [items_state[n][0] for n in range(3)] == ["committed", "committed", "failed"]


def test_N1_committed_file_gone_becomes_missing_not_failed(env, flat):
    items = _items_in(flat)
    _crash(env, "after_commit:1", items, "crashed")
    sha_after = summarize(load(Path(env.journal_dir, "crashed.jsonl")))["items"][0]["sha_after"]
    (flat / items[0].relpath).unlink()  # deleted by hand while the journal was open
    reports = recover(env)
    s = summarize(load(reports[0]["journal"]))
    assert s["items"][0]["state"] == "missing" and s["items"][0]["sha_after"] == sha_after
    assert s["items"][1]["state"] == "committed" and s["items"][2]["state"] == "failed"


# --------------------------------------------------------------------------- N2

def _corrupt_after_rename(n):
    def fp(ctx):
        if ctx["n"] == n:
            with open(ctx["path"], "ab") as f:
                f.write(b"someone else")
    return fp


def test_N2_after_rename_error_under_continue_is_committed(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    fsutil.set_failpoint("after_rename", lambda ctx: (_ for _ in ()).throw(OSError(5, "EIO"))
                         if ctx["n"] == 1 else None)
    res = run_apply(items, env, policy="continue")
    assert res.status == "succeeded" and [i.state for i in res.items] == ["committed"] * 3
    assert states(res.journal_path)[1][1] == ("committed", None)


def test_N2_bytes_changed_after_rename_continue(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    fsutil.set_failpoint("after_rename", _corrupt_after_rename(1))
    res = run_apply(items, env, policy="continue")
    assert res.status == "completed_with_errors"
    assert [i.state for i in res.items] == ["committed", "failed", "committed"]
    s = summarize(load(res.journal_path))
    assert s["items"][1]["stage"] == "post-rename" and s["items"][1]["sha_found"]


def test_N2_rollback_outcome_in_separate_field(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    fsutil.set_failpoint("after_rename", _corrupt_after_rename(1))
    res = run_apply(items, env)
    # Item 1 now holds someone else's bytes: its rollback is a conflict, never an overwrite.
    assert res.status == "failed_partial"
    assert [(i.state, i.restore_state) for i in res.items] == [
        ("committed", "restored"), ("failed", "restore_conflict"), ("skipped", None)]
    assert states(res.journal_path)[1] == {0: ("committed", "restored"), 1: ("failed", "restore_conflict"),
                                           2: ("skipped", None)}
    assert corpus.path(items[1].relpath).read_bytes().endswith(b"someone else")


# --------------------------------------------------------------------------- N3

def test_N3_renamed_album_folder_resolves_with_root_present(corpus, env):
    items = items_for(corpus, rels(corpus, KC))
    _crash(env, "after_temp_written:1", items, "crashed")
    album = corpus.root / KC
    os.rename(album, album.with_name("renamed"))
    reports = recover(env)
    assert reports[0]["status"] == "interrupted"  # temps in a vanished folder count as gone
    _, st = states(reports[0]["journal"])
    assert st[0][0] == "missing" and st[1][0] == "failed" and st[2][0] == "failed"


def test_N3_pending_job_needs_and_gets_an_explicit_resolution(env, flat, capsys, settings, tmp_path):
    items = _items_in(flat)
    _crash(env, "after_commit:1", items, "retired")
    retired = flat.with_name("retired-root")
    os.rename(flat, retired)  # the root is gone for good
    assert [r["status"] for r in recover(env)] == ["pending"]
    assert [r["status"] for r in recover(env)] == ["pending"]  # stays pending by itself
    r = resolve_pending(env, "retired")
    assert r["status"] == "pending" and len(r["unavailable"]) == 3
    assert summarize(load(r["journal"]))["status"] is None  # nothing written without confirmation
    r = resolve_pending(env, "retired", confirm_missing=True)
    assert r["status"] == "interrupted"
    _, st = states(r["journal"])
    assert [st[n][0] for n in range(3)] == ["missing", "missing", "failed"]
    assert recover(env) == []


def test_N3_gama_recover_command(env, flat, settings, capsys):
    from django.core.management import call_command

    from gama.config import Config
    settings.GAMA = Config((), Path(env.data_dir))
    items = _items_in(flat)
    _crash(env, "after_commit:1", items, "jobx")
    os.rename(flat, flat.with_name("gone"))
    call_command("gama_recover")
    assert "jobx: pending" in capsys.readouterr().out
    call_command("gama_recover", "--resolve", "jobx")
    assert "re-run with --confirm-missing" in capsys.readouterr().out
    call_command("gama_recover", "--resolve", "jobx", "--confirm-missing")
    assert "jobx: interrupted" in capsys.readouterr().out


# --------------------------------------------------------------------------- N4

def test_N4_unremovable_store_entry_does_not_block_jobs(corpus, env):
    os.mkdir(os.path.join(env.store.tmp, "stuck.part"))  # a directory named like a partial backup
    res = run_apply(items_for(corpus, rels(corpus, KC)[:1]), env)
    assert res.status == "succeeded"


def test_N4_malformed_header_is_contained(corpus, env, tmp_path):
    lib2 = tmp_path / "lib2"
    shutil.copytree(corpus.root / KC, lib2)
    done = run_apply(_items_in(lib2), env)
    Path(env.journal_dir, "bad.jsonl").write_text(
        json.dumps({"t": "header", "job": "bad", "items": [{"n": 0}]}) + "\n")
    reports = recover(env)
    assert any(r["status"] == "damaged" and r["paths"] is None for r in reports)
    refused = run_apply(items_for(corpus, rels(corpus, KC)), env)
    assert refused.status == "failed" and "unreadable journal" in refused.error
    out = run_restore([RestoreItem(i.n, str(lib2), i.relpath, it.sha_before, i.sha_after)
                       for i, it in zip(done.items, _items_in(lib2))], env)
    assert out.status in ("succeeded", "completed_with_errors")


# --------------------------------------------------------------------------- N5

def test_N5_foreign_file_at_temp_name_is_never_deleted(corpus, env, monkeypatch):
    items = items_for(corpus, rels(corpus, KC)[:1])
    foreign = corpus.path(items[0].relpath).parent / ".gama-tmp-not-mine.flac"
    foreign.write_bytes(b"someone else's file")
    monkeypatch.setattr(writer_mod, "_temp_path", lambda item, job_id: str(foreign))
    res = run_apply(items, env)
    assert res.items[0].state == "failed" and res.items[0].stage == "temp" and "FileExistsError" in res.items[0].error
    assert foreign.read_bytes() == b"someone else's file"
    assert summarize(load(res.journal_path))["temps"] == {}
    recover(env)
    assert foreign.exists()


# --------------------------------------------------------------------------- N6

def test_N6_blocking_compares_real_paths(corpus, env, tmp_path):
    items = items_for(corpus, rels(corpus, KC))
    header = {"t": "header", "job": "bad", "kind": "apply",
              "items": [{"n": 0, "root": items[0].root, "relpath": items[0].relpath}]}
    Path(env.journal_dir, "bad.jsonl").write_bytes((json.dumps(header) + "\n{broken\n{}\n").encode())
    alias = tmp_path / "alias"
    alias.symlink_to(corpus.root)
    via_alias = [writer_mod.WriteItem(0, str(alias), items[0].relpath, items[0].format, items[0].sha_before,
                                      items[0].genres_before, items[0].genres_after)]
    res = run_apply(via_alias, env)
    assert res.status == "failed" and "recovery" in res.error


# --------------------------------------------------------------------------- N7

def test_N7_leaked_temp_already_gone_is_marked_cleaned_once(corpus, env):
    items = items_for(corpus, rels(corpus, KC)[:1])
    res = run_apply(items, env)
    gone = str(corpus.path(items[0].relpath).parent / ".gama-tmp-x-0-gone.flac")
    with open(res.journal_path, "a") as f:
        f.write(json.dumps({"t": "item", "n": 0, "temp_leaked": gone}) + "\n")
    first = recover(env)
    assert [r["status"] for r in first] == ["cleaned"] and first[0]["temps_removed"] == [gone]
    assert recover(env) == []
    assert temps_left(corpus) == []
