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

from .clock import CLOCK_ENV_VAR, _retry_sleep, monotonic_now
from .paths import ascii_fold_lower, config_dir_get, config_path_get
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

#: ``ngram_size`` is accepted at most this bound: shingle construction
#: allocates one slice per window position, so an unbounded value turns a
#: large snippet into quadratic memory. Real configurations use 2-5, and
#: the largest exercised value is 7; 32 is generous headroom that keeps
#: the allocation linear.
MAX_NGRAM_SIZE = 32

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
#:     snippet matches every other one without any error being raised, and
#:     above the cap the shingle allocation turns quadratic. Real
#:     configurations use 2-5; :data:`MAX_NGRAM_SIZE` is generous headroom.
#: ``jaccard_weight``
#:     A 0-1 balance between the LSH and Jaccard scores, per ``score_hybrid``;
#:     both ends are usable (1.0 is a pure Jaccard ranking).
#: ``top_n``
#:     At least one result; 0 (or a negative value) truncates every ranking to
#:     nothing, which reads as "no matches" rather than as a bad setting.
#: No upper bound is imposed on ``top_n``: a large value only returns more
#: rows, which is not a fault.
VALUE_BOUNDS: dict[str, Range] = {
    "lsh_threshold": Range(0.0, THRESHOLD_MAX),
    "num_permutations": Range(2.0, float(MAX_NUM_PERM), high_inclusive=True),
    "top_n": Range(1.0),
    "ngram_size": Range(1.0, float(MAX_NGRAM_SIZE), high_inclusive=True),
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
    if key not in DEFAULTS:
        return f"unknown configuration key: {key!r}"
    default = DEFAULTS[key]
    if isinstance(value, bool) or type(value) is not type(default):
        return f"expected a {type(default).__name__}"
    if not isinstance(value, (int, float)):
        return None
    bounds = VALUE_BOUNDS.get(key)
    if bounds is None:
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
                logger.warning("Ignoring unknown configuration key: %s.", key)
                continue
            default = DEFAULTS[key]
            if isinstance(value, bool):
                logger.warning(
                    "Ignoring %s: expected %s, got %r.",
                    key,
                    type(default).__name__,
                    value,
                )
                continue
            if isinstance(value, str):
                # ASCII-only fold: ``casefold`` maps the long s to s, so a
                # value spelled "true" with one must not read as True here
                # while the thin client's check treats it as a string.
                text = ascii_fold_lower(value.strip())
                if text in ("true", "false"):
                    logger.warning(
                        "Ignoring %s: expected %s, got %r.",
                        key,
                        type(default).__name__,
                        value,
                    )
                    continue
            # int() truncates (2.9 -> 2), silently accepting a fractional
            # count the file never asked for.  Reject fractional floats for
            # int fields before coercion; integral floats (128.0) stay legal.
            is_int_field = isinstance(default, int) and not isinstance(default, bool)
            if is_int_field and isinstance(value, float):
                if not value.is_integer():
                    logger.warning(
                        "Ignoring %s: expected %s, got %r.",
                        key,
                        type(default).__name__,
                        value,
                    )
                    continue
                value = int(value)
            try:
                value = type(default)(value)
            except TypeError, ValueError, OverflowError:
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


class ConfigError(OSError):  # pylint: disable=too-few-public-methods
    """A config file exists but cannot be read or parsed.

    Subclasses ``OSError`` so existing ``except OSError`` handlers in the
    CLI surface it as a write/read failure; the message names the file and
    the parse/IO failure so a malformed file is never silently discarded
    by a read-modify-write update.
    """


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
    fd = os.open(config_path_get() + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl

            # Bounded wait: an unbounded flock in a CLI update wedges the
            # process forever if a crashed holder left the file open or a
            # peer holds it indefinitely.  Poll with a deadline matching
            # the sibling msvcrt poll cadence (0.05s) so both platforms
            # fail with a timeout instead of hanging.  The clock is
            # ``monotonic_now`` (pinned to 0 under ``RESEMBL_NOW``) and
            # the wait is ``_retry_sleep`` (no-op under a pin), matching
            # every other duration/retry path: a pinned replay must not
            # read wall time or sleep on this lock, or two seeded runs
            # diverge on timeout vs success.  Under a pin the monotonic
            # reading never advances, so a started+30 deadline would
            # never fire and a contended pinned holder would spin
            # forever; under a pin the deadline is the start itself, so
            # the first contention times out deterministically.
            # Unpinned, the 30s wall budget is unchanged.
            deadline = monotonic_now() + (
                0.0 if os.environ.get(CLOCK_ENV_VAR, "").strip() else 30.0
            )
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if monotonic_now() >= deadline:
                        raise TimeoutError("timed out waiting for config file lock") from None
                    _retry_sleep(0.05)
        except ImportError:
            try:
                import msvcrt

                # msvcrt.locking locks nbytes at the current file position,
                # so seek to the start to pin the region every holder locks.
                # A fresh sidecar is 0 bytes and Windows refuses to lock a
                # byte that does not exist, so ensure one byte is present.
                os.lseek(fd, 0, os.SEEK_SET)
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                # LK_LOCK retries ~10 times then raises OSError, while
                # flock waits indefinitely; a config update that loses to a
                # slow holder must wait a bounded time, not crash or hang,
                # so poll LK_NBLCK with the same 30s deadline as flock,
                # on the same pinned clock as above (immediate timeout
                # under a pin, for the same reason).
                deadline = monotonic_now() + (
                    0.0 if os.environ.get(CLOCK_ENV_VAR, "").strip() else 30.0
                )
                while True:
                    try:
                        # typeshed provides these attributes for Windows only.
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                        break
                    except OSError:
                        if monotonic_now() >= deadline:
                            raise TimeoutError("timed out waiting for config file lock") from None
                        _retry_sleep(0.05)
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
    # 0o600: the config directory may be on a shared host; other users must
    # not read the file (it can carry tuning that reveals internal
    # thresholds, and a future field could hold a credential).
    with tempfile.NamedTemporaryFile("wb", dir=cfg_dir, delete=False) as tmp:
        tmp_path = tmp.name
        try:
            tomli_w.dump(data, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.chmod(tmp_path, 0o600)
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
        # fsync the directory so the rename itself survives an OS crash
        try:
            dir_fd = os.open(cfg_dir, os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            except OSError as e:
                logger.warning("Could not fsync config directory %s: %s", cfg_dir, e)
            finally:
                os.close(dir_fd)
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
    broken file never takes down ``load_config``.  The error travels with
    the data so :func:`load_config` can report it instead of silently
    running on defaults.  Read-modify-write callers go through
    :func:`_read_config_dict`, which raises instead of overwriting a
    broken file.
    """
    cfg_path = config_path_get()
    if not os.path.exists(cfg_path):
        return {}, None
    try:
        with open(cfg_path, "rb") as f:
            return tomllib.load(f), None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, RecursionError, OSError) as e:
        return {}, e


def _read_config_dict() -> dict:
    """Read the raw config file as a dict (empty only when missing).

    A present but unreadable/malformed file raises :class:`ConfigError`
    instead of returning ``{}``: a read-modify-write caller that proceeds
    with an empty dict would overwrite the file and silently discard every
    existing override.  Callers that must tolerate a broken file use
    :func:`_read_config_file` / :func:`load_config`, which surface the
    error instead of overwriting it.
    """
    config, error = _read_config_file()
    if error is not None:
        raise ConfigError(f"cannot read config file {config_path_get()}: {error}") from error
    return config


def update_config(key: str, value: int | float | str) -> dict:
    """Update ``key`` in the config file with ``value`` and return the new config.

    Raises :class:`ConfigError` (an ``OSError``) when the existing file
    cannot be read or parsed, so the update fails closed instead of
    overwriting and discarding the file's previous contents.  Raises
    :class:`ValueError` for an unknown key or a value outside the range
    ``validate_value`` allows, so programmatic callers cannot persist a
    misconfiguration the CLI would refuse.
    """
    if key not in DEFAULTS:
        raise ValueError(f"unknown configuration key: {key!r}")
    problem = validate_value(key, value)
    if problem is not None:
        raise ValueError(f"invalid value for {key!r}: {problem}, got {value!r}")
    with _config_file_lock():
        config = _read_config_dict()
        config[key] = value
        # The file stores user overrides only (like ``remove_config_key``):
        # baking every default into the file would pin users to this release's
        # default values forever.  Callers get the effective merged view.
        save_config(config)
    return {**DEFAULTS, **config}


def remove_config_key(key: str) -> dict:
    """Remove ``key`` from the config file and return the new config.

    Raises :class:`ConfigError` (an ``OSError``) when the existing file
    cannot be read or parsed, so the removal fails closed instead of
    overwriting and discarding the file's previous contents.  Raises
    :class:`ValueError` for an unknown key.
    """
    if key not in DEFAULTS:
        raise ValueError(f"unknown configuration key: {key!r}")
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
