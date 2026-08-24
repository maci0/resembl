"""Configuration loader for resembl."""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import math
import os
import tempfile
import tomllib
from collections.abc import Iterator
from typing import TypeVar, overload

import tomli_w

DEFAULT_CONFIG_DIR = "~/.config/resembl"

#: Output formats every renderer switches on (``--format`` / config ``format``).
#: Anything else cannot be rendered: reject it where it enters instead of
#: letting commands silently fall back to one branch or another.
FORMATS = ("table", "json", "csv")

_T = TypeVar("_T")


def config_dir_get() -> str:
    """Return the config directory, respecting override environment variables.

    ``RESEMBL_CONFIG_DIR`` wins outright.  Otherwise ``$XDG_CONFIG_HOME`` is
    honored when set (freedesktop base-directory spec), falling back to the
    historical ``~/.config/resembl`` default.
    """
    override = os.environ.get("RESEMBL_CONFIG_DIR")
    if override:
        return os.path.expanduser(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "resembl")
    return os.path.expanduser(DEFAULT_CONFIG_DIR)


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
        """Merge values from *other* into this config.

        Every value is coerced to its field's type before being applied:
        a hand-edited or stale config file must not put a raw TOML value
        (a quoted ``lsh_threshold = "0.7"``, say) into a field whose readers
        compare it numerically — unvalidated, every ``find`` crashed with a
        TypeError instead of running on defaults.  Numeric coercion accepts
        the legal cross-type spellings (``0`` for a float field,
        ``128.0`` for an int field); anything the constructor rejects is
        warned about and skipped, like the malformed-TOML path in
        :func:`load_config`.  Non-finite floats (TOML's ``nan`` / ``inf``)
        are likewise rejected: an int field coerced from infinity raises
        OverflowError, and a NaN weight reaching scoring would make every
        similarity score NaN.  An out-of-enum ``format`` is also rejected
        (warn and keep the current value) so the file cannot put every
        command into an undefined render branch.
        """
        source = other if isinstance(other, dict) else dataclasses.asdict(other)
        for key, value in source.items():
            if not hasattr(self, key):
                continue
            default = DEFAULTS[key]
            try:
                value = type(default)(value)
            except (TypeError, ValueError, OverflowError):
                logger.warning(
                    "Ignoring %s: expected %s, got %r.",
                    key,
                    type(default).__name__,
                    value,
                )
                continue
            if isinstance(default, float) and not math.isfinite(value):
                logger.warning(
                    "Ignoring %s: expected a finite %s, got %r.",
                    key,
                    type(default).__name__,
                    value,
                )
                continue
            if key == "format" and value not in FORMATS:
                logger.warning(
                    "Ignoring unknown output format %r (expected one of: %s).",
                    value,
                    ", ".join(FORMATS),
                )
                continue
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


@contextlib.contextmanager
def _config_file_lock() -> Iterator[None]:
    """Hold an exclusive cross-process lock around a config-file update.

    ``update_config`` and ``remove_config_key`` are read-modify-write cycles
    (read the whole file, mutate the dict, write it back): two concurrent
    CLI processes would each read the same starting state, and the second
    writer would silently drop the first one's change.  The lock is taken on
    a sidecar file that is never replaced (``save_config`` publishes via
    ``os.replace``, which swaps the inode out from under any lock held on
    ``config.toml`` itself).  The OS releases the lock when the holder dies,
    so a crashed process cannot leave a stale lock behind; platforms with
    neither locking API fall back to the historical unlocked behavior.
    """
    os.makedirs(config_dir_get(), exist_ok=True)
    fd = os.open(config_path_get() + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            try:
                import msvcrt

                # typeshed provides these attributes for Windows only.
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
            except ImportError:
                # A platform with neither locking API (some non-CPython
                # builds) runs unlocked rather than refusing every config
                # update — the historical behavior.
                pass
        yield
    finally:
        # Closing the descriptor releases both the flock and the msvcrt lock.
        os.close(fd)


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
    with _config_file_lock():
        config = _read_config_dict()
        config[key] = value
        # The file stores user overrides only (like ``remove_config_key``):
        # baking every default into the file would pin users to this release's
        # default values forever.  Callers get the effective merged view.
        save_config(config)
    return {**DEFAULTS, **config}


def remove_config_key(key: str) -> dict:
    """Remove ``key`` from the config file and return the new config."""
    with _config_file_lock():
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
