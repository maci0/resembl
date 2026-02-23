"""Configuration loader for resembl."""

import logging
import os
import tempfile

import tomli
import tomli_w

DEFAULT_CONFIG_DIR = "~/.config/asmatch"


def config_dir_get() -> str:
    """Return the config directory, respecting the ASMATCH_CONFIG_DIR env var."""
    return os.path.expanduser(
        os.environ.get("ASMATCH_CONFIG_DIR", DEFAULT_CONFIG_DIR)
    )


def config_path_get() -> str:
    """Return the path to the config file."""
    return os.path.join(config_dir_get(), "config.toml")

DEFAULTS = {
    "lsh_threshold": 0.5,
    "num_permutations": 128,
    "top_n": 5,
}

logger = logging.getLogger(__name__)


def save_config(config: dict) -> None:
    """Write ``config`` to the config file atomically."""
    cfg_dir = config_dir_get()
    cfg_path = config_path_get()
    if not os.path.exists(cfg_dir):
        os.makedirs(cfg_dir)

    with tempfile.NamedTemporaryFile("wb", dir=cfg_dir, delete=False) as tmp:
        tomli_w.dump(config, tmp)
        tmp_path = tmp.name

    os.replace(tmp_path, cfg_path)


def update_config(key: str, value: int | float) -> dict:
    """Update ``key`` in the config file with ``value`` and return the new config."""
    config = {}
    cfg_path = config_path_get()
    if os.path.exists(cfg_path):
        with open(cfg_path, "rb") as f:
            try:
                config = tomli.load(f)
            except tomli.TOMLDecodeError:
                config = {}

    config[key] = value
    save_config({**DEFAULTS, **config})
    return {**DEFAULTS, **config}


def remove_config_key(key: str) -> dict:
    """Remove ``key`` from the config file and return the new config."""
    config = {}
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


def load_config() -> dict:
    """Load the user's configuration file if it exists."""
    cfg_path = config_path_get()
    if not os.path.exists(cfg_path):
        return DEFAULTS

    with open(cfg_path, "rb") as f:
        try:
            user_config = tomli.load(f)
            # Merge user config with defaults, user config takes precedence
            return {**DEFAULTS, **user_config}
        except tomli.TOMLDecodeError as e:
            logger.error("Error decoding config file at %s: %s", cfg_path, e)
            return DEFAULTS
