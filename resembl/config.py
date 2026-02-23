"""Configuration loader for resembl."""

from __future__ import annotations

import dataclasses
import logging
import os
import tempfile

import tomli
import tomli_w

DEFAULT_CONFIG_DIR = "~/.config/resembl"


def config_dir_get() -> str:
    """Return the config directory, respecting the RESEMBL_CONFIG_DIR env var."""
    return os.path.expanduser(
        os.environ.get("RESEMBL_CONFIG_DIR", DEFAULT_CONFIG_DIR)
    )


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

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def __getitem__(self, key: str) -> object:
        return getattr(self, key)

    def __setitem__(self, key: str, value: object) -> None:
        setattr(self, key, value)


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
        tomli_w.dump(data, tmp)
        tmp_path = tmp.name

    os.replace(tmp_path, cfg_path)


def update_config(key: str, value: int | float | str) -> dict:
    """Update ``key`` in the config file with ``value`` and return the new config."""
    config: dict = {}
    cfg_path = config_path_get()
    if os.path.exists(cfg_path):
        with open(cfg_path, "rb") as f:
            try:
                config = tomli.load(f)
            except tomli.TOMLDecodeError:
                config = {}

    config[key] = value
    merged = {**DEFAULTS, **config}
    save_config(merged)
    return merged


def remove_config_key(key: str) -> dict:
    """Remove ``key`` from the config file and return the new config."""
    config: dict = {}
    cfg_path = config_path_get()
    if os.path.exists(cfg_path):
        with open(cfg_path, "rb") as f:
            try:
                config = tomli.load(f)
            except tomli.TOMLDecodeError:
                config = {}

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

    with open(cfg_path, "rb") as f:
        try:
            user_config = tomli.load(f)
            cfg.update(user_config)
        except tomli.TOMLDecodeError as e:
            logger.error("Error decoding config file at %s: %s", cfg_path, e)

    return cfg
