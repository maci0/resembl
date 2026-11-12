"""Clock and retry-wait helpers for resembl.

Every ``created_at`` stamp and every duration/retry decision in the
package reads the same pinned clock, so it lives here rather than in
:mod:`resembl.models`.  ``models`` needs ``timestamp_now`` only as the
``default_factory`` of two ``created_at`` columns; ``config``, ``cache``
and ``core`` need the retry wait and the monotonic reading without
pulling the ORM tables into their import graph.  Standard library only.

``RESEMBL_NOW`` pins every stamp and duration to a fixed instant, given
as an ISO 8601 string. A value without an offset is read as UTC — never
as the host's local zone — so a pinned replay means the same instant
wherever it runs.  The wall clock is otherwise the only source of
``created_at`` values, and those values are written to the database,
printed back by ``collection list`` and ``version``, and embedded in
backup filenames and manifests, so two runs of the same inputs never
produce the same rows or the same output.  Setting this variable is
what makes a recorded run replay byte-for-byte.
"""

from __future__ import annotations

import math
import os
import time
from datetime import UTC, datetime

#: Environment variable that pins :func:`timestamp_now` to a fixed instant.
CLOCK_ENV_VAR = "RESEMBL_NOW"


def parse_instant(value: str) -> datetime | None:
    """Parse *value* as a full ISO 8601 instant, or return ``None``.

    ``datetime.fromisoformat`` also accepts date-only (``2024-06-01``),
    week-date (``2024-W01-1``), basic-format (``20240601T100000``) and
    minute-precision (``...T10:00``) forms.  None of those names an instant
    with an explicit time, so a stored ``created_at`` in one of them would
    silently become midnight UTC on normalize, corrupting the string
    ordering ``SnippetVersion.get_by_checksum`` relies on.  Only the
    extended ``YYYY-MM-DDTHH:MM:SS[.ffffff][offset]`` shape counts here;
    anything else is unparseable for our purposes and left verbatim.
    """
    text = value.strip()
    # ASCII-only gate: the shape pins digit positions, and an instant's
    # fields are ASCII digits. A multi-byte character in the first 19
    # characters shifts every later code-point offset, and a digit spelled
    # in another script (Arabic-Indic, fullwidth) must not pass the gate
    # for ``fromisoformat`` to decide: the Go mirror indexes bytes, so
    # agreeing on ASCII here is what keeps the two validators in step.
    if not text.isascii():
        return None
    if len(text) < 19 or text[4] != "-" or text[7] != "-" or text[10] not in ("T", "t", " "):
        return None
    if text[13] != ":" or text[16] != ":":
        return None
    # ``fromisoformat`` accepts a trailing ``Z`` but not the lowercase
    # ``z`` that ISO 8601 also allows; a foreign ``...z`` value would
    # otherwise pass the gate above, fail to parse, and be left verbatim
    # by ``timestamp_normalize`` -- breaking the string ordering
    # ``SnippetVersion.get_by_checksum`` relies on (``z`` sorts after
    # every ``+00:00`` value, so a newer row could sort oldest).
    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"
    # A lowercase ``t`` separator is valid ISO 8601 and parsed above,
    # but the canonical stored form uses uppercase ``T``.
    if text[10] == "t":
        text = text[:10] + "T" + text[11:]
    # ``-00:00`` (in any spelling) is ISO 8601's "unknown local offset",
    # not UTC: ``fromisoformat`` maps it to UTC, so normalizing it would
    # fabricate a UTC instant the row never stated -- the same corruption
    # the date-only gate above refuses.  Leave it verbatim instead.
    if text.endswith(("-00:00", "-0000", "-00", "-00:00:00")):
        return None
    try:
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is not None:
            moment.astimezone(UTC)
        return moment
    except ValueError, OverflowError:
        return None


def timestamp_now() -> str:
    """Return the current instant as a canonical UTC ISO 8601 string.

    Read from ``CLOCK_ENV_VAR`` when that variable is set, so a test or a
    replay gets the same stamp every time; an unparseable value raises
    ``ValueError`` rather than silently falling back to the wall clock, which
    would defeat the point of setting it.  A naive pin is read as UTC, not
    the host's local zone, so the same pin means the same instant on every
    machine.
    """
    override = os.environ.get(CLOCK_ENV_VAR)
    if override is None or not override.strip():
        return datetime.now(UTC).isoformat()
    parsed = parse_instant(override)
    if parsed is None:
        raise ValueError(f"{CLOCK_ENV_VAR}={override!r} is not an ISO 8601 instant")
    moment = parsed
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def monotonic_now() -> float:
    """Return a monotonic clock reading, pinned to 0 when ``CLOCK_ENV_VAR`` is set.

    The wall-clock seam in :func:`timestamp_now` covers ``created_at`` stamps,
    but every duration metric (``time_elapsed`` in core, ``latency_ms`` and
    ``uptime_seconds`` in the server) reads ``time.monotonic()`` directly.
    Two seeded runs therefore diff on those fields even when every stamp
    matches.  When the clock is pinned, durations are pinned too: every
    reading is 0, so every elapsed value is exactly 0.0.  Monotonic callers
    only take differences, so a constant clock is indistinguishable from a
    frozen one and needs no offset bookkeeping.
    """
    override = os.environ.get(CLOCK_ENV_VAR)
    if override is not None and override.strip():
        # Fail fast like timestamp_now(): an unparseable pin must not
        # silently pin every duration to 0.0 on read paths that never
        # stamp a row, where the typo would otherwise go unnoticed.
        if parse_instant(override) is None:
            raise ValueError(f"{CLOCK_ENV_VAR}={override!r} is not an ISO 8601 instant")
        return 0.0
    return time.monotonic()


def _retry_sleep(delay: float) -> None:
    """Sleep *delay* seconds unless the clock is pinned.

    ``RESEMBL_NOW`` pins every timestamp and duration to a fixed instant;
    a real ``time.sleep`` on a retry path would make a pinned replay wait
    on wall time while every other duration reads 0.0.  Skipping the
    wait under a pin keeps the retry count (the only observable) identical
    and the run instant, matching :func:`monotonic_now`.
    """
    override = os.environ.get(CLOCK_ENV_VAR)
    if override is not None and override.strip():
        # Fail fast like timestamp_now()/monotonic_now(): an unparseable pin
        # must not silently skip the wait on read/retry paths where no row
        # is stamped, where the typo would otherwise go unnoticed.
        if parse_instant(override) is None:
            raise ValueError(f"{CLOCK_ENV_VAR}={override!r} is not an ISO 8601 instant")
        return
    time.sleep(delay)


def avg_time_per_item(time_elapsed: float, item_count: int) -> float:
    """Return the exact per-item seconds for *item_count* processed items.

    *time_elapsed* is in seconds; *item_count* is a 1-based count, so 0
    (nothing processed) yields 0.0 rather than dividing by zero.
    A negative count is a caller bug and also yields 0.0; a non-finite
    elapsed time is rejected rather than propagated, since NaN/Inf in the
    elapsed reading would silently poison every downstream total.
    """
    if item_count <= 0 or not math.isfinite(time_elapsed):
        return 0.0
    return float(time_elapsed) / item_count


def timestamp_normalize(value: str | None) -> str | None:
    """Normalize a ``created_at`` string to the canonical stored form.

    Timestamps are stored as aware-UTC ISO 8601 strings, exactly as written
    by :func:`timestamp_now`.  Rows are ordered by string comparison
    (e.g. ``SnippetVersion.get_by_checksum``), which matches chronological
    order only while every value carries the same offset, so timestamps
    imported from foreign databases must be re-expressed in UTC before being
    persisted.  Naive values are interpreted as UTC (the historical writer's
    zone).  A NULL column and unparseable text are returned unchanged:
    a merge never rewrites metadata it cannot read.
    """
    if not isinstance(value, str):
        return value
    moment = parse_instant(value)
    if moment is None:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()
