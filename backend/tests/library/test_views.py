"""M6: album list, search, album page, session basket, commands."""
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from gama.config import Config
from gama.library.models import Album, LibraryRoot
from gama.library.scanner import scan_root
from gama.metadata.reader import read_file

pytestmark = [pytest.mark.slow, pytest.mark.django_db]


@pytest.fixture
def scanned(corpus, settings, tmp_path):
    settings.GAMA = Config((corpus.root.resolve(),), tmp_path / "data")
    root = LibraryRoot.objects.create(path=str(corpus.root.resolve()), name="corpus")
    scan_root(root, reader=lambda ps: [read_file(p) for p in ps])
    return root


@pytest.fixture
def user_client(client, django_user_model):
    django_user_model.objects.create_user("alice", password="s3cret-pass")
    client.login(username="alice", password="s3cret-pass")
    return client


def album(name, artist=None):
    qs = Album.objects.filter(album=name)
    return qs.get(albumartist=artist) if artist else qs.get()


def test_login_required(client, scanned):
    for url in ("/library/albums/", "/library/basket/", f"/library/albums/{Album.objects.first().id}/"):
        r = client.get(url)
        assert r.status_code == 302 and r["Location"].startswith("/accounts/login/")
    r = client.post("/library/basket/add/", {"album": Album.objects.first().id})
    assert r.status_code == 302 and r["Location"].startswith("/accounts/login/")


def test_album_list_shows_identifying_details(user_client, scanned):
    r = user_client.get("/library/albums/")
    body = r.content.decode()
    assert r.status_code == 200
    for text in ("In the Court of the Crimson King", "Selling England by the Pound", "Close to the Edge",
                 "Gama Test Orchestra/Two Disc Set", "11111111-1111-4111-8111-111111111111", "inconsistent"):
        assert text in body


@pytest.mark.parametrize("q,present,absent", [
    ("crimson", "In the Court of the Crimson King", "Close to the Edge"),        # album artist
    ("selling", "Selling England by the Pound", "In the Court"),                  # album
    ("2019 Remaster", "Anniversary", "Close to the Edge"),                        # folder
])
def test_search(user_client, scanned, q, present, absent):
    body = user_client.get("/library/albums/", {"q": q}).content.decode()
    assert present in body and absent not in body.split("<tbody>")[1]


def test_album_page_lists_tracks_and_problems(user_client, scanned):
    zoo = Album.objects.filter(album="Format Zoo").order_by("-track_count").first()
    body = user_client.get(f"/library/albums/{zoo.id}/").content.decode()
    assert "not writable" in body and "flac_mixed_style.flac" in body


def test_basket_add_persists_across_requests(user_client, scanned):
    kc, yes = album("In the Court of the Crimson King"), album("Close to the Edge")
    r = user_client.post("/library/basket/add/", {"album": [kc.id, yes.id, 999999, "x"]})
    assert r.status_code == 302 and r["Location"] == "/library/basket/"
    body = user_client.get("/library/basket/").content.decode()
    assert "In the Court of the Crimson King" in body and "Close to the Edge" in body
    # The selection stays visible on other pages.
    other = user_client.get("/library/albums/", {"q": "genesis"}).content.decode()
    assert 'aria-label="Selected albums"' in other and "In the Court of the Crimson King" in other
    assert "Selection (2)" in other and "Selection (2)" in user_client.get("/").content.decode()


def test_basket_add_is_idempotent_and_ordered(user_client, scanned):
    kc, yes = album("In the Court of the Crimson King"), album("Close to the Edge")
    user_client.post("/library/basket/add/", {"album": [yes.id]})
    user_client.post("/library/basket/add/", {"album": [kc.id, yes.id]})
    assert user_client.session["gama_basket"] == [yes.id, kc.id]


def test_basket_remove_and_clear(user_client, scanned):
    kc, yes = album("In the Court of the Crimson King"), album("Close to the Edge")
    user_client.post("/library/basket/add/", {"album": [kc.id, yes.id]})
    user_client.post("/library/basket/remove/", {"album": [kc.id]})
    assert user_client.session["gama_basket"] == [yes.id]
    user_client.post("/library/basket/clear/")
    assert user_client.session["gama_basket"] == []


def test_basket_drops_albums_removed_by_a_rescan(user_client, scanned):
    yes = album("Close to the Edge")
    user_client.post("/library/basket/add/", {"album": [yes.id]})
    yes.delete()
    assert "Selection (0)" in user_client.get("/library/basket/").content.decode()
    assert user_client.session["gama_basket"] == []


def test_basket_requires_post(user_client, scanned):
    assert user_client.get("/library/basket/add/").status_code == 405


def test_redirect_after_add_never_leaves_the_site(user_client, scanned):
    kc = album("In the Court of the Crimson King")
    r = user_client.post("/library/basket/add/", {"album": kc.id, "next": "https://evil.example/"})
    assert r["Location"] == "/library/basket/"
    r = user_client.post("/library/basket/add/", {"album": kc.id, "next": "/library/albums/?q=x"})
    assert r["Location"] == "/library/albums/?q=x"


# --------------------------------------------------------------------------- commands

def test_gama_scan_command(corpus, settings, tmp_path, capsys):
    settings.GAMA = Config((corpus.root.resolve(),), tmp_path / "data")
    call_command("gama_scan")
    out = capsys.readouterr().out
    assert f"{len(corpus.manifest)} added" in out
    assert Album.objects.filter(album="Close to the Edge").count() == 1
    call_command("gama_scan", "--root", str(corpus.root))
    assert "0 added, 0 updated, 0 removed" in capsys.readouterr().out


def test_gama_scan_refuses_without_roots(settings, tmp_path):
    settings.GAMA = Config((), tmp_path / "data")
    with pytest.raises(CommandError, match="No library roots"):
        call_command("gama_scan")


def test_gama_scan_refuses_unknown_root(corpus, settings, tmp_path):
    settings.GAMA = Config((corpus.root.resolve(),), tmp_path / "data")
    with pytest.raises(CommandError, match="not a configured"):
        call_command("gama_scan", "--root", str(tmp_path))


def test_gama_demo_library(tmp_path, capsys):
    out = tmp_path / "demo"
    call_command("gama_demo_library", str(out))
    assert (out / "library" / "King Crimson").is_dir()
    assert "files in" in capsys.readouterr().out
    with pytest.raises(CommandError, match="not empty"):
        call_command("gama_demo_library", str(out))
