"""Command-line interface for the resembl assembly similarity tool.

This module wires together the ``core``, ``config``, ``database``, and
``models`` modules into a user-facing CLI built with Typer.  The
``--quiet``, ``--no-color``, and ``--format`` global options are read
from ``state``; a command that never reads ``state.format`` ignores
``--format``.

Key design choices
------------------
* **Checksum prefix resolution** - Any command that accepts a checksum
  also accepts a unique prefix, resolved via ``_resolve_checksum``.
* **Structured output** - The commands that report data render
  ``--format json`` and ``--format csv`` in addition to the default
  Rich table output.  The commands that only acknowledge a mutation
  print a human line in every format.
* **Quiet mode** - ``_echo`` is used instead of ``console.print`` so
  that ``--quiet`` suppresses informational output.
"""

from __future__ import annotations

import atexit
import contextvars
import csv
import difflib
import json
import logging
import multiprocessing
import operator
import os
import signal
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from datetime import UTC, tzinfo
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
from rich.console import Console
from rich.markup import escape as _markup_escape
from rich.panel import Panel
from rich.progress import track
from rich.table import Table
from rich.theme import Theme
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from .clock import CLOCK_ENV_VAR, avg_time_per_item, monotonic_now, parse_instant
from .config import (
    DEFAULTS,
    FORMATS,
    ConfigError,
    ResemblConfig,
    _read_config_dict,
    config_path_get,
    load_config,
    remove_config_key,
    update_config,
    validate_value,
)
from .core import (
    IndexBuildError,
    adaptive_worker_count,
    collection_add_snippet,
    collection_create,
    collection_delete,
    collection_list,
    collection_remove_snippet,
    db_backup,
    db_backup_for_delete,
    db_clean,
    db_merge,
    db_reindex,
    db_stats,
    db_verify,
    db_verify_backup,
    snippet_add,
    snippet_add_batch,
    snippet_collection_names_stream,
    snippet_compare,
    snippet_delete,
    snippet_export,
    snippet_export_yara,
    snippet_find_matches,
    snippet_get,
    snippet_list,
    snippet_matches_payload,
    snippet_name_add,
    snippet_name_remove,
    snippet_names_stream,
    snippet_prepare,
    snippet_search_by_name,
    snippet_tag_add,
    snippet_tag_remove,
    snippet_version_list,
)
from .database import db_create, get_engine
from .find_client import _read_bounded_response as _read_bounded_server_response
from .lsh import banding_params, lsh_meta_get, lsh_meta_matches
from .paths import (
    _ASCII_LOWER_TABLE,
    LOG_LEVEL_ENV_VAR,
    cache_dir_get,
    console_utf8_reconfigure,
    db_url_get,
    db_url_mask,
    log_level_get,
    port_file_cleanup,
    serve_allow_unauthenticated_get,
    server_port_path,
)
from .scoring import MAX_NUM_PERM
from .theme import ACCENT, RICH_STYLES, score_color

if TYPE_CHECKING:
    from .models import Snippet

logger = logging.getLogger(__name__)

# --- Rich Consoles ---


def _legacy_windows_for(stream: object) -> bool | None:
    """Return the ``legacy_windows`` setting for a Console over *stream*.

    rich renders a legacy Windows console through the Win32 API, and only
    when the Console's file is a standard stream (fd 1 or 2).  A *redirected*
    stdout is one, so rich wrote the styles to the console's screen buffer and
    the pipe received none of them: ``FORCE_COLOR=1`` could not put color into
    a CI log, a file, or a test's captured output.  The Win32 path is for the
    console, so auto detection (``None``) is kept only when the stream is one.
    """
    isatty = getattr(stream, "isatty", None)
    if isatty is None or isatty():
        return None
    return False


# highlighting=False keeps rich's stock highlighter from tinting quoted text
# inside messages: a color in resembl's output is always one this module put
# there (see ``theme``), never one the library guessed.
console = Console(
    theme=Theme(RICH_STYLES), highlight=False, legacy_windows=_legacy_windows_for(sys.stdout)
)
err_console = Console(
    theme=Theme(RICH_STYLES),
    stderr=True,
    highlight=False,
    legacy_windows=_legacy_windows_for(sys.stderr),
)

_OUTPUT_CHOICES = frozenset({"auto", "text", "json"})
_output_choice: contextvars.ContextVar[str] = contextvars.ContextVar(
    "resembl_output", default="auto"
)
_pending_error: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "resembl_pending_error", default=None
)


def _write_error(kind: str, message: str, hint: str) -> None:
    """Write one clispec error envelope as the next stderr line."""
    payload = {"error": {"kind": kind, "message": message, "hint": hint}}
    sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stderr.flush()


def _bind_err_console(target: Console) -> Console:
    """Keep a rich error line in text mode, and defer it in JSON mode.

    JSON mode stashes the message. ``typer.Exit`` then writes the envelope
    with the kind that matches the exit code, so the line is not emitted
    before the code is known.
    """
    original = target.print

    def _print(*args: object, **kwargs: Any) -> None:
        if (
            args
            and isinstance(args[0], str)
            and args[0].startswith("[red]Error:[/red] ")
            and _output_choice.get() == "json"
        ):
            _pending_error.set(args[0].removeprefix("[red]Error:[/red] "))
            return
        original(*args, **kwargs)

    target.print = _print  # type: ignore[method-assign]
    return target


# Mutable, not a constant: pylint's const-rgx would demand UPPER_CASE for the literal.
_hooks_installed = False  # pylint: disable=invalid-name


def _install_exit_hook() -> None:
    """When JSON mode stashed an error, write it as ``typer.Exit`` is built."""
    original = typer.Exit.__init__

    def _init(self: typer.Exit, code: int = 0) -> None:
        original(self, code)
        message = _pending_error.get()
        if message and code and _output_choice.get() == "json":
            kind = "usage" if code == 2 else "failure"
            _pending_error.set(None)
            _write_error(kind, message, "")

    typer.Exit.__init__ = _init  # type: ignore[method-assign]


def _install_click_error_hook() -> None:
    """Usage errors in JSON mode are the stderr envelope, not a rich panel."""
    from typer._click.exceptions import ClickException
    from typer._click.exceptions import UsageError as ClickUsageError
    from typer.rich_utils import rich_format_error

    original_show = ClickException.show
    original_rich = rich_format_error

    def _show(self: ClickException, file: Any = None) -> None:
        if _output_choice.get() == "json":
            kind = "usage" if isinstance(self, ClickUsageError) else "failure"
            _write_error(kind, self.format_message(), "")
            return
        original_show(self, file=file)

    def _rich_format_error(self: ClickException) -> None:
        if _output_choice.get() == "json":
            kind = "usage" if isinstance(self, ClickUsageError) else "failure"
            _write_error(kind, self.format_message(), "")
            return
        original_rich(self)

    ClickException.show = _show  # type: ignore[method-assign]
    # Typer's main() calls rich_utils.rich_format_error directly when rich
    # markup is enabled (the default here), bypassing ClickException.show.
    # Patch the function in its module so that path also emits the envelope.
    import typer.rich_utils

    typer.rich_utils.rich_format_error = _rich_format_error


def _install_hooks() -> None:
    """Install the JSON error hooks once per process."""
    global _hooks_installed
    if _hooks_installed:
        return
    _install_exit_hook()
    _install_click_error_hook()
    _hooks_installed = True


err_console = _bind_err_console(err_console)
_install_hooks()


def _accept_output(value: str) -> str:
    """Record ``-o`` before the subcommand runs."""
    if value not in _OUTPUT_CHOICES:
        raise typer.BadParameter("must be one of auto, text, json")
    _output_choice.set(value)
    return value


# --- Typer apps ---

_HELP_OPTIONS = {"help_option_names": ["-h", "--help"]}

app = typer.Typer(
    help="A CLI for finding similar assembly code snippets.",
    add_completion=False,
    rich_markup_mode="rich",
    context_settings=_HELP_OPTIONS,
)
config_app = typer.Typer(
    help="Manage user configuration.", rich_markup_mode="rich", context_settings=_HELP_OPTIONS
)
name_app = typer.Typer(
    help="Manage snippet names.", rich_markup_mode="rich", context_settings=_HELP_OPTIONS
)
tag_app = typer.Typer(
    help="Manage snippet tags.", rich_markup_mode="rich", context_settings=_HELP_OPTIONS
)
collection_app = typer.Typer(
    help="Manage snippet collections.", rich_markup_mode="rich", context_settings=_HELP_OPTIONS
)
app.add_typer(config_app, name="config")
app.add_typer(name_app, name="name")
app.add_typer(tag_app, name="tag")
app.add_typer(collection_app, name="collection")


# --- State ---


class State:
    """Shared state for all commands."""

    session: Session
    quiet: bool = False
    format: str = "table"
    tz: tzinfo | None = None

    def __init__(self) -> None:
        # Config defaults until the CLI callback loads the user's file;
        # helpers that read ``config`` (e.g. ``_find_via_server``) then
        # also work when called directly without the callback having run.
        self.config = ResemblConfig()


state = State()

#: The session the main callback opened, tracked so it can be released.
#: Registering ``state.session.close`` itself as the exit hook left one
#: atexit entry per CLI invocation, and every entry holds a strong reference
#: to its own session: a process that embeds the CLI (a test harness driving
#: ``CliRunner``, a long-lived wrapper script) grew an unbounded registry and
#: kept every one of those sessions alive, each holding a checked-out
#: connection, so repeated invocations drained the engine's pool.  A
#: module-level function is a single stable callable, so it registers once
#: for the process and covers every session it is asked to close.
#: Mutable lazy singleton, not a constant: pylint's const-rgx would have it
#: UPPER_CASE because the initializer is a literal.
_active_session: Session | None = None  # pylint: disable=invalid-name


def _close_active_session() -> None:
    """Close the session the main callback opened, if one is open."""
    global _active_session
    if _active_session is not None:
        _active_session.close()
        _active_session = None


atexit.register(_close_active_session)

#: Next step shown whenever a listing comes back empty.  An empty result
#: printed nothing at all, or a bare "No snippets found.", which reads as a
#: finished answer: the user has no way to tell an empty database from a
#: wrong query, and no command to try next.  Kept to one terminal line.
_NO_SNIPPETS_HINT = "Add one with 'resembl add <name> <code>'."


def _package_version() -> str:
    """Return the installed distribution version, or ``unknown``."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("resembl")
    except PackageNotFoundError:  # running from a source tree without metadata
        return "unknown"


def _echo(message: object, **kwargs: Any) -> None:
    """Print a message unless ``--quiet`` is set."""
    if not state.quiet:
        console.print(message, **kwargs)


#: Code points dropped from text before it is shown on a terminal: C0 controls
#: except tab and newline (so a multi-line query still reads), carriage return
#: (it overwrites the line so far, which spoofs what was printed before it),
#: DEL, and the C1 range.  ESC goes with them, which is what stops a stored
#: name from smuggling ANSI sequences (clear screen, write a fake prompt, set
#: the window title) into the viewer's terminal.
_DISPLAY_STRIP = dict.fromkeys(
    [*range(0x09), 0x0B, 0x0C, 0x0D, *range(0x0E, 0x20), 0x7F, *range(0x80, 0xA0)],
    None,
)


def _strip_controls(text: str) -> str:
    """Return *text* with terminal control characters removed.

    Names and other database text are untrusted on the way out as well as in:
    a shared or merged database can carry a name with raw ESC bytes that would
    rewrite the viewer's screen.  Tab and newline survive (they are how
    multi-line queries render); everything else below a space, carriage
    return, DEL and the C1 range go.
    """
    return text.translate(_DISPLAY_STRIP)


def _display(text: str) -> str:
    """Return *text* safe to embed in a rich-markup string.

    Drops terminal controls (see :func:`_strip_controls`) and escapes rich's
    bracket markup, so a stored name like ``[bold]`` reaches the terminal as
    literal text instead of restyling the output it ships in.
    """
    return _markup_escape(_strip_controls(text))


def _zone_get(name: str | None) -> tzinfo | None:
    """Return the IANA zone *name* resolves to, or ``None`` for the local zone.

    ``None`` keeps :func:`_format_created_at` on the host's zone.  A fixed
    offset (``+02:00``) is refused rather than accepted: it names one instant
    of the year, so every date after a DST transition would render at the
    wrong wall clock.  Raises :class:`ValueError` naming the input.
    """
    if name is None:
        return None
    if name.startswith(("+", "-", "UTC+", "UTC-")) or not name[:1].isalpha():
        raise ValueError(
            f"{name!r} is a fixed offset, not a zone name; pass an IANA zone such as "
            "'Europe/Warsaw', whose offset follows daylight saving."
        )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{name!r} is not a known IANA time zone") from exc


def _format_created_at(value: object, fmt: str, tz: tzinfo | None = None) -> str:
    """Render a stored ``created_at`` string in the viewer's local zone.

    Stored values are aware-UTC ISO 8601 strings (see ``Collection.created_at``);
    they are converted to *tz* (the ``--tz`` zone, else the system's local
    zone) before formatting so table output matches the viewer's wall clock.
    Naive legacy values are interpreted as UTC; unparseable values are shown
    verbatim, and a NULL column (a database written without one) has no instant
    to show and renders as an empty cell.  Structured (JSON/CSV) output keeps
    the raw stored string.
    """
    if not isinstance(value, str):
        return ""
    # Only a full instant renders converted; a date-only or week-date
    # string is not an instant, so converting it would fabricate a
    # midnight-UTC moment and show the wrong calendar day in most zones.
    # Show such values verbatim, exactly like unparseable ones.
    moment = parse_instant(value)
    if moment is None:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz).strftime(fmt)


#: Leading characters spreadsheet applications interpret as formulas when a
#: CSV cell is opened (= starts a formula, +/-/@ insert one, tab/CR continue
#: one).  Snippet names and tags are arbitrary text from merge sources, so
#: exported cells must be neutralized before they reach such an application.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

#: Record terminator every CSV writer uses.  ``csv`` defaults to ``"\r\n"``,
#: but the writers here target ``sys.stdout``, which is a text stream with
#: ``newline=None`` and therefore rewrites every ``"\n"`` to ``os.linesep``.
#: On Windows that turns the default terminator into ``"\r\r\n"``, and a
#: spreadsheet or ``csv.reader`` reads the stray ``\r`` as a field.  ``"\n"``
#: is what every other renderer here emits and is byte-identical on all
#: platforms.
_CSV_LINETERMINATOR = "\n"


def _csv_safe(value: object) -> object:
    """Return *value* made safe for one CSV cell.

    A string whose first character would make a spreadsheet application
    evaluate the cell as a formula gets a text-forcing apostrophe prefix
    (the standard mitigation; the apostrophe is not part of the value's
    meaning).  Non-strings and safe strings pass through unchanged.
    Whitespace and invisible characters before the trigger are stripped
    for the check, because spreadsheets ignore them when parsing a
    formula, so ``" =1+1"`` is as live as ``"=1+1"``.  The two classes
    are stripped in a loop, as ``name_normalize`` does: they can sit on
    either side of each other (``"\ufeff =1+1"``), so a single
    ``lstrip().lstrip(BOM)`` leaves the space that followed the BOM and
    the formula evades the check.
    """
    if not isinstance(value, str):
        return value
    if value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    stripped = value
    while True:
        # Stripped invisible set: the BOM and the zero-width space/joiner
        # pair is not the whole class.  U+2060 (WORD JOINER), U+00AD (SOFT
        # HYPHEN, rendered invisible at a non-break), U+180E (MONGOLIAN VOWEL
        # SEPARATOR, formerly a space) and the other format characters a
        # spreadsheet skips when it lands on a formula trigger all sit in
        # front of "=1+1" exactly like U+200B does, so leaving one of them
        # out of the lstrip set leaves the same evasion open through a
        # different code point.
        next_stripped = stripped.strip().lstrip(
            "\ufeff\u200b\u200c\u200d\u2060\u00ad\u180e\u061c\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        )
        if next_stripped == stripped:
            break
        stripped = next_stripped
    if stripped.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def _csv_safe_tree(value: object) -> object:
    """Return *value* with every string inside it made safe for a CSV cell.

    :func:`_csv_safe` only sees a cell that is already a string, so the
    guard is reapplied down through the nested lists and dicts a per-query
    payload carries (find-batch's ``matches``), where an untrusted name
    would otherwise reach a spreadsheet as a formula.
    """
    if isinstance(value, dict):
        return {k: _csv_safe_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_csv_safe_tree(v) for v in value]
    return _csv_safe(value)


def _echo_format(data: object) -> None:
    """Print data in the requested format (JSON/CSV) unless ``--quiet``."""
    if state.quiet:
        return
    if state.format == "csv":
        if isinstance(data, dict) and "matches" in data:
            data = data["matches"]
        if isinstance(data, list) and not data:
            # An empty result set has no columns to name: CSV's "no rows" is
            # no output, not a JSON array smuggled into a CSV stream.
            return
        # One writer for both shapes: a list of row dicts keeps its keys, a
        # single dict becomes one row.  ``fieldnames`` is collected from the
        # rows rather than read off the first one, because rows may carry
        # heterogeneous keys (e.g. one failed query in a server-side
        # find-batch gains an "error" column) and DictWriter raises on a row
        # the first-row-only header does not name.
        rows: list[dict]
        fieldnames: list[str]
        if isinstance(data, list) and isinstance(data[0], dict):
            for row in data:
                if isinstance(row.get("names"), list):
                    row["names"] = ", ".join(row["names"])
                # A per-query payload (find-batch) keeps its nested match
                # list in one cell.  ``csv`` would stringify that with
                # ``str()``, writing Python's single-quoted repr into the
                # cell; JSON is the same value the json format emits and
                # both are read back.  The strings inside it still go
                # through the formula guard, so a name smuggled into a
                # nested payload is neutralized like a top-level one.
                for key, value in row.items():
                    if key != "names" and isinstance(value, (list, dict)):
                        row[key] = json.dumps(_csv_safe_tree(value))
            rows = data
            fieldnames = list(dict.fromkeys(k for row in rows for k in row))
        elif isinstance(data, dict):
            rows = [{k: ", ".join(v) if isinstance(v, list) else v for k, v in data.items()}]
            fieldnames = list(rows[0])
        else:
            console.print(json.dumps(data, indent=2), markup=False)
            return
        writer = csv.DictWriter(
            sys.stdout, fieldnames=fieldnames, lineterminator=_CSV_LINETERMINATOR
        )
        writer.writeheader()
        writer.writerows({k: _csv_safe(v) for k, v in row.items()} for row in rows)
    else:
        # Reached only for "json": every caller guards on
        # `format in ("json", "csv")`. A list is a collection document.
        sys.stdout.write(_json_text(data) + "\n")


def _echo_kv_table(title: str, rows: list[tuple[str, str]]) -> None:
    """Render ``(key, value)`` rows as the shared two-column result table.

    The summary tables of the report commands (add/import/export/stats/...)
    differ only in title and rows; routing them through here keeps the
    geometry identical by construction.
    """
    table = Table(title=title, show_header=False, title_style=f"bold {ACCENT}")
    table.add_column("Key", style="dim")
    table.add_column("Value")
    for key, value in rows:
        table.add_row(key, value)
    _echo(table)


def _snippet_table(rows: Iterable[tuple[str, Sequence[str]]], title: str, start: int = 1) -> Table:
    """Render ``(checksum, names)`` rows as the shared three-column listing.

    The ``list`` and ``search`` tables (and each streamed ``list`` batch)
    differ only in their row source and title; routing them through here
    keeps the geometry identical by construction.
    """
    table = Table(title=title, title_style=f"bold {ACCENT}")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Checksum", style="bold")
    table.add_column("Names")
    for i, (checksum, names) in enumerate(rows, start):
        table.add_row(str(i), checksum[:12] + "…", ", ".join(_display(n) for n in names))
    return table


def _json_text(data: object) -> str:
    """Serialize one JSON document. A list becomes ``items`` and ``total``."""
    if isinstance(data, list):
        data = {"items": data, "total": len(data)}
    if sys.stdout.isatty():
        return json.dumps(data, indent=2)
    return json.dumps(data, separators=(",", ":"))


def _write_json_stream(items: Iterator[dict]) -> None:
    """Write *items* as one ``{"items", "total"}`` document.

    Shared by ``list`` and ``collection show``. Rows are written as they
    arrive so the whole set is not built in memory first. ``total`` is the
    count of rows actually written.
    """
    sys.stdout.write('{"items":[')
    total = 0
    for item in items:
        if total:
            sys.stdout.write(",")
        total += 1
        sys.stdout.write(json.dumps(item, separators=(",", ":")))
    sys.stdout.write(f'],"total":{total}}}\n')


def _build_progress_printer() -> Callable[[int, int], None]:
    """Return a progress callback for long index builds (table mode).

    Prints one line per 100k snippets processed so a multi-minute build on a
    large database reports progress instead of appearing hung.  Returns a
    no-op when the dataset is too small for that granularity to matter.
    """

    last_marker = 0

    def _report(done: int, total: int) -> None:
        nonlocal last_marker
        if total >= 100_000 and done - last_marker >= 100_000:
            last_marker = done
            _echo(f"[dim]  indexed {done:,}/{total:,} snippets…[/dim]")

    return _report


def _session_db_url(session: Session) -> str:
    """Return the session bind's URL exactly as clients address it.

    ``str(URL)`` masks the password (``user:***@host``), so hashing that
    string for the port-file name diverges from the raw ``DATABASE_URL``
    the standalone ``resembl-find`` client hashes: with credentials in the
    URL, the server would advertise under one digest while every thin
    client looked under another, and warm finds would silently fall back to
    cold process startups. SQLAlchemy can also URL-encode Windows paths when
    rendering them. Preserve the configured spelling when it names this bind;
    render an externally supplied session's URL unmasked otherwise.
    """
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    url = cast(Engine, session.get_bind()).url
    configured = db_url_get()
    try:
        if make_url(configured) == url:
            return configured
    except ArgumentError:
        # An externally supplied session need not use the environment's URL.
        pass
    return url.render_as_string(hide_password=False)


#: Hard ceiling on a warm-server response the CLI will buffer.  Mirrors
#: ``resembl.find_client._MAX_RESPONSE_BYTES``: a port-file swap or a
#: compromised loopback server must not pin unbounded memory via
#: ``response.read()`` with no size.  Kept as the name callers use; the
#: reader is shared so the two copies cannot drift.
_MAX_SERVER_RESPONSE_BYTES = 16 * 1024 * 1024


def _server_request(path: str, body: dict, timeout: float) -> dict | None:
    """Send one JSON request to the running ``serve`` process.

    Returns the decoded payload, or ``None`` when no server is reachable
    (the caller falls back to the in-process path).  A timeout means the
    server is alive but slow (e.g. busy serving a large find-batch) — its
    port file is kept; only a dead server's stale file is removed, since
    deleting it would orphan a healthy server.
    """
    import urllib.error
    import urllib.request

    db_url = _session_db_url(state.session)
    port_file = server_port_path(db_url, cache_dir_get())
    try:
        with open(port_file, encoding="utf-8") as f:
            port = int(f.read().strip())
    except (OSError, ValueError):
        return None
    # Same ephemeral-range guard as the standalone client (find_client.py):
    # a planted port file must not steer the query to a privileged local
    # service (22, 3306, 6379) — treat it as no server and fall back.
    if not 1024 <= port <= 65535:
        return None

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        # Loopback must not go through a proxy (http_proxy): the server is
        # on this host, and a proxied request would either fail or send the
        # corpus query to a third party.  ProxyHandler({}) disables proxies.
        # urlopen()'s context is an SSLContext, not an opener: passing an
        # opener there only wrapped it as an HTTPSHandler context, so the
        # default opener's env ProxyHandler still proxied the http request.
        # OpenerDirector.open() is the opener-aware path.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(_read_bounded_server_response(response).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx answer is a decision, not an outage: the server is
        # healthy and deliberately refused this request.  Fall back to the
        # in-process path, which validates the same parameters and reports
        # the same errors on the caller's terms.  Log the refusal: falling
        # back silently hides a server-side 500 from the operator.
        logger.warning(
            "Server request to %s failed with HTTP %s; falling back to the in-process path.",
            path,
            getattr(exc, "code", "?"),
        )
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Only a verifiably dead server (connection refused: nothing is
        # listening on the advertised port) loses its port file.  Resets,
        # read timeouts, and malformed responses all occur against a
        # *healthy* server under load (connection churn surfaces as RST);
        # removing the advertisement there would orphan the warm process
        # for every other find client until it restarts.
        logger.debug(
            "Server request to %s failed (%s); falling back to the in-process path.",
            path,
            exc,
        )
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, ConnectionRefusedError):
            # Retire the stale advertisement only while it still names the
            # dead *port* this request failed on; between reading the file
            # and the refused connect, another ``serve`` may have bound and
            # rewritten it.  Same rule as the server's own exit path.
            port_file_cleanup(port_file, port)
        return None
    if "error" in payload:
        # A 200 carrying an error envelope is a deliberate server refusal
        # (e.g. the index needs a rebuild), not a result.  Falling back to
        # the in-process path keeps the caller moving, but the refusal
        # itself is the only trace of the server-side fault: log it like
        # the HTTPError branch above, or a server that only ever answers
        # with envelopes fails silently from the operator's log.
        logger.warning(
            "Server request to %s was refused (%s); falling back to the in-process path.",
            path,
            payload.get("error"),
        )
        return None
    return payload


def _find_body_params(
    top_n: int,
    threshold: float | None,
    normalize: bool,
    ngram_size: int,
) -> dict:
    """Return the find parameters shared by the ``/find`` and ``/find-batch`` bodies.

    ``num_permutations`` and ``jaccard_weight`` are sent explicitly (like the
    thin client does) so a server started under a different config still
    answers exactly what the in-process path would have.
    """
    return {
        "top_n": top_n,
        "threshold": threshold,
        "normalize": normalize,
        "ngram_size": ngram_size,
        "num_permutations": state.config.num_permutations,
        "jaccard_weight": state.config.jaccard_weight,
    }


def _find_via_server(
    query: str,
    top_n: int,
    threshold: float | None,
    normalize: bool,
    ngram_size: int,
) -> dict | None:
    """Query a running ``serve`` process for the current database."""
    return _server_request(
        "/find",
        {
            "query": query,
            **_find_body_params(top_n, threshold, normalize, ngram_size),
        },
        timeout=5,
    )


def _find_batch_via_server(
    queries: list[str],
    top_n: int,
    threshold: float | None,
    normalize: bool,
    ngram_size: int,
) -> list[dict] | None:
    """Query a running ``serve`` process with a batch (one round trip per chunk).

    The endpoint caps ``queries`` per request (see
    ``resembl.server.MAX_BATCH_QUERIES``), so a longer file is sent in
    chunks; results keep the input order.
    """
    from .server import MAX_BATCH_QUERIES

    params = _find_body_params(top_n, threshold, normalize, ngram_size)
    results: list[dict] = []
    for start in range(0, len(queries), MAX_BATCH_QUERIES):
        chunk = queries[start : start + MAX_BATCH_QUERIES]
        payload = _server_request("/find-batch", {"queries": chunk, **params}, timeout=60)
        if payload is None:
            return None
        results.extend(payload["results"])
    return results


def _query_inline_statements(query_string: str) -> str:
    """Treat ``;`` as a statement separator in single-line query strings.

    Inline ``--query`` strings use ';' as a statement separator (the
    documented convenience format): splitting on ';' stops the lexer from
    silently treating the rest of the query as a comment.  Multi-line
    input keeps normal NASM semantics, where ';' starts a comment.
    """
    if ";" in query_string and "\n" not in query_string:
        return query_string.replace(";", "\n")
    return query_string


def _render_find_payload(payload: dict) -> None:
    """Render a ``{lsh_candidates, matches}`` payload in the current format."""
    if state.format in ("json", "csv"):
        _echo_format(payload)
        return
    _echo(f"[dim]Found {payload['lsh_candidates']} candidates via LSH.[/dim]")
    matches = payload["matches"]
    if matches:
        table = Table(title="Top Matches", title_style=f"bold {ACCENT}")
        table.add_column("#", style="dim", justify="right")
        table.add_column("Checksum", style="bold")
        table.add_column("Names")
        table.add_column("Score (Hybrid)", justify="right")
        for i, match in enumerate(matches, 1):
            score = match["score"]
            color = score_color(score)
            table.add_row(
                str(i),
                match["checksum"][:12] + "…",
                ", ".join(_display(n) for n in match["names"]),
                f"[{color}]{score:.2f}[/{color}]",
            )
        _echo(table)
    else:
        _echo("[yellow]No matches found after ranking.[/yellow]")


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Interface to bind (default: loopback only)."
    ),
    port: int = typer.Option(0, "--port", help="Port to bind (0 = auto-assign)."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Log every served request (DEBUG)."
    ),
    stateless: bool = typer.Option(
        False,
        "--stateless",
        help=(
            "Open no database: serve only /score, /band-keys, /scheme, /health and "
            "/metrics, for replicas that share no state. Find paths answer 501."
        ),
    ),
    allow_unauthenticated: bool = typer.Option(
        False,
        "--allow-unauthenticated",
        help=(
            "Allow binding a non-loopback interface without authentication. "
            "The server has no auth: anyone who can reach the port can read "
            "the corpus. Only use behind a network gate that provides auth."
        ),
    ),
) -> None:
    """Serve find queries from a warm process (instant warm finds).

    Start this once per database; ``resembl find`` then talks to it over
    localhost instead of paying ~450 ms of interpreter startup per query.
    The port is written to the cache directory and removed on exit.
    With ``--stateless`` no database is opened and no port file is written.
    """
    if not 0 <= port <= 65535:
        err_console.print(f"[red]Error:[/red] --port must be between 0 and 65535 (got {port}).")
        raise typer.Exit(code=USAGE_ERROR)
    from .server import serve as serve_start

    # ``-v`` is also a global (callback-level) option, which click only
    # accepts before the subcommand name.  serve has no other work to do in
    # the foreground, and the per-request DEBUG log is the only trail a
    # served query leaves, so the flag is repeated here and it is the one
    # that reaches it.
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    db_url: str | None = None
    if not stateless:
        _db_open()
        db_url = _session_db_url(state.session)

    def _is_loopback_bind(h: str) -> bool:
        """Whether *h* names a loopback interface (same test as server.py).

        ``host not in ("127.0.0.1", "localhost", "::1")`` alone let the
        whole 127.0.0.0/8 block (127.0.0.2, 127.1.2.3, ...) through as
        "non-loopback" and demanded --allow-unauthenticated for a bind
        that only local users can reach — but, more importantly, any
        future edit that inverted that list would open 127/8 without
        the warning.  ``ipaddress`` is the authority, matching
        ``resembl.server._loopback_allowed_hosts``.
        """
        if h.casefold() == "localhost":
            return True
        try:
            import ipaddress

            return ipaddress.ip_address(h).is_loopback
        except ValueError:
            return False

    if not _is_loopback_bind(host):
        if not allow_unauthenticated and not serve_allow_unauthenticated_get():
            err_console.print(
                "[red]Error:[/red] refusing to bind non-loopback "
                f"{_display(host)} without --allow-unauthenticated: the "
                "server has no authentication and anyone who can reach the "
                "port can read the corpus. Bind loopback, or put an "
                "authenticating gate in front of the port."
            )
            raise typer.Exit(code=1)
        _echo(
            "[yellow]Warning: binding a non-loopback interface exposes an "
            "unauthenticated find service.[/yellow]"
        )
    if not stateless:
        _echo("[dim]Warming up index (first serve can take a moment)…[/dim]")
    try:
        httpd = serve_start(db_url, host=host, port=port)
    except (ValueError, IndexBuildError) as e:
        # ValueError: another serve process already owns this database;
        # IndexBuildError: the warm-up migration/index build failed.
        err_console.print(f"[red]Error:[/red] {_display(str(e))}")
        raise typer.Exit(code=1) from e
    except OverflowError:
        # A port outside 0-65535 reaches the socket layer as OverflowError
        # ("bind(): port must be 0-65535"), not OSError.  The range check
        # above rejects it first; keep this as a safety net so a future
        # caller that bypasses it still gets a clean usage error.
        err_console.print(f"[red]Error:[/red] --port must be between 0 and 65535 (got {port}).")
        raise typer.Exit(code=USAGE_ERROR) from None
    except OSError as e:
        # e.g. --port already in use by another process.
        err_console.print(
            f"[red]Error:[/red] could not bind {_display(host)}:{port}: "
            f"{_display(str(e.strerror or e))}"
        )
        raise typer.Exit(code=1) from e
    _echo(f"[dim]resembl server listening on {_display(host)}:{httpd.server_address[1]}[/dim]")

    # Service managers (systemd, Docker stop, kill) send SIGTERM, whose
    # default disposition kills the process without running the atexit
    # cleanup — leaving a stale port file behind.  Turn SIGTERM into a
    # KeyboardInterrupt so the normal shutdown path runs (server close +
    # port-file removal); the thin client tolerates a stale file, but a
    # clean lifecycle keeps the cache dir tidy across restarts.
    def _handle_sigterm(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_sigterm)
    # Windows has no SIGTERM.  What a service manager sends there is
    # CTRL_BREAK_EVENT, which arrives as SIGBREAK, and its default disposition
    # ends the process without running the shutdown above — the same stale port
    # file this handler exists to prevent.  SIGBREAK is the Windows spelling of
    # the same request, so it takes the same handler.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_sigterm)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


#: Exit code for a rejected command line: a bad flag value, a malformed
#: argument, an unusable setting.  Matches what click/typer already return for
#: their own parse errors, so a script can tell "you typed it wrong" (2) from
#: "the command failed" (1).
USAGE_ERROR = 2


def _fail(
    message: str,
    code: int = 1,
    *,
    kind: str | None = None,
    hint: str = "",
) -> None:
    """Print one error and exit. ``-o json`` writes the stderr envelope."""
    resolved = kind or ("usage" if code == USAGE_ERROR else "failure")
    _pending_error.set(None)
    if _output_choice.get() == "json":
        _write_error(resolved, message, hint)
    else:
        err_console.print(f"[red]Error:[/red] {_display(message)}")
    raise typer.Exit(code=code)


def _emit_kept_error(message: str) -> None:
    """Report one error and keep going. JSON mode writes the envelope now."""
    if _output_choice.get() == "json":
        _write_error("failure", message, "")
        return
    err_console.print(f"[red]Error:[/red] {_display(message)}")


def _confirm(prompt: str, force: bool) -> None:
    """Confirm on a terminal. Off a terminal, require ``--force``."""
    if force:
        return
    if not sys.stdin.isatty():
        _fail(
            "This command needs a terminal to confirm, or --force to proceed.",
            kind="confirmation_required",
            hint="Pass --force to proceed without a prompt.",
        )
    typer.confirm(_strip_controls(prompt), abort=True)


def _backup_before_delete(backup_dir: str, no_backup: bool, *, label: str = "Pre-delete") -> None:
    """Take the verified pre-delete backup unless it was skipped.

    Reports the failure and exits, so callers must not proceed when
    the backup cannot be written or verified.
    """
    if no_backup:
        return
    try:
        db_backup_for_delete(state.session, backup_dir)
    except (ValueError, OSError) as e:
        err_console.print(f"[red]Error:[/red] {label} backup failed: {_display(str(e))}")
        raise typer.Exit(code=1) from e


def _validate_find_params(
    threshold: float, num_perm: int, ngram_size: int, top_n: int | None = None
) -> None:
    """Exit with a clean error if *threshold* / *num_perm* / *ngram* are unusable.

    The banding needs b >= 2 bands; at 128 permutations that caps the
    buildable threshold near 0.98 (0.981 gives b=1).  Without this check,
    an unbuildable threshold made the index build fail and ``find`` return
    zero results silently.

    The permutation count must be MinHash-buildable (>= 2) and bounded
    like the server's request validation: unvalidated, a bad config value
    reached :func:`snippet_find_matches`, which runs the full auto-reindex
    side effect first and only then crashes inside fingerprint construction
    with a raw ValueError.  An ``ngram_size`` below 1 does not crash but
    silently produces degenerate fingerprints (every shingle is empty), so
    every snippet would match every other one.
    """
    if not 0.0 <= threshold < 0.99:
        err_console.print("[red]Error:[/red] --threshold must be between 0.0 and 0.99 (exclusive).")
        raise typer.Exit(code=USAGE_ERROR)
    if not 2 <= num_perm <= MAX_NUM_PERM:
        err_console.print(
            f"[red]Error:[/red] num_permutations must be between 2 and {MAX_NUM_PERM} "
            f"(got {num_perm})."
        )
        raise typer.Exit(code=USAGE_ERROR)
    if ngram_size < 1:
        err_console.print(f"[red]Error:[/red] ngram_size must be at least 1 (got {ngram_size}).")
        raise typer.Exit(code=USAGE_ERROR)
    # A --top-n override bypasses ``config validate_value``, which is where
    # top_n is otherwise kept at 1 or above.  A zero or negative top_n
    # truncates the ranking to nothing and reads as a clean "no matches"
    # with a full candidate count next to it, which is indistinguishable
    # from a real miss.
    if top_n is not None and top_n < 1:
        err_console.print(f"[red]Error:[/red] --top-n must be at least 1 (got {top_n}).")
        raise typer.Exit(code=USAGE_ERROR)
    try:
        bands, _ = banding_params(threshold, num_perm)
    except ValueError:
        bands = 1
    if bands < 2:
        err_console.print(
            "[red]Error:[/red] --threshold is too high for "
            f"{num_perm} permutations (it would leave fewer than 2 bands).  "
            "Lower the threshold or raise num_permutations."
        )
        raise typer.Exit(code=USAGE_ERROR)


def _resolve_checksum(prefix: str) -> str | None:
    """Resolve a checksum prefix to a full checksum.

    If *prefix* matches exactly one snippet, return its full checksum.
    If it matches zero or more than one, print an error and return ``None``.
    """
    from sqlmodel import select  # Local import — only needed for prefix LIKE query

    from .models import Snippet as SnippetModel

    exact = SnippetModel.get_by_checksum(state.session, prefix)
    if exact:
        return exact.checksum

    # Prefix search.  Only the checksum column is fetched, and the fetch is
    # capped at two rows: a short prefix can match thousands of checksums,
    # and the resolver only distinguishes zero, one, and "many".  The
    # ambiguous path runs one indexed COUNT so the error message reports the
    # real number without pulling every match through the ORM (a 1-char
    # prefix used to materialize the whole matching set in memory just to
    # print its size).
    from sqlmodel import func

    # Checksums are hex, so a legitimate prefix never contains SQL LIKE
    # metacharacters — escape them so garbage input ("%", "_") is matched
    # literally and reports "no snippet found" instead of silently resolving
    # to (and e.g. deleting) an arbitrary wildcard-matched snippet.
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like_expr = SnippetModel.checksum.like(f"{escaped}%", escape="\\")  # type: ignore[attr-defined]
    candidate_checksums: Sequence[str] = state.session.exec(
        select(SnippetModel.checksum).where(like_expr).limit(2)
    ).all()

    if len(candidate_checksums) == 0:
        err_console.print(f"[red]Error:[/red] No snippet found matching '{_display(prefix)}'.")
        return None
    if len(candidate_checksums) > 1:
        total = state.session.exec(
            select(func.count(SnippetModel.checksum)).where(like_expr)  # type: ignore[arg-type]
        ).one()
        err_console.print(
            f"[red]Error:[/red] Ambiguous prefix '{_display(prefix)}' matches {total} snippets."
        )
        return None
    return candidate_checksums[0]


def _apply_snippet_field_mutation(
    mutate: Callable[[], Snippet | None],
    *,
    get_values: Callable[[Snippet], list[str]],
    plural: str,
    failure_message: str,
) -> None:
    """Apply one name/tag mutation and render its outcome.

    The four ``name add/remove`` / ``tag add/remove`` sub-commands differ
    only in the core mutator and the rendered field; routing them through
    here keeps their JSON/CSV payload, quiet suppression, and exit codes
    identical by construction.  A failure exits 1 regardless of ``--quiet``
    or ``--format``: quiet suppresses output, never the failure signal.
    """
    snippet = mutate()
    if snippet is None:
        if state.format in ("json", "csv"):
            _fail(failure_message)
        elif not state.quiet:
            err_console.print(f"[red]Error:[/red] {_display(failure_message)}")
        raise typer.Exit(code=1)
    values = get_values(snippet)
    if state.format in ("json", "csv"):
        _echo_format({"checksum": snippet.checksum, plural: values})
    else:
        _echo(
            f"[green]✓[/green] Snippet [bold]{snippet.checksum[:12]}…[/bold] "
            f"now has {plural}: {', '.join(_display(v) for v in values)}"
        )


@app.callback(invoke_without_command=True)
def app_callback(
    ctx: typer.Context,
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress informational output."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Increase output verbosity."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colored output."),
    format_opt: str | None = typer.Option(
        None,
        "--format",
        metavar="{" + "|".join(FORMATS) + "}",
        help="Output format: " + ", ".join(FORMATS) + ". Overrides config.",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        is_eager=True,
        help="Show the resembl version and exit.",
    ),
    tz_name: str | None = typer.Option(
        None,
        "--tz",
        metavar="ZONE",
        help=(
            "IANA zone for printed timestamps (e.g. Europe/Warsaw). Default: the host's local zone."
        ),
    ),
    output_format: str = typer.Option(
        "auto",
        "--output",
        "-o",
        callback=_accept_output,
        is_eager=True,
        help="Output mode before the subcommand: auto, text, or json. auto stays text.",
    ),
) -> None:
    """Set up logging and shared state.

    ``resembl schema`` prints the clispec 0.3 document and does not open a database.
    """
    global console, err_console

    if version:
        # Eager: answer before the database is opened, so `resembl --version`
        # works against an unreachable or missing DATABASE_URL.
        console.print(f"resembl {_package_version()}")
        raise typer.Exit(code=0)

    # schema is a document about this CLI. It must not read config or open a database.
    if ctx.invoked_subcommand == "schema":
        return

    if ctx.invoked_subcommand is None:
        # Bare `resembl` is a request for the command list, not a command;
        # print it the way `--help` does and keep the nonzero exit a missing
        # subcommand has always had. It is a usage error, so the help goes
        # to stderr like every other usage error (and like the bare group
        # commands), keeping stdout clean for scripts.
        if _output_choice.get() == "json":
            _fail(
                "No command given.",
                code=2,
                kind="usage",
                hint="Run 'resembl --help' for the command list.",
            )
        # Typer's rich help prints straight to stdout and get_help()
        # returns "", so render plain to get a string for stderr.
        command = ctx.command
        original_mode = getattr(command, "rich_markup_mode", None)
        if original_mode is not None:
            command.rich_markup_mode = None  # type: ignore[attr-defined]
        try:
            help_text = ctx.get_help()
        finally:
            if original_mode is not None:
                command.rich_markup_mode = original_mode  # type: ignore[attr-defined]
        err_console.print(help_text)
        raise typer.Exit(code=2)

    state.quiet = quiet

    if no_color:
        console = Console(
            no_color=True, highlight=False, legacy_windows=_legacy_windows_for(sys.stdout)
        )
        err_console = _bind_err_console(
            Console(
                stderr=True,
                no_color=True,
                highlight=False,
                legacy_windows=_legacy_windows_for(sys.stderr),
            )
        )

    log_level = logging.INFO
    env_level = log_level_get()
    if env_level is not None:
        if env_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            err_console.print(
                f"[red]Error:[/red] {LOG_LEVEL_ENV_VAR} must be one of DEBUG, INFO, "
                f"WARNING, ERROR, CRITICAL, got '{env_level}'."
            )
            raise typer.Exit(code=2)
        log_level = getattr(logging, env_level)
    # Fail fast on an invalid pinned clock / seed, the same validation
    # RESEMBL_LOG_LEVEL gets above: today an invalid RESEMBL_NOW (or
    # RESEMBL_SEED) only raises when a write/stats path happens to stamp
    # or sample, so `resembl list` / `resembl --version`-adjacent
    # commands exit 0 with the typo silently ignored while `resembl
    # stats` on a non-empty store and `serve` crash mid-run with a
    # ValueError.  Validate once here so every command refuses a bad
    # pin/seed at startup with a clear message, instead of depending on
    # which code path happens to read it.
    clock_override = os.environ.get(CLOCK_ENV_VAR)
    if clock_override is not None and clock_override.strip():
        if parse_instant(clock_override) is None:
            err_console.print(
                f"[red]Error:[/red] {CLOCK_ENV_VAR}={clock_override!r} is not an "
                "ISO 8601 instant."
            )
            raise typer.Exit(code=2)
    from .diagnostics import SEED_ENV_VAR

    seed_override = os.environ.get(SEED_ENV_VAR)
    if seed_override is not None and seed_override.strip():
        try:
            int(seed_override.strip(), 10)
        except ValueError:
            err_console.print(
                f"[red]Error:[/red] {SEED_ENV_VAR}={seed_override!r} is not an integer."
            )
            raise typer.Exit(code=2) from None
    if quiet:
        log_level = logging.WARNING
    elif verbose:
        log_level = logging.DEBUG
    # stderr, not stdout: a warning raised mid-run (a config key ignored, a
    # parallel import falling back) would otherwise be interleaved into the
    # JSON/CSV array a script is parsing.
    logging.basicConfig(
        level=log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    try:
        _read_config_dict()
    except ConfigError as e:
        # A present but malformed/unreadable config.toml must fail fast at
        # startup, the way `serve` already does: the CLI answers every
        # find/stats/list from this config, so a silently discarded tuned
        # threshold (top_n=999 + a typo line) would run the whole command
        # on defaults with exit 0 and an ERROR log line interleaved in
        # stderr, while `config set` on the same file exits 1.  load_config
        # keeps its report-and-default contract for library callers; the
        # CLI boundary is where the fail-fast belongs.
        err_console.print(f"[red]Error:[/red] {_display(str(e))}")
        raise typer.Exit(code=USAGE_ERROR) from e
    state.config = load_config()
    if format_opt is not None:
        state.format = format_opt
    elif output_format == "json":
        state.format = "json"
    elif output_format == "text":
        state.format = "table"
    else:
        state.format = state.config.format
    if state.format not in FORMATS:
        # An unknown format is a usage error, not a silent fallback: the
        # renderers branch on this value, so an unrecognized one used to
        # print a table the caller had asked not to get, with exit 0.
        err_console.print(
            f"[red]Error:[/red] --format must be one of {', '.join(FORMATS)}, "
            f"got '{_display(state.format)}'."
        )
        raise typer.Exit(code=2)
    if state.format == "json" and output_format != "text":
        # `--format json` (or a config default of json) is structured
        # output too: its failures carry the same stderr envelope `-o json`
        # uses, so a script parsing stdout as JSON can parse stderr the
        # same way. Checked after the format is validated. An explicit
        # `-o text` wins over both, exactly as it wins over `--json` in
        # find_client: the caller asked for text errors alongside a JSON
        # data document.
        _output_choice.set("json")
    try:
        state.tz = _zone_get(tz_name)
    except ValueError as e:
        err_console.print(f"[red]Error:[/red] {_display(str(e))}.")
        raise typer.Exit(code=2) from e
    # serve opens the database itself, and not at all under --stateless.
    if ctx.invoked_subcommand != "serve":
        _db_open()


def _db_open() -> None:
    """Create the configured database's tables and bind ``state.session`` to it.

    Exits 1 with the URL (password masked) when the database cannot be opened.
    """
    global _active_session
    try:
        db_create()
    except SQLAlchemyError as e:
        # The URL may carry credentials (DATABASE_URL=...://user:pass@host/db):
        # mask them before the message reaches the terminal.  The URL comes
        # from the same resolver the engine used, so the message names the
        # database that actually failed.
        err_console.print(
            f"[red]Error:[/red] cannot open the database "
            f"({_display(db_url_mask(db_url_get()))}): {_display(str(e))}"
        )
        raise typer.Exit(code=1) from e
    _close_active_session()
    state.session = Session(get_engine())
    _active_session = state.session


# --- Snippet commands ---


@app.command("schema")
def schema_cmd(
    command: list[str] | None = typer.Argument(
        None, help="Command path to include. Other commands are omitted."
    ),
) -> None:
    """Print the clispec 0.3 JSON document for this CLI."""
    from .clispec import build_schema

    path = " ".join(command or [])
    document = build_schema(path or None)
    if path and not document["commands"]:
        _fail(f"no command matches {path!r}", code=USAGE_ERROR, kind="usage")
    sys.stdout.write(json.dumps(document, separators=(",", ":")) + "\n")


@app.command()
def add(
    name: str = typer.Argument(help="The name or alias for the snippet."),
    code: str = typer.Argument(help="The assembly code of the snippet."),
) -> None:
    """Add a new snippet or an alias to existing code.

    Examples:
      resembl add "memcpy" "mov ecx, ebx"
      resembl add "memcpy" "mov ecx, ebx; add eax, 1"
    """
    snippet = snippet_add(
        state.session,
        name,
        code,
        ngram_size=state.config.ngram_size,
        num_perm=state.config.num_permutations,
    )
    if snippet:
        if state.format in ("json", "csv"):
            _echo_format({"checksum": snippet.checksum, "names": snippet.name_list})
        else:
            _echo(
                f"[green]✓[/green] Snippet [bold]{snippet.checksum[:12]}…[/bold] "
                f"now has names: {', '.join(_display(n) for n in snippet.name_list)}"
            )
    else:
        if state.format in ("json", "csv"):
            _fail("Failed to add snippet.")
        else:
            err_console.print("[red]Error:[/red] Snippet could not be added (empty code?).")
        raise typer.Exit(code=1)


@app.command("export")
def export_cmd(
    directory: str = typer.Argument(help="The directory to export snippets to."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Export all snippets to a directory."""
    _confirm(f"Are you sure you want to export all snippets to '{directory}'?", force)

    try:
        result = snippet_export(state.session, directory)
    except OSError as e:
        err_console.print(f"[red]Error:[/red] Export failed: {_display(str(e))}")
        raise typer.Exit(code=1) from e

    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        rows = [
            ("Snippets exported", str(result["num_exported"])),
            ("Time elapsed", f"{result['time_elapsed']:.4f}s"),
        ]
        if result["num_exported"] > 0:
            rows.append(("Avg per snippet", f"{result['avg_time_per_snippet'] * 1000:.4f}ms"))
        if result.get("num_removed"):
            # A re-run over a directory an earlier export wrote drops the files
            # of snippets that have since been renamed or deleted; say so,
            # because the deletion is the only destructive part of the command.
            rows.append(("Stale files removed", str(result["num_removed"])))
        _echo_kv_table("Export Complete", rows)


@app.command("export-yara")
def export_yara_cmd(
    output_file: str = typer.Argument(help="The output file to save YARA rules to."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Export snippets as YARA string patterns."""
    _confirm(f"Are you sure you want to export YARA rules to '{output_file}'?", force)

    try:
        result = snippet_export_yara(state.session, output_file)
    except OSError as e:
        err_console.print(f"[red]Error:[/red] YARA export failed: {_display(str(e))}")
        raise typer.Exit(code=1) from e

    if state.format in ("json", "csv"):
        _echo_format(result)
        return

    rows = [
        ("Rules exported", str(result["num_exported"])),
        ("Time elapsed", f"{result['time_elapsed']:.4f}s"),
    ]
    if result["num_exported"] > 0:
        rows.append(("Avg per rule", f"{result['avg_time_per_snippet'] * 1000:.4f}ms"))
    _echo_kv_table("YARA Export Complete", rows)


def _import_prepare_file(args: tuple[str, int, int]) -> tuple[str, str, str, bytes] | None:
    """Worker: read a file and compute its checksum + MinHash fingerprint.

    Top-level and pure (no database access) so it can run in a process pool.
    Returns a ``snippet_prepare`` tuple, or ``None`` if the file is empty
    or unreadable (a failure here must never silently drop rows — see
    ``import_cmd``, which counts rejects).
    """
    file_path, ngram_size, num_perm = args
    try:
        with open(file_path, encoding="utf-8") as f:
            code = f.read()
    except (OSError, UnicodeDecodeError):
        # Unreadable or non-UTF-8 files are rejected, never fatal.
        return None
    name = os.path.splitext(os.path.basename(file_path))[0]
    return snippet_prepare(name, code, ngram_size, num_perm)


@app.command("import")
def import_cmd(
    path: str = typer.Argument(
        help="A .asm or .txt file, or a directory containing them (searched recursively)."
    ),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
    jobs: int | None = typer.Option(
        None,
        "--jobs",
        "-j",
        help="Number of worker processes (default: adaptive — one worker "
        "per ~100 files, capped at the CPU count).",
    ),
) -> None:
    """Bulk import snippets from a file or directory.

    Takes a single .asm/.txt file as readily as a directory tree: pointing
    the command at one file used to be answered with "Directory not found",
    which names the wrong kind of object as the problem.
    """
    file_paths: list[str]
    if os.path.isfile(path):
        file_paths = [path]
    elif os.path.isdir(path):
        # Case-insensitive extension match so a directory imports identically
        # on every platform: glob patterns are folded by os.path.normcase on
        # Windows but matched verbatim on Linux/macOS, so "*.asm" silently
        # skipped FOO.ASM there.
        #
        # The list is sorted because ``os.walk`` yields directory entries in
        # readdir order, which is not stable across runs of the same tree.  Rows
        # are written in this order, so an unsorted list made the resulting
        # database's row order (and every later score tie, which is broken by
        # insertion order) depend on the filesystem.
        file_paths = []
        for root, _dirs, files in os.walk(path):
            file_paths.extend(
                os.path.join(root, fname)
                for fname in files
                # ASCII-fold only: ``casefold`` maps the long s (U+017F) to
                # s, so ``FOO.A<long-s>M`` would pass as ``.asm`` and be imported
                # under an extension no other tool recognizes.
                if fname.translate(_ASCII_LOWER_TABLE).endswith((".asm", ".txt"))
            )
        file_paths.sort()
    else:
        err_console.print(f"[red]Error:[/red] Directory not found: {_display(path)}")
        raise typer.Exit(code=1)

    if not file_paths:
        # Reported before the confirmation prompt and before the run: a
        # directory of the wrong files used to answer "Snippets imported: 0"
        # and exit 0, which reads as a successful import of a directory the
        # user meant to load.  The same error the missing-directory case
        # gives, with the reason spelled out.
        err_console.print(
            f"[red]Error:[/red] No .asm or .txt files found in {_display(path)} "
            "(searched recursively, case-insensitive)."
        )
        raise typer.Exit(code=1)

    if not force:
        target = "snippet" if len(file_paths) == 1 else "all snippets"
        _confirm(f"Are you sure you want to import {target} from '{path}'?", force)

    start_time = monotonic_now()
    ngram_size = state.config.ngram_size
    num_perm = state.config.num_permutations

    if jobs is None:
        jobs = adaptive_worker_count(len(file_paths), os.cpu_count() or 1)
    jobs = min(jobs, max(1, len(file_paths)))

    # Prepared results are flushed to the database in chunks, so importing a
    # very large directory never accumulates the whole dataset in memory.
    import_chunk_size = 10_000
    added_total = 0
    aliased_total = 0
    prepared: list[tuple[str, str, str, bytes]] = []

    def flush_prepared() -> None:
        nonlocal prepared, added_total, aliased_total
        if not prepared:
            return
        batch_result = snippet_add_batch(
            state.session, prepared, ngram_size=ngram_size, num_perm=num_perm
        )
        added_total += batch_result["added"]
        aliased_total += batch_result["aliased"]
        prepared = []
        # Drop the committed Snippet objects from the identity map.  Without
        # this, a multi-million-snippet import accumulates every row in memory
        # (≈2.8 GB per million snippets); the objects are fully persisted, so
        # detaching them is safe.
        state.session.expunge_all()

    # Phase 1 — parallel preparation (CPU-bound lexing + MinHash).  A process
    # pool is used because the work is pure Python and GIL-bound; a thread pool
    # would serialize it.  Spawn (rather than fork) so workers never inherit
    # the parent's SQLite connection.
    rejects = 0

    def handle_prepared(result: tuple[str, str, str, bytes] | None) -> None:
        nonlocal rejects
        if result is None:
            rejects += 1
        else:
            prepared.append(result)
            if len(prepared) >= import_chunk_size:
                flush_prepared()

    def prepare_sequential() -> None:
        for fp in file_paths:
            handle_prepared(_import_prepare_file((fp, ngram_size, num_perm)))

    if file_paths and jobs > 0:
        ctx = multiprocessing.get_context("spawn")
        try:
            from collections import deque

            with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as executor:
                # Bound in-flight futures: submitting every file at once
                # keeps O(n) futures + results in memory (hundreds of MB for
                # a million files).  A small window keeps memory flat while
                # the workers stay saturated.
                file_iter = iter(file_paths)
                window = max(jobs * 4, 16)
                in_flight: deque[Future[tuple[str, str, str, bytes] | None]] = deque()

                def _submit_next() -> bool:
                    fp = next(file_iter, None)
                    if fp is None:
                        return False
                    in_flight.append(
                        executor.submit(_import_prepare_file, (fp, ngram_size, num_perm))
                    )
                    return True

                for _ in range(min(window, len(file_paths))):
                    _submit_next()

                def _completed_futures() -> Iterator[Future[tuple[str, str, str, bytes] | None]]:
                    # Results are consumed in submission order, never in the
                    # order the workers happen to finish: rows are written in
                    # this order, so consuming as-completed would make the
                    # row order (and with it the outcome of every later
                    # score tie) depend on worker scheduling.  A replay of
                    # the same file list then reproduces the same database.
                    while in_flight:
                        done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                        finished = set(done)
                        completed = [future for future in in_flight if future in finished]
                        still_running = [future for future in in_flight if future not in finished]
                        in_flight.clear()
                        in_flight.extend(still_running)
                        for future in completed:
                            yield future
                            _submit_next()

                if state.quiet or state.format in ("json", "csv"):
                    for future in _completed_futures():
                        handle_prepared(future.result())
                else:
                    for future in track(
                        _completed_futures(),
                        total=len(file_paths),
                        description="Preparing snippets...",
                        console=err_console,
                    ):
                        handle_prepared(future.result())
        except Exception as exc:
            # The pool may be unavailable (e.g. spawned from a stdin script)
            # or a worker/flush may have failed mid-run; either way the
            # original failure must reach the log, not vanish behind a
            # generic fallback message.
            logger.warning(
                "Parallel import preparation failed (%s); falling back to in-process preparation.",
                exc,
                exc_info=True,
            )
            # A failed flush aborts the session's transaction; roll it back
            # or every write of the sequential redo fails with
            # PendingRollbackError instead of redoing the work.
            state.session.rollback()
            # Redo every file sequentially, exactly as the pool run would
            # have (inserts dedupe on checksum): reset the flushed counters
            # too, or already-flushed chunks are counted twice.
            added_total = 0
            aliased_total = 0
            rejects = 0
            prepared = []
            prepare_sequential()
    elif file_paths:
        # jobs <= 0: no worker processes — import sequentially in-process,
        # matching `reindex --jobs 0` (marking every file as skipped here
        # would silently import nothing).
        prepare_sequential()

    # Phase 2 — flush any remaining prepared snippets (batched single-session
    # write; dedupes by checksum and merges alias names).
    flush_prepared()

    end_time = monotonic_now()
    time_elapsed = end_time - start_time
    stats = {
        "num_imported": added_total,
        "aliased": aliased_total,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": avg_time_per_item(time_elapsed, added_total),
    }
    if rejects:
        stats["skipped"] = rejects

    if state.format in ("json", "csv"):
        _echo_format(stats)
    else:
        rows = [("Snippets imported", str(stats["num_imported"]))]
        if stats["aliased"]:
            rows.append(("Aliases updated", str(stats["aliased"])))
        if rejects:
            rows.append(("Files skipped", f"{rejects} (empty or unreadable)"))
        rows.append(("Time elapsed", f"{stats['time_elapsed']:.4f}s"))
        if stats["num_imported"] > 0:
            rows.append(("Avg per snippet", f"{stats['avg_time_per_snippet'] * 1000:.4f}ms"))
        _echo_kv_table("Import Complete", rows)


@app.command("list")
def list_cmd(
    range_str: str | None = typer.Option(
        None, "--range", help="A range of snippets to list (e.g., 10-30)."
    ),
) -> None:
    """List all snippets.

    Examples:
      resembl list
      resembl list --range 10-30
      resembl --format json list
      resembl -o json list
    """
    start, end = 0, None
    if range_str:
        parts = range_str.split("-")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            err_console.print(
                "[red]Error:[/red] Invalid --range: use start-end (e.g., 10-30), "
                f"got '{_display(range_str)}'."
            )
            raise typer.Exit(code=USAGE_ERROR)
        start, end = map(int, parts)
        if end < start:
            err_console.print(
                f"[red]Error:[/red] Invalid --range '{_display(range_str)}': "
                "start must not exceed end."
            )
            raise typer.Exit(code=USAGE_ERROR)

    if end is None:
        # Unbounded list: stream in batches so a large database never loads
        # every row (including the code column, which dominates the table)
        # into memory at once.  Use --range to page a specific window.
        _stream_list(state.session)
        return

    snippets = snippet_list(state.session, start, end)
    if state.format in ("json", "csv"):
        _echo_format([{"checksum": s.checksum, "names": s.name_list} for s in snippets])
    elif snippets:
        _echo(_snippet_table(((s.checksum, s.name_list) for s in snippets), "Snippets"))
    else:
        _echo(f"[dim]No snippets in range {start}-{end}. {_NO_SNIPPETS_HINT}[/dim]")


def _stream_list(session: Session) -> None:
    """Render the unbounded ``list`` output in bounded-memory batches.

    Only the (checksum, names) columns are fetched (see
    ``snippet_names_stream``).  JSON output is the items document, CSV a
    header plus streamed rows — the same rows as the in-memory render — and
    table output is printed per batch so a huge
    listing never builds one giant table.  In table mode an empty database
    says so, instead of printing nothing at all.
    """
    if state.quiet:
        return

    if state.format == "json":
        _write_json_stream(
            {"checksum": checksum, "names": json.loads(raw)}
            for batch in snippet_names_stream(session)
            for checksum, raw in batch
        )
    elif state.format == "csv":
        writer = csv.DictWriter(
            sys.stdout, fieldnames=["checksum", "names"], lineterminator=_CSV_LINETERMINATOR
        )
        writer.writeheader()
        for batch in snippet_names_stream(session):
            for checksum, raw in batch:
                writer.writerow(
                    {
                        "checksum": _csv_safe(checksum),
                        "names": _csv_safe(", ".join(json.loads(raw))),
                    }
                )
    else:
        offset = 0
        for batch in snippet_names_stream(session):
            rows = [(checksum, json.loads(raw)) for checksum, raw in batch]
            _echo(_snippet_table(rows, "Snippets", offset + 1))
            offset += len(batch)
        if not offset:
            _echo(f"[dim]No snippets found. {_NO_SNIPPETS_HINT}[/dim]")


@app.command()
def show(
    checksum: str = typer.Argument(help="The checksum (or prefix) of the snippet."),
) -> None:
    """Show detailed information for a specific snippet."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        raise typer.Exit(code=1)

    snippet = snippet_get(state.session, resolved)
    if not snippet:
        err_console.print(
            f"[red]Error:[/red] Snippet with checksum {_display(resolved)} not found."
        )
        raise typer.Exit(code=1)

    if state.format in ("json", "csv"):
        _echo_format(
            {
                "checksum": snippet.checksum,
                "names": snippet.name_list,
                "code": snippet.code,
            }
        )
    else:
        # Deferred import: rich.syntax pulls in pygments.lexers (~30 ms),
        # which only `show`/`compare` render; every other command skips it.
        from rich.syntax import Syntax

        syntax = Syntax(snippet.code, "nasm", theme="monokai", word_wrap=True)
        _echo(
            Panel(
                syntax,
                title=f"[bold]{', '.join(_display(n) for n in snippet.name_list)}[/bold]",
                subtitle=snippet.checksum[:16] + "…",
                border_style=ACCENT,
            )
        )


@app.command()
def rm(
    checksum: str = typer.Argument(help="The checksum of the snippet to remove."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
    backup_dir: str = typer.Option(
        ".resembl-pre-delete-backups",
        "--backup-dir",
        help="Directory for the verified pre-delete backup.",
    ),
    no_backup: bool = typer.Option(
        False,
        "--no-backup",
        help="Skip the verified pre-delete backup (not recommended).",
    ),
) -> None:
    """Remove a snippet by its checksum (or prefix)."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        raise typer.Exit(code=1)
    _confirm(
        f"Are you sure you want to delete the snippet with checksum '{resolved}'?",
        force,
    )
    _backup_before_delete(backup_dir, no_backup)
    if not snippet_delete(state.session, resolved, quiet=state.quiet):
        err_console.print(
            f"[red]Error:[/red] Snippet with checksum '{_display(resolved)}' not found."
        )
        raise typer.Exit(code=1)


@app.command()
def stats() -> None:
    """Show database statistics."""
    try:
        result = db_stats(state.session)
    except ValueError as e:
        # e.g. fingerprints written at mixed permutation counts (a merge
        # clears the stamps; the healing reindex runs on `find`, not here).
        # Report cleanly instead of a traceback — same contract as compare.
        err_console.print(f"[red]Error:[/red] {_display(str(e))}")
        raise typer.Exit(code=1) from e
    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        _echo_kv_table(
            "Database Statistics",
            [
                ("Number of snippets", str(result["num_snippets"])),
                ("Avg snippet size", f"{result['avg_snippet_size']:.2f} chars"),
                ("Vocabulary size", f"{result['vocabulary_size']} tokens"),
                ("Avg Jaccard similarity", f"{result['avg_jaccard_similarity']:.2f}"),
            ],
        )


@app.command()
def verify() -> None:
    """Verify database health (index, fingerprints, pending work).

    Reports the snippet/bucket counts, the fingerprint format version, and
    any pending work: a missing index or stale fingerprints are *warnings*
    (healed automatically by the next ``find``); a bucket/snippet mismatch
    is an *issue* (the index is stale — ``reindex --force`` should run).
    Exits 1 only when issues are found.
    """
    result = db_verify(state.session)
    if state.format in ("json", "csv"):
        _echo_format(result)
        # The exit status is part of the contract in every format: a script
        # gating on `verify --format json` must see the same failure the table
        # render reports, not a clean 0 carrying a non-empty ``issues`` list.
        if result["issues"]:
            raise typer.Exit(code=1)
    else:
        table = Table(title="Database Health", show_header=False, title_style=f"bold {ACCENT}")
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")
        table.add_row("Snippets", str(result["num_snippets"]))
        table.add_row(
            "Bucket rows",
            (
                f"{result['num_buckets']} (expected {result['expected_buckets']})"
                if result["expected_buckets"] is not None
                else "—"
            ),
        )
        table.add_row("Fingerprint version", str(result["fingerprint_version"]))
        _echo(table)
        for warning in result["warnings"]:
            _echo(f"[yellow]• {_display(warning)}[/yellow]")
        for issue in result["issues"]:
            _echo(f"[red]• {_display(issue)}[/red]")
        if result["issues"]:
            raise typer.Exit(code=1)
        if not result["warnings"]:
            _echo("[green]All checks passed.[/green]")


@app.command()
def reindex(
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
    jobs: int | None = typer.Option(
        None,
        "--jobs",
        "-j",
        help="Worker processes for fingerprint recomputation (default: one per CPU).",
    ),
) -> None:
    """Re-calculate all MinHashes in the database."""
    _confirm(
        "Are you sure you want to re-index the entire database? This may take a while.",
        force,
    )

    if jobs is None:
        jobs = max(1, os.cpu_count() or 1)
    result = db_reindex(
        state.session,
        ngram_size=state.config.ngram_size,
        jobs=jobs,
        progress=(
            _build_progress_printer() if state.format == "table" and not state.quiet else None
        ),
    )
    if "error" in result:
        err_console.print(f"[red]Error:[/red] {_display(str(result['error']))}")
        raise typer.Exit(code=1)
    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        rows = [
            ("Snippets re-indexed", str(result["num_reindexed"])),
            ("Time elapsed", f"{result['time_elapsed']:.4f}s"),
        ]
        if result["num_reindexed"] > 0:
            rows.append(("Avg per snippet", f"{result['avg_time_per_snippet'] * 1000:.4f}ms"))
        _echo_kv_table("Re-indexing Complete", rows)


@app.command()
def find(
    query: str | None = typer.Option(None, "--query", help="The query string to search for."),
    file: typer.FileText | None = typer.Option(
        None,
        "--file",
        help="Path to a file containing the query. Use '-' for stdin.",
        encoding="utf-8",
    ),
    top_n: int | None = typer.Option(None, "--top-n", help="Number of top matches to return."),
    threshold: float | None = typer.Option(
        None, "--threshold", help="LSH threshold override (0.0-0.99)."
    ),
    no_normalization: bool = typer.Option(
        False, "--no-normalization", help="Disable token normalization for this query."
    ),
) -> None:
    """Find similar snippets.

    The query comes from --query, from --file ('-' reads stdin), or from
    stdin itself when neither is given and stdin is not a terminal.

    Examples:
      resembl find --query "push ebp; mov ebp, esp"
      resembl find --file routine.asm --top-n 10
      cat routine.asm | resembl --format json find
    """
    effective_top_n = top_n if top_n is not None else state.config.top_n
    effective_threshold = threshold if threshold is not None else state.config.lsh_threshold

    # The banding requires b >= 2 bands; at 128 permutations that caps the
    # buildable threshold near 0.98 (0.981 gives b=1).  Reject unbuildable
    # values up front instead of a silent zero-result build failure.
    _validate_find_params(
        effective_threshold,
        state.config.num_permutations,
        state.config.ngram_size,
        effective_top_n,
    )

    query_string: str | None = None
    if query:
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in query):
            err_console.print("[red]Error:[/red] query input is not valid UTF-8.")
            raise typer.Exit(code=USAGE_ERROR)
        # Single-line ';' separates statements (see
        # ``_query_inline_statements``); multi-line input keeps NASM comments.
        query_string = _query_inline_statements(query)
    elif file:
        try:
            query_string = file.read()
        except UnicodeDecodeError:
            err_console.print("[red]Error:[/red] query input is not valid UTF-8.")
            raise typer.Exit(code=USAGE_ERROR) from None
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in query_string):
            err_console.print("[red]Error:[/red] query input is not valid UTF-8.")
            raise typer.Exit(code=USAGE_ERROR)
    elif not sys.stdin.isatty():
        # A piped or redirected query: `resembl find < snippet.asm`.  Only
        # when neither --query nor --file was given, and only off a TTY, so an
        # interactive run is never left waiting on a terminal.
        try:
            piped = sys.stdin.buffer.read().decode("utf-8")
        except UnicodeDecodeError:
            err_console.print("[red]Error:[/red] query input is not valid UTF-8.")
            raise typer.Exit(code=USAGE_ERROR) from None
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in piped):
            err_console.print("[red]Error:[/red] query input is not valid UTF-8.")
            raise typer.Exit(code=USAGE_ERROR)
        query_string = _query_inline_statements(piped) if piped.strip() else None

    if not query_string:
        err_console.print("[red]Error:[/red] No query provided. Use --query, --file, or stdin.")
        raise typer.Exit(code=USAGE_ERROR)

    # Fast path: if a `serve` process is running for this database, ask it
    # (~ms) instead of paying interpreter startup (~450 ms).  Falls back to
    # the in-process path when no server is reachable.
    ngram_size = state.config.ngram_size
    server_payload = _find_via_server(
        query_string,
        effective_top_n,
        effective_threshold,
        not no_normalization,
        ngram_size,
    )
    if server_payload is not None:
        _render_find_payload(server_payload)
        return

    # The LSH index is built lazily (and rebuilt when threshold/permutation
    # settings change), and fingerprints may need a one-time migration
    # reindex.  Pass a progress printer in table mode so long-running
    # operations report progress (it is a no-op below 100k snippets).
    build_progress = (
        _build_progress_printer() if state.format == "table" and not state.quiet else None
    )
    if state.format == "table" and not state.quiet:
        meta = lsh_meta_get(state.session)
        num_perm = state.config.num_permutations
        if not lsh_meta_matches(meta, effective_threshold, num_perm):
            _echo("[dim]Building LSH index (first search on this database)…[/dim]")

    try:
        num_candidates, matches = snippet_find_matches(
            state.session,
            query_string,
            effective_top_n,
            effective_threshold,
            not no_normalization,
            ngram_size=ngram_size,
            num_permutations=state.config.num_permutations,
            jaccard_weight=state.config.jaccard_weight,
            progress=build_progress,
        )
    except IndexBuildError as e:
        err_console.print(f"[red]Error:[/red] {_display(str(e))}")
        raise typer.Exit(code=1) from e

    _render_find_payload(snippet_matches_payload(num_candidates, matches))


@app.command()
def find_batch(
    file: typer.FileText = typer.Option(
        ...,
        "--file",
        help="File of queries, one per line ('#' = comment).",
        encoding="utf-8",
    ),
    top_n: int | None = typer.Option(
        None, "--top-n", help="Number of top matches to return per query."
    ),
    threshold: float | None = typer.Option(
        None, "--threshold", help="LSH threshold override (0.0-0.99)."
    ),
) -> None:
    """Find matches for many queries in one process.

    Processes every query line in *file* in a single invocation, amortizing
    interpreter startup and the LSH index load across the whole batch — for
    N queries this is roughly N times faster than N separate ``find`` calls.
    The first query pays the one-time index build; the rest are warm.
    """
    effective_top_n = top_n if top_n is not None else state.config.top_n
    effective_threshold = threshold if threshold is not None else state.config.lsh_threshold
    ngram_size = state.config.ngram_size
    _validate_find_params(
        effective_threshold,
        state.config.num_permutations,
        ngram_size,
        effective_top_n,
    )

    try:
        queries = [
            line.strip() for line in file if line.strip() and not line.lstrip().startswith("#")
        ]
    except UnicodeDecodeError:
        err_console.print("[red]Error:[/red] query input is not valid UTF-8.")
        raise typer.Exit(code=USAGE_ERROR) from None
    if any(0xD800 <= ord(ch) <= 0xDFFF for line in queries for ch in line):
        err_console.print("[red]Error:[/red] query input is not valid UTF-8.")
        raise typer.Exit(code=USAGE_ERROR)
    if not queries:
        err_console.print("[red]Error:[/red] No queries found in the file.")
        raise typer.Exit(code=USAGE_ERROR)

    results: list[dict] = []
    # Same convenience as `find --query`: single-line ';' separates
    # statements (in a multi-line batch entry ';' stays a comment).
    converted = [_query_inline_statements(raw_query) for raw_query in queries]

    # Fast path: a running `serve` process answers the whole batch in one
    # round trip with a warm index (fall back to in-process otherwise).
    server_results = _find_batch_via_server(
        converted,
        effective_top_n,
        effective_threshold,
        True,
        ngram_size,
    )
    if server_results is not None:
        results = server_results
    else:
        try:
            for query_string in converted:
                num_candidates, matches = snippet_find_matches(
                    state.session,
                    query_string,
                    effective_top_n,
                    effective_threshold,
                    ngram_size=ngram_size,
                    num_permutations=state.config.num_permutations,
                    jaccard_weight=state.config.jaccard_weight,
                )
                results.append(
                    {
                        "query": query_string,
                        **snippet_matches_payload(num_candidates, matches),
                    }
                )
        except IndexBuildError as e:
            err_console.print(f"[red]Error:[/red] {_display(str(e))}")
            raise typer.Exit(code=1) from e

    if state.format in ("json", "csv"):
        _echo_format(results)
    else:
        for i, result in enumerate(results, 1):
            _echo(f"[bold]{i}. {_display(result['query'])}[/bold]")
            if "error" in result:
                # A per-query server-side failure (isolated by /find-batch)
                # must render as that query's error, not crash the whole
                # batch on the missing payload keys.
                _emit_kept_error(result["error"])
            else:
                _render_find_payload(result)
            _echo("")


@app.command()
def search(
    pattern: str = typer.Argument(help="The name pattern to search for."),
    limit: int = typer.Option(
        50, "--limit", help="Maximum number of results to return (default: 50)."
    ),
) -> None:
    """Search for snippets by matching their names.

    Examples:
      resembl search memcpy
      resembl search memcpy --limit 20
      resembl --format json search memcpy
    """
    snippets = snippet_search_by_name(state.session, pattern, limit=limit)

    if state.format in ("json", "csv"):
        _echo_format([{"checksum": s.checksum, "names": s.name_list} for s in snippets])
    else:
        found = f"{len(snippets)}+" if len(snippets) >= limit else str(len(snippets))
        _echo(f"[dim]Found {found} snippets matching '{_display(pattern)}'.[/dim]")
        if snippets:
            _echo(_snippet_table(((s.checksum, s.name_list) for s in snippets), "Search Results"))
        elif not found.endswith("+"):
            # Names are matched as a substring, so a miss is usually a pattern
            # the user has to shorten.  Say what to try next rather than
            # leaving a bare count as the end of the interaction.
            _echo("[dim]Try a shorter pattern, or 'resembl list' to see every snippet.[/dim]")


@app.command()
def compare(
    checksum1: str = typer.Argument(help="The checksum of the first snippet."),
    checksum2: str = typer.Argument(help="The checksum of the second snippet."),
) -> None:
    """Compare two snippets directly (supports checksum prefixes)."""
    resolved1 = _resolve_checksum(checksum1)
    resolved2 = _resolve_checksum(checksum2)
    if not resolved1 or not resolved2:
        raise typer.Exit(code=1)

    try:
        comparison = snippet_compare(state.session, resolved1, resolved2)
    except ValueError as e:
        # e.g. a corrupt fingerprint (disk rot) — report cleanly, no traceback.
        err_console.print(f"[red]Error:[/red] {_display(str(e))}")
        raise typer.Exit(code=1) from e
    if not comparison:
        err_console.print("[red]Error:[/red] One or both snippets could not be found.")
        raise typer.Exit(code=1)

    if state.format in ("json", "csv"):
        _echo_format(comparison)
        return

    s1 = comparison["snippet1"]
    s2 = comparison["snippet2"]
    comp = comparison["comparison"]

    _echo(
        Panel(
            f"[bold]Snippet 1:[/bold] {_display(str(s1['names']))} "
            f"[dim]({s1['checksum'][:12]}…)[/dim]\n"
            f"[bold]Snippet 2:[/bold] {_display(str(s2['names']))} "
            f"[dim]({s2['checksum'][:12]}…)[/dim]",
            title="Snippet Comparison",
            border_style=ACCENT,
        )
    )

    table = Table(title="Similarity Metrics", title_style=f"bold {ACCENT}")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row(
        "Jaccard Similarity (Structure)",
        f"{comp['jaccard_similarity']:.2f}",
    )
    table.add_row("Levenshtein Score (Code)", f"{comp['levenshtein_score']:.2f}")
    table.add_row(
        "[bold]Hybrid Score",
        f"[bold {ACCENT}]{comp['hybrid_score']:.2f}[/bold {ACCENT}]",
    )
    table.add_row("CFG Similarity", f"{comp['cfg_similarity']:.2f}")
    table.add_row("Shared Normalized Tokens", f"{comp['shared_normalized_tokens']}")
    _echo(table)

    _echo("")
    snip1 = snippet_get(state.session, resolved1)
    snip2 = snippet_get(state.session, resolved2)
    if snip1 is None or snip2 is None:
        err_console.print("[red]Error:[/red] One or both snippets could not be found.")
        raise typer.Exit(code=1)
    diff = list(
        difflib.unified_diff(
            snip1.code.splitlines(keepends=True),
            snip2.code.splitlines(keepends=True),
            fromfile=s1["checksum"][:12],
            tofile=s2["checksum"][:12],
            n=3,
        )
    )
    if diff:
        diff_text = "".join(diff)
        from rich.syntax import Syntax

        syntax = Syntax(diff_text, "diff", theme="monokai", word_wrap=True)
        _echo(Panel(syntax, title="[bold]Code Diff[/bold]", border_style=ACCENT))
    else:
        _echo(
            Panel(
                "[italic]Code is identical.[/italic]",
                title="[bold]Code Diff[/bold]",
                border_style=ACCENT,
            )
        )


@app.command()
def clean(
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
    backup_dir: str = typer.Option(
        ".resembl-pre-delete-backups",
        "--backup-dir",
        help="Directory for the verified pre-delete backup.",
    ),
    no_backup: bool = typer.Option(
        False,
        "--no-backup",
        help="Skip the verified pre-delete backup (not recommended).",
    ),
) -> None:
    """Drop the LSH index and vacuum the database."""
    _confirm("Drop the LSH index and vacuum the database?", force)
    _backup_before_delete(backup_dir, no_backup)
    result = db_clean(state.session)
    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        rows = []
        if result.get("vacuum_success"):
            rows.append(("Database", "[green]Vacuumed successfully[/green]"))
        rows.append(("LSH index", "[green]Dropped[/green]"))
        rows.append(("Time elapsed", f"{result['time_elapsed']:.4f}s"))
        _echo_kv_table("Database Cleaned", rows)


@app.command()
def backup(
    directory: str = typer.Argument(
        help="Directory to write the backup into (created if missing)."
    ),
    keep: int = typer.Option(7, "--keep", help="Newest backups to retain; older ones are removed."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Write a consistent, restorable copy of the database."""
    _confirm(f"Write a backup of the database to '{directory}'?", force)
    try:
        result = db_backup(state.session, directory, keep=keep)
    except (ValueError, OSError) as e:
        err_console.print(f"[red]Error:[/red] Backup failed: {_display(str(e))}")
        raise typer.Exit(code=1) from e
    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        rows = [
            ("Backup", _display(result["backup"])),
            ("Snippets", str(result["num_snippets"])),
        ]
        if result.get("verified") is False:
            rows.append(("Verified", "[red]no[/red]"))
            rows.extend(("Issue", issue) for issue in result.get("verify_issues", []))
        if result.get("num_removed"):
            rows.append(("Old backups removed", str(result["num_removed"])))
        _echo_kv_table("Backup Complete", rows)
    if result.get("manifest_written") is False or result.get("verified") is False:
        err_console.print(
            "[red]Error:[/red] Backup file is complete but its manifest could not be "
            "written; verify-backup cannot cross-check it. Treat it as unverified."
            if result.get("manifest_written") is False
            else "[red]Error:[/red] Backup failed verification; treat it as unrestorable."
        )
        raise typer.Exit(code=1)


@app.command("verify-backup")
def verify_backup_cmd(
    backup_path: str = typer.Argument(help="Path to a backup file to check."),
) -> None:
    """Check a backup opens and holds its manifest's snippet count."""
    if not os.path.isfile(backup_path):
        err_console.print(f"[red]Error:[/red] File not found: {_display(backup_path)}")
        raise typer.Exit(code=1)
    result = db_verify_backup(state.session, backup_path)
    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        rows = [("Snippets", str(result["num_snippets"]))]
        rows.append(("OK", "[green]yes[/green]" if result["ok"] else "[red]no[/red]"))
        rows.extend(("Issue", _display(issue)) for issue in result["issues"])
        _echo_kv_table("Backup Verification", rows)
    if not result["ok"]:
        raise typer.Exit(code=1)


@app.command()
def merge(
    source: str = typer.Argument(
        help="Path to the source database file, or a full DATABASE_URL "
        "(e.g. duckdb:///file.db, postgresql+pg8000://...)."
    ),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
    backup_dir: str = typer.Option(
        ".resembl-pre-delete-backups",
        "--backup-dir",
        help="Directory for the verified pre-merge backup.",
    ),
    no_backup: bool = typer.Option(
        False,
        "--no-backup",
        help="Skip the verified pre-merge backup (not recommended).",
    ),
) -> None:
    """Merge snippets from another resembl database into this one."""
    source_path = source
    if "://" not in source:
        source_path = os.path.abspath(source)
        if not os.path.exists(source_path):
            err_console.print(f"[red]Error:[/red] File not found: {_display(source_path)}")
            raise typer.Exit(code=1)

    if state.format not in ("json", "csv"):
        # A full source URL may embed credentials; mask them for display.
        _echo(f"Merging from [bold]{_display(db_url_mask(source_path))}[/bold]...")
    _confirm(
        f"Merge snippets from '{db_url_mask(source_path)}' into the current database?",
        force,
    )
    _backup_before_delete(backup_dir, no_backup, label="Pre-merge")
    result = db_merge(state.session, source_path)

    if "error" in result:
        err_console.print(f"[red]Error:[/red] {_display(str(result['error']))}")
        raise typer.Exit(code=1)

    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        _echo_kv_table(
            "Merge Complete",
            [
                ("Added", f"[green]{result['added']}[/green] new snippets"),
                (
                    "Updated",
                    f"[yellow]{result['updated']}[/yellow] snippets (merged names/tags)",
                ),
                ("Skipped", f"[dim]{result['skipped']}[/dim] already present"),
                ("Total in source", str(result["total_source"])),
                ("Time elapsed", f"{result['time_elapsed']:.4f}s"),
            ],
        )


# --- Name sub-commands ---


@name_app.command("add")
def name_add_cmd(
    checksum: str = typer.Argument(help="The checksum of the snippet."),
    name: str = typer.Argument(help="The new name for the snippet."),
) -> None:
    """Add a new name to a snippet (supports checksum prefixes)."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        raise typer.Exit(code=1)
    _apply_snippet_field_mutation(
        lambda: snippet_name_add(state.session, resolved, name, quiet=state.quiet),
        get_values=operator.attrgetter("name_list"),
        plural="names",
        failure_message="Failed to add name to snippet.",
    )


@name_app.command("remove")
def name_remove_cmd(
    checksum: str = typer.Argument(help="The checksum of the snippet."),
    name: str = typer.Argument(help="The name to remove."),
) -> None:
    """Remove a name from a snippet (supports checksum prefixes)."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        raise typer.Exit(code=1)
    _apply_snippet_field_mutation(
        lambda: snippet_name_remove(state.session, resolved, name, quiet=state.quiet),
        get_values=operator.attrgetter("name_list"),
        plural="names",
        failure_message="Failed to remove name from snippet.",
    )


# --- Tag sub-commands ---


@tag_app.command("add")
def tag_add_cmd(
    checksum: str = typer.Argument(help="The checksum of the snippet."),
    tag: str = typer.Argument(help="The tag to add."),
) -> None:
    """Add a tag to a snippet (supports checksum prefixes)."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        raise typer.Exit(code=1)
    _apply_snippet_field_mutation(
        lambda: snippet_tag_add(state.session, resolved, tag, quiet=state.quiet),
        get_values=operator.attrgetter("tag_list"),
        plural="tags",
        failure_message="Failed to add tag to snippet.",
    )


@tag_app.command("remove")
def tag_remove_cmd(
    checksum: str = typer.Argument(help="The checksum of the snippet."),
    tag: str = typer.Argument(help="The tag to remove."),
) -> None:
    """Remove a tag from a snippet (supports checksum prefixes)."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        raise typer.Exit(code=1)
    _apply_snippet_field_mutation(
        lambda: snippet_tag_remove(state.session, resolved, tag, quiet=state.quiet),
        get_values=operator.attrgetter("tag_list"),
        plural="tags",
        failure_message="Failed to remove tag from snippet.",
    )


# --- Collection sub-commands ---


@collection_app.command("create")
def collection_create_cmd(
    name: str = typer.Argument(help="Name for the new collection."),
    description: str = typer.Option(
        "", "--description", "-d", help="Description of the collection."
    ),
) -> None:
    """Create a new snippet collection."""
    from .models import Collection

    # ``collection_create`` converges on a name that already exists, so probe
    # first to report which of the two happened rather than claiming a fresh
    # creation on every run.
    already_existed = Collection.get_by_name(state.session, name) is not None
    try:
        col = collection_create(state.session, name, description)
    except ValueError as e:
        logger.error("Could not create collection %r: %s", name, e)
        err_console.print(f"[red]Error:[/red] {_display(str(e))}")
        raise typer.Exit(code=1) from e
    except SQLAlchemyError as e:
        # Driver text carries the SQL statement and its bind parameters, which
        # must not reach the terminal; the name is the caller's own input.
        logger.error("Could not create collection %r: %s", name, e)
        err_console.print(f"[red]Error:[/red] could not create collection '{_display(name)}'.")
        raise typer.Exit(code=1) from e
    if already_existed:
        _echo(f"[green]✓[/green] Collection [bold]{_display(col.name)}[/bold] already exists.")
    else:
        _echo(f"[green]✓[/green] Created collection [bold]{_display(col.name)}[/bold]")


@collection_app.command("delete")
def collection_delete_cmd(
    name: str = typer.Argument(help="Name of the collection to delete."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
    backup_dir: str = typer.Option(
        ".resembl-pre-delete-backups",
        "--backup-dir",
        help="Directory for the verified pre-delete backup.",
    ),
    no_backup: bool = typer.Option(
        False,
        "--no-backup",
        help="Skip the verified pre-delete backup (not recommended).",
    ),
) -> None:
    """Delete a collection (snippets are kept but unassigned)."""
    _confirm(f"Delete collection '{name}'?", force)
    _backup_before_delete(backup_dir, no_backup)
    if collection_delete(state.session, name, quiet=state.quiet):
        _echo(f"[green]✓[/green] Deleted collection [bold]{_display(name)}[/bold]")
    else:
        err_console.print(f"[red]Error:[/red] Collection '{_display(name)}' not found.")
        raise typer.Exit(code=1)


@collection_app.command("list")
def collection_list_cmd() -> None:
    """List all collections."""
    cols = collection_list(state.session)
    if state.format != "table":
        # An empty list is still a document: ``{"items", "total"}`` (json) or a
        # bare header (csv), never the human "No collections found." line, which
        # a parser would choke on.
        _echo_format(cols)
        return

    if not cols:
        _echo("[dim]No collections found.[/dim]")
        return

    table = Table(title="Collections", title_style=f"bold {ACCENT}")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Snippets", justify="right")
    table.add_column("Created", style="dim")
    for col in cols:
        table.add_row(
            _display(col["name"]),
            _display(col["description"]),
            str(col["snippet_count"]),
            _format_created_at(col["created_at"], "%Y-%m-%d", state.tz),
        )
    _echo(table)


@collection_app.command("show")
def collection_show_cmd(
    name: str = typer.Argument(help="Name of the collection to show."),
) -> None:
    """Show all snippets in a collection.

    Streams ``(checksum, names)`` batches so a large collection never loads
    its members' full rows (including the ``code`` column, which dominates
    the table) into memory at once.  Output shape is unchanged.
    """
    if state.quiet:
        return

    batches = snippet_collection_names_stream(state.session, name)
    first_batch = next(batches, None)
    if first_batch is None:
        if state.format != "table":
            # Keep the structured forms a document even when empty.
            _echo_format([])
        else:
            _echo(f"[dim]No snippets in collection '{_display(name)}'.[/dim]")
        return

    def rows() -> Iterator[tuple[str, str]]:
        yield from first_batch
        for batch in batches:
            yield from batch

    if state.format == "json":
        _write_json_stream(
            {"checksum": checksum, "names": json.loads(raw), "collection": name}
            for checksum, raw in rows()
        )
    elif state.format == "csv":
        writer = csv.DictWriter(
            sys.stdout,
            fieldnames=["checksum", "names", "collection"],
            lineterminator=_CSV_LINETERMINATOR,
        )
        writer.writeheader()
        for checksum, raw in rows():
            writer.writerow(
                {
                    "checksum": _csv_safe(checksum),
                    "names": _csv_safe(", ".join(json.loads(raw))),
                    "collection": _csv_safe(name),
                }
            )
    else:
        # The table is built from the streamed (checksum, names) rows; the
        # ORM objects and their ``code`` strings never enter memory.
        table = Table(title=f"Collection: {_display(name)}", title_style=f"bold {ACCENT}")
        table.add_column("Checksum", style="dim")
        table.add_column("Names", style="bold")
        for checksum, raw in rows():
            table.add_row(checksum[:12] + "…", ", ".join(_display(n) for n in json.loads(raw)))
        _echo(table)


@collection_app.command("add")
def collection_add_cmd(
    collection_name: str = typer.Argument(help="Name of the collection."),
    checksum: str = typer.Argument(help="Checksum (or prefix) of the snippet to add."),
) -> None:
    """Add a snippet to a collection."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        raise typer.Exit(code=1)
    snippet = collection_add_snippet(state.session, collection_name, resolved, quiet=state.quiet)
    if snippet:
        names = ", ".join(snippet.name_list)
        _echo(
            f"[green]✓[/green] Added [bold]{_display(names)}[/bold] to collection "
            f"[bold]{_display(collection_name)}[/bold]"
        )
    else:
        if not state.quiet:
            err_console.print("[red]Error:[/red] Failed to add snippet to collection.")
        raise typer.Exit(code=1)


@collection_app.command("remove")
def collection_remove_cmd(
    checksum: str = typer.Argument(
        help="Checksum (or prefix) of the snippet to remove from its collection."
    ),
) -> None:
    """Remove a snippet from its collection."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        raise typer.Exit(code=1)
    snippet = collection_remove_snippet(state.session, resolved, quiet=state.quiet)
    if snippet:
        names = ", ".join(snippet.name_list)
        _echo(f"[green]✓[/green] Removed [bold]{_display(names)}[/bold] from its collection")
    else:
        if not state.quiet:
            err_console.print("[red]Error:[/red] Failed to remove snippet from collection.")
        raise typer.Exit(code=1)


# --- Version commands ---


@app.command("version")
def version_cmd(
    checksum: str = typer.Argument(help="Checksum (or prefix) of the snippet."),
) -> None:
    """Show version history for a snippet."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        raise typer.Exit(code=1)
    # Table renders at most a screen; cap the fetch so a long history
    # does not materialize whole just to show its first rows. JSON
    # keeps the full history (machine consumers may page it).
    versions = snippet_version_list(
        state.session,
        resolved,
        limit=50 if state.format == "table" else None,
    )

    if state.format != "table":
        _echo_format(versions)
        return

    if not versions:
        _echo("[dim]No version history for this snippet.[/dim]")
        return

    table = Table(title="Version History", title_style=f"bold {ACCENT}")
    table.add_column("ID", justify="right")
    table.add_column("Created At")
    for v in versions:
        table.add_row(
            str(v["id"]), _format_created_at(v["created_at"], "%Y-%m-%d %H:%M:%S %z", state.tz)
        )
    _echo(table)


# --- Config sub-commands ---


@config_app.command("path")
def config_path_cmd() -> None:
    """Show the path to the config file."""
    typer.echo(config_path_get())


@config_app.command("list")
def config_list_cmd() -> None:
    """List current settings."""
    full_config = load_config()
    if state.format in ("json", "csv"):
        _echo_format(dict(full_config.items()))
    else:
        table = Table(title="Configuration", title_style=f"bold {ACCENT}")
        table.add_column("Key", style="bold")
        table.add_column("Value", justify="right")
        for key, value in full_config.items():
            table.add_row(key, str(value))
        _echo(table)


@config_app.command("get")
def config_get_cmd(
    key: str = typer.Argument(help="The configuration key to get."),
) -> None:
    """Get a configuration value."""
    if key not in DEFAULTS:
        # Same rule as `config set`: an unknown key is a typo, not a value.
        # getattr's default would have answered "None" and exited 0.
        err_console.print(
            f"[red]Error:[/red] Invalid configuration key: '{_display(key)}'. "
            f"Valid keys: {', '.join(DEFAULTS)}."
        )
        raise typer.Exit(code=USAGE_ERROR)
    cfg = load_config()
    value = getattr(cfg, key)
    if state.format in ("json", "csv"):
        _echo_format({key: value})
    else:
        _echo(_display(str(value)))


@config_app.command("set")
def config_set_cmd(
    key: str = typer.Argument(help="The configuration key to set."),
    value: str = typer.Argument(help="The value to set."),
) -> None:
    """Set a configuration value."""
    if key not in DEFAULTS:
        err_console.print(
            f"[red]Error:[/red] Invalid configuration key: '{_display(key)}'. "
            f"Valid keys: {', '.join(DEFAULTS)}."
        )
        raise typer.Exit(code=USAGE_ERROR)
    default_value = DEFAULTS[key]
    try:
        typed_value: int | float | str = type(default_value)(value)
    except (TypeError, ValueError, OverflowError) as exc:
        err_console.print(
            f"[red]Error:[/red] Invalid value for '{_display(key)}': expected "
            f"{type(default_value).__name__}, got '{_display(value)}'."
        )
        raise typer.Exit(code=USAGE_ERROR) from exc
    # "nan"/"inf" coerce to a float cleanly but would poison every downstream
    # calculation (a NaN jaccard_weight makes every similarity score NaN), and
    # a value outside the range the code works in (ngram_size 0, threshold 5.0)
    # is a silent misconfiguration rather than a setting: reject both here,
    # with the same checker the config file is read through (config.validate_value).
    problem = validate_value(key, typed_value)
    if problem is not None:
        err_console.print(
            f"[red]Error:[/red] Invalid value for '{_display(key)}': "
            f"{_display(problem)}, got '{_display(value)}'."
        )
        raise typer.Exit(code=USAGE_ERROR)
    try:
        new_config = update_config(key, typed_value)
    except OSError as e:
        err_console.print(f"[red]Error:[/red] cannot write the config file: {_display(str(e))}")
        raise typer.Exit(code=1) from e
    _echo(f"[green]✓[/green] Set [bold]{_display(key)}[/bold] to {_display(str(new_config[key]))}")
    state.config.update(new_config)


@config_app.command("unset")
def config_unset_cmd(
    key: str = typer.Argument(help="The configuration key to unset."),
) -> None:
    """Unset a configuration value."""
    if key not in DEFAULTS:
        err_console.print(
            f"[red]Error:[/red] Invalid configuration key: '{_display(key)}'. "
            f"Valid keys: {', '.join(DEFAULTS)}."
        )
        raise typer.Exit(code=USAGE_ERROR)
    try:
        new_config = remove_config_key(key)
    except OSError as e:
        err_console.print(f"[red]Error:[/red] cannot write the config file: {_display(str(e))}")
        raise typer.Exit(code=1) from e
    _echo(f"[green]✓[/green] Unset [bold]{_display(key)}[/bold], returning to default.")
    state.config.update(new_config)


def _pre_scan_output(argv: list[str]) -> None:
    """Record ``-o``/``--output`` before Typer parses the command line.

    Typer validates the whole group before invoking the eager ``-o``
    callback: an unknown option or command at the group level fails the
    parse first, so ``-o json --badflag`` never reaches ``_accept_output``
    and the JSON error envelope would not be emitted for its own usage
    error.  Scanning the leading global options here mirrors the
    ``--no-color`` pre-scan below.
    """
    argv = list(argv)
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("-o", "--output"):
            if index + 1 < len(argv):
                value = argv[index + 1]
                if value in _OUTPUT_CHOICES:
                    _output_choice.set(value)
                index += 2
                continue
            break
        if token.startswith("--output="):
            value = token.split("=", 1)[1]
            if value in _OUTPUT_CHOICES:
                _output_choice.set(value)
            index += 1
            continue
        if token.startswith("-") and token != "-":
            # A global flag that may take a value (--format, --tz) consumes
            # the next token; other flags do not.  Only the forms that can
            # precede -o matter, and every other token ends the scan: the
            # subcommand name is not a global option.
            if token in ("--format", "--tz"):
                index += 2
                continue
            if "=" in token:
                index += 1
                continue
            index += 1
            continue
        break


def main() -> None:
    """Entry point for the resembl command line interface."""
    _pre_scan_output(sys.argv[1:])
    # Before anything is written: a locale-encoded stdout raises
    # UnicodeEncodeError on the first snippet name it cannot represent, which
    # costs the user the whole report.
    console_utf8_reconfigure()
    if "--no-color" in sys.argv:
        # --no-color has to reach typer's own renderer, not just this module's
        # consoles: the --help panel is painted before the callback runs, and
        # typer forces color whenever GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS
        # is set, even into a pipe.  Reading argv here is the one point that
        # runs before the help is built.
        from typer import rich_utils

        rich_utils.FORCE_TERMINAL = False
        rich_utils.COLOR_SYSTEM = None
    app()


if __name__ == "__main__":
    main()
