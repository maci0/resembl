"""Configuration loader for resembl."""

from __future__ import annotations

import dataclasses
import logging
import os
import tempfile
import tomllib
from typing import TypeVar, overload

import tomli_w

DEFAULT_CONFIG_DIR = "~/.config/resembl"

_T = TypeVar("_T")


def config_dir_get() -> str:
    """Return the config directory, respecting the RESEMBL_CONFIG_DIR env var."""
    return os.path.expanduser(os.environ.get("RESEMBL_CONFIG_DIR", DEFAULT_CONFIG_DIR))


def config_path_get() -> str:
    """Return the path to the config file."""
    return os.path.join(config_dir_get(), "config.toml")


@dataclasses.dataclass
class ResemblConfig:
    """Typed configuration for resembl with defaults.

    Provides dict-like access (``get``, ``items``, ``update``, ``clear``)
    so that callers can migrate incrementally.
    """

    lsh_threshold: float = 0.5
    num_permutations: int = 128
    top_n: int = 5
    ngram_size: int = 3
    jaccard_weight: float = 0.4
    format: str = "table"

    # ---- dict-compatible helpers ----

    @overload
    def get(self, key: str, default: _T) -> _T: ...

    @overload
    def get(self, key: str, default: None = None) -> object: ...

    def get(self, key: str, default: object = None) -> object:
        """Return the value for *key* if it exists, else *default*."""
        if hasattr(self, key):
            return getattr(self, key)
        return default

    def items(self) -> list[tuple[str, object]]:
        """Return all configuration key-value pairs."""
        return [(f.name, getattr(self, f.name)) for f in dataclasses.fields(self)]

    def update(self, other: dict | ResemblConfig) -> None:
        """Merge values from *other* into this config."""
        source = other if isinstance(other, dict) else dataclasses.asdict(other)
        for key, value in source.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def clear(self) -> None:
        """Reset all fields to their defaults."""
        defaults = ResemblConfig()
        for f in dataclasses.fields(self):
            setattr(self, f.name, getattr(defaults, f.name))

    def to_dict(self) -> dict:
        """Return a plain dict representation for serialization."""
        return dataclasses.asdict(self)


# Keep DEFAULTS as a dict for backward compatibility (used by CLI validation
# and test_config.py).
DEFAULTS = ResemblConfig().to_dict()

logger = logging.getLogger(__name__)


def save_config(config: dict | ResemblConfig) -> None:
    """Write ``config`` to the config file atomically."""
    cfg_dir = config_dir_get()
    cfg_path = config_path_get()
    os.makedirs(cfg_dir, exist_ok=True)

    data = config if isinstance(config, dict) else config.to_dict()
    with tempfile.NamedTemporaryFile("wb", dir=cfg_dir, delete=False) as tmp:
        tmp_path = tmp.name
        try:
            tomli_w.dump(data, tmp)
        except Exception:
            # The half-written temp file must not outlive a failed save.
            tmp.close()
            os.unlink(tmp_path)
            raise

    try:
        os.replace(tmp_path, cfg_path)
    except OSError:
        # The temp file would otherwise accumulate in the config directory on
        # every failed save (e.g. read-only target); remove it and re-raise.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_config_dict() -> dict:
    """Read the raw config file as a dict (empty when missing or malformed)."""
    cfg_path = config_path_get()
    if not os.path.exists(cfg_path):
        return {}
    try:
        with open(cfg_path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError:
        return {}


def update_config(key: str, value: int | float | str) -> dict:
    """Update ``key`` in the config file with ``value`` and return the new config."""
    config = _read_config_dict()
    config[key] = value
    merged = {**DEFAULTS, **config}
    save_config(merged)
    return merged


def remove_config_key(key: str) -> dict:
    """Remove ``key`` from the config file and return the new config."""
    config = _read_config_dict()
    if key in config:
        del config[key]
        save_config(config)

    return {**DEFAULTS, **config}


def load_config() -> ResemblConfig:
    """Load the user's configuration file and return a typed config object."""
    cfg_path = config_path_get()
    cfg = ResemblConfig()

    if not os.path.exists(cfg_path):
        return cfg

    try:
        with open(cfg_path, "rb") as f:
            user_config = tomllib.load(f)
        cfg.update(user_config)
    except tomllib.TOMLDecodeError as e:
        logger.error("Error decoding config file at %s: %s", cfg_path, e)
    except OSError as e:
        # Unreadable file (permissions, I/O error): run on defaults like
        # the malformed-TOML path instead of crashing every command.
        logger.error("Error reading config file at %s: %s", cfg_path, e)

    return cfg
