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
from typing import NamedTuple

import tomli_w

from .paths import config_dir_get, config_path_get
from .scoring import MAX_NUM_PERM

#: Output formats every renderer switches on (``--format`` / config ``format``).
#: Anything else cannot be rendered: reject it where it enters instead of
#: letting commands silently fall back to one branch or another.
FORMATS = ("table", "json", "csv")


#: A half-open numeric range: ``low`` always inclusive, ``high`` only when
#: ``high_inclusive`` is set (unbounded above otherwise).
class Range(NamedTuple):
    """Bounds one configuration value has to stay inside."""

    low: float
    high: float = float("inf")
    high_inclusive: bool = False


#: ``lsh_threshold`` is accepted strictly below this bound: the banding needs
#: at least 2 bands, which caps a buildable threshold below 1.0 (0.981 gives
#: b=1 at 128 permutations).  A threshold inside this range can still be
#: unbuildable at a small permutation count; that combination is refused by
#: the caller that pairs the two values (``cli._validate_find_params`` and
#: the index build), not here.
THRESHOLD_MAX = 0.99

#: Range each numeric setting has to stay inside, checked after the value has
#: been coerced to its field's type.  These are the ranges the rest of the
#: code actually works in, so a value outside one is a misconfiguration the
#: user must hear about where it is written, not a runtime failure (or a
#: silently wrong index) later:
#:
#: ``num_permutations``
#:     The lower bound is what MinHash construction accepts; the upper bound is
#:     ``scoring.MAX_NUM_PERM`` ("real configurations use 64-128"), the same
#:     cap the server applies to request parameters and stored blobs.
#: ``ngram_size``
#:     Below 1 every shingle collapses to the empty token tuple, so every
#:     snippet matches every other one without any error being raised.
#: ``jaccard_weight``
#:     A 0-1 balance between the LSH and Jaccard scores, per ``score_hybrid``;
#:     both ends are usable (1.0 is a pure Jaccard ranking).
#: ``top_n``
#:     At least one result; 0 (or a negative value) truncates every ranking to
#:     nothing, which reads as "no matches" rather than as a bad setting.
#: No upper bound is imposed on ``top_n`` or ``ngram_size``: a large value only
#: returns more rows or builds coarser shingles, neither of which is a fault.
VALUE_BOUNDS: dict[str, Range] = {
    "lsh_threshold": Range(0.0, THRESHOLD_MAX),
    "num_permutations": Range(2.0, float(MAX_NUM_PERM), high_inclusive=True),
    "top_n": Range(1.0),
    "ngram_size": Range(1.0),
    "jaccard_weight": Range(0.0, 1.0, high_inclusive=True),
}


def validate_value(key: str, value: object) -> str | None:
    """Return why *value* is unusable as a setting for *key*, or None if fine.

    The one place a configuration value is checked: ``resembl config set``
    refuses a value that returns a message, and :meth:`ResemblConfig.update`
    warns and keeps the current value when a hand-edited config file holds
    one.  Both entry points share this, so the file and the CLI can never
    disagree about what a legal value is.

    *value* must already be coerced to the field's type (see
    :meth:`ResemblConfig.update`); the enum and range rules are what this adds.
    """
    if key == "format":
        if value not in FORMATS:
            return f"expected one of: {', '.join(FORMATS)}"
        return None
    bounds = VALUE_BOUNDS.get(key)
    if bounds is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return f"expected a finite {type(DEFAULTS[key]).__name__}"
    if numeric < bounds.low:
        return f"expected a value of at least {bounds.low:g}"
    if bounds.high_inclusive and numeric <= bounds.high:
        return None
    if not bounds.high_inclusive and numeric < bounds.high:
        return None
    end = "at most" if bounds.high_inclusive else "below"
    return f"expected a value {end} {bounds.high:g}"


@dataclasses.dataclass
class ResemblConfig:
    """Typed configuration for resembl with defaults.

    Callers read attributes directly; :func:`update_config` and
    :func:`remove_config_key` return plain dicts for the values they write.
    """

    lsh_threshold: float = 0.5
    num_permutations: int = 128
    top_n: int = 5
    ngram_size: int = 3
    jaccard_weight: float = 0.4
    format: str = "table"

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

        Values of the right type but outside the range the code works in
        (``ngram_size = 0``, ``lsh_threshold = 5.0``, ``top_n = -1``) are
        rejected the same way, by :func:`validate_value`: unchecked, the
        reindex they drive produces degenerate fingerprints (every snippet
        matches every other) or the index build and every ``find`` fail at
        use time, long after the value was written.  ``resembl config set``
        runs the same check, so the two entry points cannot disagree.
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
            problem = validate_value(key, value)
            if problem is not None:
                logger.warning("Ignoring %s: %s, got %r.", key, problem, value)
                continue
            setattr(self, key, value)

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
        except BaseException:
            # The half-written temp file must not outlive a failed save, and
            # a Ctrl-C lands here as often as a serialization error: the
            # ``with`` closes the handle, then the name goes.
            tmp.close()
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
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


def _read_config_file() -> tuple[dict, Exception | None]:
    """Return the raw config file as a dict, plus the read error if any.

    The one reader behind every config access: a missing, malformed, or
    unreadable file yields an empty config rather than an exception, so a
    broken file never takes down ``load_config`` or a read-modify-write
    update.  The error travels with the data so :func:`load_config` can
    report it instead of silently running on defaults.
    """
    cfg_path = config_path_get()
    if not os.path.exists(cfg_path):
        return {}, None
    try:
        with open(cfg_path, "rb") as f:
            return tomllib.load(f), None
    except (tomllib.TOMLDecodeError, OSError) as e:
        return {}, e


def _read_config_dict() -> dict:
    """Read the raw config file as a dict (empty when missing or malformed)."""
    return _read_config_file()[0]


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
    cfg = ResemblConfig()
    user_config, error = _read_config_file()
    if error is not None:
        # Malformed TOML or an unreadable file: report it and run on defaults
        # instead of crashing every command.
        logger.error("Error reading config file at %s: %s", config_path_get(), error)
        return cfg

    cfg.update(user_config)
    return cfg
