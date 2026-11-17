import os
from pathlib import Path

import pytest

from gama.config import Config, ConfigError, load_config, validate

from ..conftest import production_paths


def env(tmp_path, **kw):
    e = {"HOME": str(tmp_path / "home"), "GAMA_CONFIG": ""}
    e.update(kw)
    return e


def test_defaults(tmp_path):
    cfg = load_config(env(tmp_path, XDG_DATA_HOME=str(tmp_path / "xdg")))
    assert cfg.library_roots == ()
    assert cfg.data_dir == (tmp_path / "xdg" / "gama").resolve()
    assert cfg.backup_margin_mb == 64
    assert cfg.verify_audio == "auto"
    assert cfg.database_path == cfg.data_dir / "gama.sqlite3"


def test_env_overrides(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    cfg = load_config(env(tmp_path, GAMA_LIBRARY_ROOTS=f"{a}{os.pathsep}{b}", GAMA_DATA_DIR=str(tmp_path / "d"),
                          GAMA_BACKUP_MARGIN_MB="10", GAMA_VERIFY_AUDIO="off"))
    assert cfg.library_roots == (a.resolve(), b.resolve())
    assert cfg.data_dir == (tmp_path / "d").resolve()
    assert (cfg.backup_margin_mb, cfg.verify_audio) == (10, "off")


def test_toml_file(tmp_path):
    f = tmp_path / "gama.toml"
    f.write_text(f'library_roots = ["{tmp_path / "lib"}"]\ndata_dir = "{tmp_path / "data"}"\nbackup_margin_mb = 5\n')
    cfg = load_config(env(tmp_path, GAMA_CONFIG=str(f)))
    assert cfg.library_roots == ((tmp_path / "lib").resolve(),)
    assert cfg.backup_margin_mb == 5


def test_toml_unknown_key_refused(tmp_path):
    f = tmp_path / "gama.toml"
    f.write_text('librry_roots = ["/x"]\n')
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(env(tmp_path, GAMA_CONFIG=str(f)))


@pytest.mark.parametrize("data_rel", ["lib", "lib/.gama", "lib/sub/data"])
def test_refuses_data_dir_inside_root(tmp_path, data_rel):
    with pytest.raises(ConfigError, match="inside library root"):
        load_config(env(tmp_path, GAMA_LIBRARY_ROOTS=str(tmp_path / "lib"), GAMA_DATA_DIR=str(tmp_path / data_rel)))


def test_refuses_root_inside_data_dir(tmp_path):
    with pytest.raises(ConfigError, match="inside data_dir"):
        load_config(env(tmp_path, GAMA_LIBRARY_ROOTS=str(tmp_path / "data/music"), GAMA_DATA_DIR=str(tmp_path / "data")))


def test_refuses_data_dir_inside_root_via_symlink(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "lib")
    with pytest.raises(ConfigError, match="inside library root"):
        load_config(env(tmp_path, GAMA_LIBRARY_ROOTS=str(tmp_path / "lib"), GAMA_DATA_DIR=str(tmp_path / "link/data")))


def test_sibling_prefix_is_not_inside(tmp_path):
    # /x/lib-data is not inside /x/lib, despite the string prefix.
    cfg = load_config(env(tmp_path, GAMA_LIBRARY_ROOTS=str(tmp_path / "lib"), GAMA_DATA_DIR=str(tmp_path / "lib-data")))
    assert cfg.data_dir.name == "lib-data"


def test_refuses_overlapping_roots(tmp_path):
    roots = f"{tmp_path / 'lib'}{os.pathsep}{tmp_path / 'lib/sub'}"
    with pytest.raises(ConfigError, match="overlap"):
        load_config(env(tmp_path, GAMA_LIBRARY_ROOTS=roots, GAMA_DATA_DIR=str(tmp_path / "d")))


@pytest.mark.parametrize("kw,match", [
    ({"GAMA_LIBRARY_ROOTS": "relative/path"}, "absolute"),
    ({"GAMA_DATA_DIR": "relative"}, "absolute"),
    ({"GAMA_VERIFY_AUDIO": "sometimes"}, "verify_audio"),
    ({"GAMA_BACKUP_MARGIN_MB": "lots"}, "integer"),
    ({"GAMA_BACKUP_MARGIN_MB": "-1"}, ">= 0"),
])
def test_invalid_values(tmp_path, kw, match):
    with pytest.raises(ConfigError, match=match):
        load_config(env(tmp_path, **kw))


def test_validate_direct():
    with pytest.raises(ConfigError):
        validate(Config((Path("/m"),), Path("/m")))


def test_production_guard_detects_music_paths(tmp_path):
    assert production_paths([Path("/storage/music")]) == [Path("/storage/music")]
    assert production_paths([Path("/storage/music/Artist/Album")])
    assert production_paths([Path("/storage/music/../music/x")])
    assert not production_paths([Path("/storage/musical"), tmp_path])


def test_production_guard_follows_symlinks(tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to("/storage/music")  # only the link is created; the target is never opened
    assert production_paths([link / "x"])


def test_test_settings_are_safe(settings):
    assert settings.GAMA.library_roots == ()
    assert not production_paths([settings.GAMA.data_dir])
