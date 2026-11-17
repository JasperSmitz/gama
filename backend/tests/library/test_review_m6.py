"""Regression tests for the M6 review (findings 1-7)."""
import os
import shutil
import subprocess

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from gama.config import Config
from gama.library import basket
from gama.library.models import Album, LibraryRoot, MediaFile, Track, visible_albums
from gama.library.scanner import ScanError, scan_root
from gama.metadata.model import ReadResult
from gama.metadata.reader import read_file

pytestmark = [pytest.mark.slow, pytest.mark.django_db]

KC = "King Crimson/1969 - In the Court of the Crimson King"


def inproc(paths):
    return [read_file(p) for p in paths]


@pytest.fixture
def lib(tmp_path, corpus, settings):
    """A small configured library: one artist dir with one album of 3 tracks."""
    root = tmp_path / "lib"
    shutil.copytree(corpus.root / KC, root / "Art" / "Alb")
    for p in root.rglob("*"):
        if p.is_file():
            p.chmod(0o644)
    settings.GAMA = Config((root.resolve(),), tmp_path / "data")
    return LibraryRoot.objects.create(path=str(root.resolve()), name="lib")


# --------------------------------------------------------------------------- 1: unlistable directories

def test_unlistable_directory_keeps_its_rows(lib):
    scan_root(lib, reader=inproc)
    art = os.path.join(lib.path, "Art")
    os.chmod(art, 0)
    try:
        s = scan_root(lib, reader=inproc)
    finally:
        os.chmod(art, 0o755)
    assert s.removed == 0 and s.unlisted_dirs == ["Art"]
    assert MediaFile.objects.count() == 3 and Album.objects.count() == 1


def test_unlistable_root_aborts_without_changes(lib):
    scan_root(lib, reader=inproc)
    os.chmod(lib.path, 0)
    try:
        with pytest.raises(ScanError, match="cannot list"):
            scan_root(lib, reader=inproc)
    finally:
        os.chmod(lib.path, 0o755)
    assert MediaFile.objects.count() == 3


def test_empty_root_with_index_refused_unless_allowed(lib):
    scan_root(lib, reader=inproc)
    hidden = lib.path + "-real"
    os.rename(lib.path, hidden)
    os.mkdir(lib.path)  # an empty mountpoint
    try:
        with pytest.raises(ScanError, match="mounted"):
            scan_root(lib, reader=inproc)
        assert MediaFile.objects.count() == 3
        assert scan_root(lib, reader=inproc, allow_empty=True).removed == 3
    finally:
        os.rmdir(lib.path)
        os.rename(hidden, lib.path)


def test_gama_scan_reports_unlisted_dirs_and_aborts(lib, capsys):
    call_command("gama_scan")
    os.chmod(os.path.join(lib.path, "Art"), 0)
    try:
        call_command("gama_scan")
    finally:
        os.chmod(os.path.join(lib.path, "Art"), 0o755)
    assert "could not list Art" in capsys.readouterr().err
    os.chmod(lib.path, 0)
    try:
        with pytest.raises(CommandError, match="scan failed"):
            call_command("gama_scan")
    finally:
        os.chmod(lib.path, 0o755)


# --------------------------------------------------------------------------- 2 and 5: basket

class FakeRequest:
    def __init__(self):
        self.session = {}


def test_basket_deduplicates(lib):
    scan_root(lib, reader=inproc)
    aid = Album.objects.get().id
    req = FakeRequest()
    assert basket.add(req, [aid, aid]) == (1, 0)
    assert req.session[basket.KEY] == [aid]
    req.session[basket.KEY] = [aid, aid, aid]  # an old, bad session repairs itself
    assert basket.ids(req) == [aid] and req.session[basket.KEY] == [aid]


def test_basket_cap_reports_refused(lib, corpus, monkeypatch):
    shutil.copytree(corpus.root / "Yes", os.path.join(lib.path, "Yes"))
    scan_root(lib, reader=inproc)
    a, b = Album.objects.order_by("id").values_list("id", flat=True)
    monkeypatch.setattr(basket, "MAX_ALBUMS", 1)
    req = FakeRequest()
    assert basket.add(req, [a, b]) == (1, 1)
    assert req.session[basket.KEY] == [a]


def test_basket_cap_message(lib, client, django_user_model, corpus, monkeypatch):
    shutil.copytree(corpus.root / "Yes", os.path.join(lib.path, "Yes"))
    scan_root(lib, reader=inproc)
    django_user_model.objects.create_user("u", password="s3cret-pass")
    client.login(username="u", password="s3cret-pass")
    monkeypatch.setattr(basket, "MAX_ALBUMS", 1)
    r = client.post("/library/basket/add/", {"album": list(Album.objects.values_list("id", flat=True))}, follow=True)
    body = r.content.decode()
    assert "1 album(s) added" in body and "were not added" in body


# --------------------------------------------------------------------------- 3: unconfigured roots

def test_albums_of_unconfigured_roots_are_invisible(lib, settings, tmp_path, client, django_user_model):
    scan_root(lib, reader=inproc)
    aid = Album.objects.get().id
    django_user_model.objects.create_user("u", password="s3cret-pass")
    client.login(username="u", password="s3cret-pass")
    client.post("/library/basket/add/", {"album": [aid]})
    assert client.session["gama_basket"] == [aid]
    settings.GAMA = Config((), tmp_path / "data")  # root removed from the configuration
    assert visible_albums().count() == 0
    assert "Alb" not in client.get("/library/albums/").content.decode().split("<tbody>")[1]
    assert client.get(f"/library/albums/{aid}/").status_code == 404
    assert "Selection (0)" in client.get("/library/basket/").content.decode()
    assert client.post("/library/basket/add/", {"album": [aid]}) and client.session["gama_basket"] == []


# --------------------------------------------------------------------------- 4: failed reads retried; unreadable files shown

def test_failed_read_of_new_file_is_retried(lib):
    target = os.path.join(lib.path, "Art", "Alb", sorted(os.listdir(os.path.join(lib.path, "Art", "Alb")))[1])

    def crashing(paths):
        return [ReadResult(p, False, reason="reader-crashed", error="boom", skip_reason="unreadable")
                if p == target else read_file(p) for p in paths]
    scan_root(lib, reader=crashing)
    mf = MediaFile.objects.get(relpath=os.path.relpath(target, lib.path))
    assert not mf.read_ok and mf.size == -1
    s = scan_root(lib, reader=inproc)
    assert s.updated == 1 and s.unchanged == 2
    mf.refresh_from_db()
    assert mf.read_ok and Track.objects.filter(file=mf).exists()


def test_unreadable_files_shown_with_their_album(lib, client, django_user_model):
    alb = os.path.join(lib.path, "Art", "Alb")
    with open(os.path.join(alb, "99 broken.flac"), "wb") as f:
        f.write(b"\0" * 100)
    scan_root(lib, reader=inproc)
    a = Album.objects.get()
    assert a.track_count == 3
    assert [m.relpath for m in a.unreadable_files()] == ["Art/Alb/99 broken.flac"]
    django_user_model.objects.create_user("u", password="s3cret-pass")
    client.login(username="u", password="s3cret-pass")
    body = client.get(f"/library/albums/{a.id}/").content.decode()
    assert "99 broken.flac" in body and "could not be read" in body


# --------------------------------------------------------------------------- 6: disc count

def test_disc_dir_and_disc_tag_give_the_same_disc(lib):
    alb = os.path.join(lib.path, "Art", "Alb")
    cd1 = os.path.join(alb, "CD1")
    os.mkdir(cd1)
    names = sorted(n for n in os.listdir(alb) if n.endswith(".flac"))
    for n in names:
        os.rename(os.path.join(alb, n), os.path.join(cd1, n))
    subprocess.run(["metaflac", "--set-tag=DISCNUMBER=1", os.path.join(cd1, names[0])], check=True)
    subprocess.run(["metaflac", "--remove-tag=DISCNUMBER", os.path.join(cd1, names[1])], check=True)
    scan_root(lib, reader=inproc)
    (a,) = Album.objects.all()
    assert a.album_dir == "Art/Alb" and a.disc_count == 1


# --------------------------------------------------------------------------- 7: gama_demo_library safety

def test_demo_library_refuses_production_root_before_touching_it(tmp_path, monkeypatch):
    import fixtures.build as b
    prod = tmp_path / "music"
    prod.mkdir()
    monkeypatch.setattr(b, "PRODUCTION_ROOTS", (prod,))
    with pytest.raises(CommandError, match="production or library root"):
        call_command("gama_demo_library", str(prod / "demo"))
    assert list(prod.iterdir()) == []


def test_demo_library_refuses_a_file_target(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(CommandError, match="not a directory"):
        call_command("gama_demo_library", str(f))
