"""Gama runtime configuration: ``gama.toml`` plus environment overrides.

No Django imports, so the config can be loaded and tested on its own.

Sources, later ones win:

1. the TOML file named by ``GAMA_CONFIG``, or ``$XDG_CONFIG_HOME/gama/gama.toml``
   if that exists;
2. environment variables ``GAMA_LIBRARY_ROOTS`` (``os.pathsep``-separated),
   ``GAMA_DATA_DIR``, ``GAMA_BACKUP_MARGIN_MB`` and ``GAMA_VERIFY_AUDIO``.

Validation refuses a ``data_dir`` inside (or equal to) any library root and any
library root inside ``data_dir``: backups, journals and the database must never
live in the collection, and the collection must never live in Gama's data.
"""
from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

VERIFY_AUDIO_CHOICES = ("auto", "off")
KNOWN_KEYS = {"library_roots", "data_dir", "backup_margin_mb", "verify_audio"}


class ConfigError(ValueError):
    """The configuration is invalid or unsafe."""


@dataclass(frozen=True)
class Config:
    library_roots: tuple[Path, ...]
    data_dir: Path
    backup_margin_mb: int = 64
    verify_audio: str = "auto"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "gama.sqlite3"


def _default_data_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME", "~")).expanduser() / ".local/share")
    return Path(base) / "gama"


def _default_config_file(env: Mapping[str, str]) -> Path | None:
    if "GAMA_CONFIG" in env:
        return Path(env["GAMA_CONFIG"]) if env["GAMA_CONFIG"] else None
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME", "~")).expanduser() / ".config")
    p = Path(base) / "gama" / "gama.toml"
    return p if p.is_file() else None


def _is_within(child: Path, parent: Path) -> bool:
    return child == parent or child.is_relative_to(parent)


def _abs_path(value: object, what: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{what} must be a non-empty path string, got {value!r}")
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise ConfigError(f"{what} must be an absolute path, got {value!r}")
    # resolve() follows symlinks, so a root reached through a link is compared
    # by where it really is.
    return p.resolve(strict=False)


def validate(cfg: Config) -> Config:
    """Raise ConfigError if the configuration is unsafe; return it otherwise."""
    if cfg.verify_audio not in VERIFY_AUDIO_CHOICES:
        raise ConfigError(f"verify_audio must be one of {VERIFY_AUDIO_CHOICES}, got {cfg.verify_audio!r}")
    if cfg.backup_margin_mb < 0:
        raise ConfigError("backup_margin_mb must be >= 0")
    seen: list[Path] = []
    for root in cfg.library_roots:
        if _is_within(cfg.data_dir, root):
            raise ConfigError(f"data_dir {cfg.data_dir} is inside library root {root}")
        if _is_within(root, cfg.data_dir):
            raise ConfigError(f"library root {root} is inside data_dir {cfg.data_dir}")
        for other in seen:
            if _is_within(root, other) or _is_within(other, root):
                raise ConfigError(f"library roots overlap: {other} and {root}")
        seen.append(root)
    return cfg


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    raw: dict = {}
    cfg_file = _default_config_file(env)
    if cfg_file is not None:
        try:
            raw = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"cannot read config file {cfg_file}: {e}") from e
        unknown = set(raw) - KNOWN_KEYS
        if unknown:
            raise ConfigError(f"unknown keys in {cfg_file}: {sorted(unknown)}")

    roots_raw = raw.get("library_roots", [])
    if "GAMA_LIBRARY_ROOTS" in env:
        roots_raw = [r for r in env["GAMA_LIBRARY_ROOTS"].split(os.pathsep) if r]
    if not isinstance(roots_raw, list):
        raise ConfigError("library_roots must be a list of paths")
    roots = tuple(_abs_path(r, "library root") for r in roots_raw)

    data_raw = env.get("GAMA_DATA_DIR") or raw.get("data_dir") or str(_default_data_dir(env))
    data_dir = _abs_path(data_raw, "data_dir")

    margin_raw = env.get("GAMA_BACKUP_MARGIN_MB", raw.get("backup_margin_mb", 64))
    try:
        margin = int(margin_raw)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"backup_margin_mb must be an integer, got {margin_raw!r}") from e

    verify_audio = str(env.get("GAMA_VERIFY_AUDIO", raw.get("verify_audio", "auto")))
    return validate(Config(roots, data_dir, margin, verify_audio))
