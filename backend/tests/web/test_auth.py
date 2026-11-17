import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.db import connections
from django.urls import URLPattern, URLResolver, get_resolver

ALLOWED_ANONYMOUS = {"login", "logout", "admin:login"}


def iter_urls(patterns, prefix="", ns=None):
    for p in patterns:
        if isinstance(p, URLResolver):
            sub_ns = ":".join(filter(None, [ns, p.namespace])) or None
            yield from iter_urls(p.url_patterns, prefix + str(p.pattern), sub_ns)
        elif isinstance(p, URLPattern):
            name = f"{ns}:{p.name}" if ns and p.name else p.name
            yield prefix + str(p.pattern), name, p.callback


ALL_URLS = list(iter_urls(get_resolver().url_patterns))


def is_exempt(callback) -> bool:
    # What LoginRequiredMiddleware itself checks.
    return getattr(callback, "login_required", True) is False


def test_urlconf_is_not_empty():
    names = {n for _, n, _ in ALL_URLS}
    assert {"home", "login", "logout", "admin:index"} <= names


@pytest.mark.parametrize("route,name,callback", ALL_URLS, ids=[f"{r}|{n}" for r, n, _ in ALL_URLS])
def test_every_view_requires_login(route, name, callback):
    if is_exempt(callback):
        assert name in ALLOWED_ANONYMOUS, f"{route} ({name}) is reachable without login"


@pytest.mark.django_db
@pytest.mark.parametrize("route", sorted({r for r, _, cb in ALL_URLS
                                          if not is_exempt(cb) and not any(c in r for c in "<^$()?")}))
def test_anonymous_get_redirects_to_login(client, route):
    r = client.get("/" + route)
    assert r.status_code == 302, route
    assert r["Location"].startswith(("/accounts/login/", "/admin/login/")), r["Location"]


@pytest.mark.django_db
def test_root_redirects_to_login(client):
    r = client.get("/")
    assert r.status_code == 302
    assert r["Location"] == "/accounts/login/?next=/"


@pytest.mark.django_db
def test_login_page_renders(client):
    r = client.get("/accounts/login/")
    assert r.status_code == 200
    assert b'name="password"' in r.content


@pytest.mark.django_db
def test_login_then_home_then_logout(client, django_user_model):
    django_user_model.objects.create_user("alice", password="s3cret-pass")
    r = client.post("/accounts/login/", {"username": "alice", "password": "s3cret-pass"})
    assert r.status_code == 302 and r["Location"] == "/"
    r = client.get("/")
    assert r.status_code == 200 and b"alice" in r.content
    r = client.post("/accounts/logout/")
    assert r.status_code == 302
    assert client.get("/").status_code == 302


@pytest.mark.django_db
def test_admin_needs_staff(client, django_user_model):
    django_user_model.objects.create_user("bob", password="s3cret-pass")
    client.login(username="bob", password="s3cret-pass")
    r = client.get("/admin/")
    assert r.status_code == 302 and r["Location"].startswith("/admin/login/")


@pytest.mark.django_db
def test_logout_requires_post(client, django_user_model):
    django_user_model.objects.create_user("carol", password="s3cret-pass")
    client.login(username="carol", password="s3cret-pass")
    assert client.get("/accounts/logout/").status_code == 405


@pytest.mark.django_db
def test_sqlite_pragmas(tmp_path):
    cfg = dict(connections.settings["default"])
    cfg["NAME"] = str(tmp_path / "pragma.sqlite3")
    wrapper = type(connections["default"])(cfg, alias="pragma_check")
    try:
        with wrapper.cursor() as c:
            c.execute("PRAGMA journal_mode")
            assert c.fetchone()[0] == "wal"
            c.execute("PRAGMA synchronous")
            assert c.fetchone()[0] == 2  # FULL
    finally:
        wrapper.close()


def test_production_settings_require_secret_key(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GAMA_", "DJANGO_"))}
    env.update(GAMA_CONFIG="", GAMA_DATA_DIR=str(tmp_path / "d"), DJANGO_SETTINGS_MODULE="gama.settings")
    src = Path(__file__).resolve().parents[2] / "src"
    code = "import django; django.setup()"
    r = subprocess.run([sys.executable, "-c", code], env=env | {"PYTHONPATH": str(src)},
                       capture_output=True, text=True)
    assert r.returncode != 0 and "GAMA_SECRET_KEY" in r.stderr
    r = subprocess.run([sys.executable, "-c", code], env=env | {"PYTHONPATH": str(src), "GAMA_DEBUG": "1"},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
