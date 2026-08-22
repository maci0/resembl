"""Command-line interface for the resembl assembly similarity tool.

This module wires together the ``core``, ``config``, ``database``, and
``models`` modules into a user-facing CLI built with Typer.  Every
command respects the ``--quiet``, ``--no-color``, and ``--format``
global options.

Key design choices
------------------
* **Checksum prefix resolution** – Any command that accepts a checksum
  also accepts a unique prefix, resolved via ``_resolve_checksum``.
* **Structured output** – Every command supports ``--format json`` and
  ``--format csv`` in addition to the default Rich table output.
* **Quiet mode** – ``_echo`` is used instead of ``console.print`` so
  that ``--quiet`` suppresses all informational output.
"""

from __future__ import annotations

import atexit
import csv
import difflib
import json
import logging
import multiprocessing
import os
import signal
import sys
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from typing import Any, cast

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import track
from rich.syntax import Syntax
from rich.table import Table
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from .config import (
    DEFAULTS,
    ResemblConfig,
    config_path_get,
    load_config,
    remove_config_key,
    update_config,
)
from .core import (
    IndexBuildError,
    adaptive_worker_count,
    collection_add_snippet,
    collection_create,
    collection_delete,
    collection_list,
    collection_remove_snippet,
    db_clean,
    db_merge,
    db_reindex,
    db_stats,
    db_verify,
    snippet_add,
    snippet_add_batch,
    snippet_compare,
    snippet_delete,
    snippet_export,
    snippet_export_yara,
    snippet_find_matches,
    snippet_get,
    snippet_list,
    snippet_name_add,
    snippet_name_remove,
    snippet_prepare,
    snippet_search_by_name,
    snippet_tag_add,
    snippet_tag_remove,
    snippet_version_list,
)
from .database import db_create, get_engine
from .lsh import lsh_meta_get, lsh_meta_matches

logger = logging.getLogger(__name__)

# --- Rich Consoles ---

console = Console()
err_console = Console(stderr=True)

# --- Typer apps ---

app = typer.Typer(
    help="A CLI for finding similar assembly code snippets.",
    add_completion=False,
    rich_markup_mode="rich",
)
config_app = typer.Typer(help="Manage user configuration.", rich_markup_mode="rich")
name_app = typer.Typer(help="Manage snippet names.", rich_markup_mode="rich")
tag_app = typer.Typer(help="Manage snippet tags.", rich_markup_mode="rich")
collection_app = typer.Typer(
    help="Manage snippet collections.", rich_markup_mode="rich"
)
app.add_typer(config_app, name="config")
app.add_typer(name_app, name="name")
app.add_typer(tag_app, name="tag")
app.add_typer(collection_app, name="collection")


# --- State ---


class State:
    """Shared state for all commands."""

    session: Session
    config: ResemblConfig
    quiet: bool = False
    no_color: bool = False
    format: str = "table"


state = State()


def _echo(message: object, **kwargs: Any) -> None:
    """Print a message unless ``--quiet`` is set."""
    if not state.quiet:
        console.print(message, **kwargs)


def _echo_format(data: object) -> None:
    """Print data in the requested format (JSON/CSV) unless ``--quiet``."""
    if state.quiet:
        return
    if state.format == "csv":
        if isinstance(data, dict) and "matches" in data:
            data = data["matches"]
        if isinstance(data, list) and data and isinstance(data[0], dict):
            for row in data:
                if "names" in row and isinstance(row["names"], list):
                    row["names"] = ", ".join(row["names"])
            writer = csv.DictWriter(sys.stdout, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)
        elif isinstance(data, dict):
            data = {
                k: ", ".join(v) if isinstance(v, list) else v for k, v in data.items()
            }
            writer = csv.DictWriter(sys.stdout, fieldnames=data.keys())
            writer.writeheader()
            writer.writerow(data)
        else:
            console.print(json.dumps(data, indent=2))
    else:
        # JSON is the default structured format
        console.print_json(json.dumps(data, indent=2))


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

    from .server import server_port_path

    db_url = str(cast(Engine, state.session.get_bind()).url)
    port_file = server_port_path(db_url)
    try:
        with open(port_file, encoding="utf-8") as f:
            port = int(f.read().strip())
    except (OSError, ValueError):
        return None

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if isinstance(exc, urllib.error.URLError) and isinstance(
            exc.reason, TimeoutError
        ):
            return None
        try:
            os.remove(port_file)
        except OSError:
            pass
        return None
    if "error" in payload:
        return None
    return payload


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
            "top_n": top_n,
            "threshold": threshold,
            "normalize": normalize,
            "ngram_size": ngram_size,
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
    """Query a running ``serve`` process with a batch (one round trip)."""
    payload = _server_request(
        "/find-batch",
        {
            "queries": queries,
            "top_n": top_n,
            "threshold": threshold,
            "normalize": normalize,
            "ngram_size": ngram_size,
        },
        timeout=60,
    )
    if payload is None:
        return None
    return payload["results"]


def _render_find_payload(payload: dict) -> None:
    """Render a ``{lsh_candidates, matches}`` payload in the current format."""
    if state.format in ("json", "csv"):
        _echo_format(payload)
        return
    _echo(f"[dim]Found {payload['lsh_candidates']} candidates via LSH.[/dim]")
    matches = payload["matches"]
    if matches:
        table = Table(title="Top Matches", title_style="bold cyan")
        table.add_column("#", style="dim", justify="right")
        table.add_column("Checksum", style="bold")
        table.add_column("Names")
        table.add_column("Score (Hybrid)", justify="right")
        for i, match in enumerate(matches, 1):
            score = match["score"]
            score_color = "green" if score >= 80 else "yellow" if score >= 50 else "red"
            table.add_row(
                str(i),
                match["checksum"][:12] + "…",
                ", ".join(match["names"]),
                f"[{score_color}]{score:.2f}[/{score_color}]",
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
) -> None:
    """Serve find queries from a warm process (instant warm finds).

    Start this once per database; ``resembl find`` then talks to it over
    localhost instead of paying ~450 ms of interpreter startup per query.
    The port is written to the cache directory and removed on exit.
    """
    from .server import serve as serve_start

    db_url = str(cast(Engine, state.session.get_bind()).url)
    if host not in ("127.0.0.1", "localhost", "::1"):
        _echo(
            "[yellow]Warning: binding a non-loopback interface exposes an "
            "unauthenticated find service.[/yellow]"
        )
    _echo("[dim]Warming up index (first serve can take a moment)…[/dim]")
    try:
        httpd = serve_start(db_url, host=host, port=port)
    except ValueError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1)
    except OSError as e:
        # e.g. --port already in use by another process.
        err_console.print(
            f"[red]Error:[/red] could not bind {host}:{port}: {e.strerror or e}"
        )
        raise typer.Exit(code=1)
    _echo(f"[dim]resembl server listening on {host}:{httpd.server_address[1]}[/dim]")

    # Service managers (systemd, Docker stop, kill) send SIGTERM, whose
    # default disposition kills the process without running the atexit
    # cleanup — leaving a stale port file behind.  Turn SIGTERM into a
    # KeyboardInterrupt so the normal shutdown path runs (server close +
    # port-file removal); the thin client tolerates a stale file, but a
    # clean lifecycle keeps the cache dir tidy across restarts.
    def _handle_sigterm(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def _validate_find_threshold(threshold: float, num_perm: int) -> None:
    """Exit with a clean error if *threshold* cannot build an index.

    The banding needs b >= 2 bands; at 128 permutations that caps the
    buildable threshold near 0.98 (0.981 gives b=1).  Without this check,
    an unbuildable threshold made the index build fail and ``find`` return
    zero results silently.
    """
    if not 0.0 <= threshold < 0.99:
        err_console.print(
            "[red]Error:[/red] --threshold must be between 0.0 and 0.99 (exclusive)."
        )
        raise typer.Exit(code=1)
    from .lsh import _banding_params

    try:
        bands, _ = _banding_params(threshold, num_perm)
    except ValueError:
        bands = 1
    if bands < 2:
        err_console.print(
            "[red]Error:[/red] --threshold is too high for "
            f"{num_perm} permutations (it would leave fewer than 2 bands).  "
            "Lower the threshold or raise num_permutations."
        )
        raise typer.Exit(code=1)


def _resolve_checksum(prefix: str) -> str | None:
    """Resolve a checksum prefix to a full checksum.

    If *prefix* matches exactly one snippet, return its full checksum.
    If it matches zero or more than one, print an error and return ``None``.
    """
    from sqlmodel import select  # Local import — only needed for prefix LIKE query

    from .models import Snippet as SnippetModel

    # Try exact match first
    exact = SnippetModel.get_by_checksum(state.session, prefix)
    if exact:
        return exact.checksum

    # Prefix search
    candidates = state.session.exec(
        select(SnippetModel).where(
            SnippetModel.checksum.like(f"{prefix}%")  # type: ignore[attr-defined]
        )
    ).all()

    if len(candidates) == 0:
        err_console.print(f"[red]Error:[/red] No snippet found matching '{prefix}'.")
        return None
    if len(candidates) > 1:
        err_console.print(
            f"[red]Error:[/red] Ambiguous prefix '{prefix}' matches {len(candidates)} snippets."
        )
        return None
    return candidates[0].checksum


@app.callback()
def app_callback(
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Suppress informational output."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Increase output verbosity."
    ),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colored output."),
    format_opt: str | None = typer.Option(
        None, "--format", help="Output format: table, json, csv. Overrides config."
    ),
) -> None:
    """Set up logging and shared state."""
    global console, err_console

    state.quiet = quiet
    state.no_color = no_color

    if no_color:
        console = Console(no_color=True, highlight=False)
        err_console = Console(stderr=True, no_color=True, highlight=False)

    log_level = logging.INFO
    if quiet:
        log_level = logging.WARNING
    elif verbose:
        log_level = logging.DEBUG
    logging.basicConfig(level=log_level, stream=sys.stdout)

    state.config = load_config()
    state.format = format_opt or state.config.format
    try:
        db_create()
    except SQLAlchemyError as e:
        err_console.print(
            f"[red]Error:[/red] cannot open the database "
            f"({os.environ.get('DATABASE_URL', 'sqlite:///assembly.db')}): {e}"
        )
        raise typer.Exit(code=1)
    state.session = Session(get_engine())
    atexit.register(state.session.close)


# --- Snippet commands ---


@app.command()
def add(
    name: str = typer.Argument(help="The name or alias for the snippet."),
    code: str = typer.Argument(help="The assembly code of the snippet."),
) -> None:
    """Add a new snippet or an alias to existing code."""
    snippet = snippet_add(state.session, name, code, ngram_size=state.config.ngram_size)
    if snippet:
        if state.format in ("json", "csv"):
            _echo_format({"checksum": snippet.checksum, "names": snippet.name_list})
        else:
            _echo(
                f"[green]✓[/green] Snippet [bold]{snippet.checksum[:12]}…[/bold] "
                f"now has names: {snippet.name_list}"
            )
    else:
        if state.format in ("json", "csv"):
            _echo_format({"error": "Failed to add snippet."})
        else:
            err_console.print(
                "[red]Error:[/red] Snippet could not be added (empty code?)."
            )
            raise typer.Exit(code=1)


@app.command("export")
def export_cmd(
    directory: str = typer.Argument(help="The directory to export snippets to."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Export all snippets to a directory."""
    if not force:
        typer.confirm(
            f"Are you sure you want to export all snippets to '{directory}'?",
            abort=True,
        )

    try:
        result = snippet_export(state.session, directory)
    except OSError as e:
        err_console.print(f"[red]Error:[/red] Export failed: {e}")
        raise typer.Exit(code=1)

    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        table = Table(
            title="Export Complete", show_header=False, title_style="bold cyan"
        )
        table.add_column("Key", style="dim")
        table.add_column("Value")
        table.add_row("Snippets exported", str(result["num_exported"]))
        table.add_row("Time elapsed", f"{result['time_elapsed']:.4f}s")
        if result["num_exported"] > 0:
            table.add_row(
                "Avg per snippet", f"{result['avg_time_per_snippet'] * 1000:.4f}ms"
            )
        _echo(table)


@app.command("export-yara")
def export_yara_cmd(
    output_file: str = typer.Argument(help="The output file to save YARA rules to."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Export snippets as YARA string patterns."""
    if not force:
        typer.confirm(
            f"Are you sure you want to export YARA rules to '{output_file}'?",
            abort=True,
        )

    try:
        result = snippet_export_yara(state.session, output_file)
    except OSError as e:
        err_console.print(f"[red]Error:[/red] YARA export failed: {e}")
        raise typer.Exit(code=1)

    if state.format in ("json", "csv"):
        _echo_format(result)
        return

    table = Table(
        title="YARA Export Complete", show_header=False, title_style="bold cyan"
    )
    table.add_column("Key", style="dim")
    table.add_column("Value")
    table.add_row("Rules exported", str(result["num_exported"]))
    table.add_row("Time elapsed", f"{result['time_elapsed']:.4f}s")
    if result["num_exported"] > 0:
        table.add_row("Avg per rule", f"{result['avg_time_per_snippet'] * 1000:.4f}ms")
    _echo(table)


def _import_prepare_file(args: tuple[str, int]) -> tuple[str, str, str, bytes] | None:
    """Worker: read a file and compute its checksum + MinHash fingerprint.

    Top-level and pure (no database access) so it can run in a process pool.
    Returns a ``snippet_prepare`` tuple, or ``None`` if the file is empty
    or unreadable (a failure here must never silently drop rows — see
    ``import_cmd``, which counts rejects).
    """
    file_path, ngram_size = args
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()
    except (OSError, UnicodeDecodeError):
        # Unreadable or non-UTF-8 files are rejected, never fatal.
        return None
    name = os.path.splitext(os.path.basename(file_path))[0]
    return snippet_prepare(name, code, ngram_size)


@app.command("import")
def import_cmd(
    directory: str = typer.Argument(
        help="The directory containing .asm or .txt files."
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
    """Bulk import snippets from a directory."""
    if not os.path.isdir(directory):
        err_console.print(f"[red]Error:[/red] Directory not found: {directory}")
        raise typer.Exit(code=1)

    if not force:
        typer.confirm(
            f"Are you sure you want to import all snippets from '{directory}'?",
            abort=True,
        )

    start_time = time.monotonic()
    ngram_size = state.config.ngram_size

    # Case-insensitive extension match so a directory imports identically
    # on every platform: glob patterns are folded by os.path.normcase on
    # Windows but matched verbatim on Linux/macOS, so "*.asm" silently
    # skipped FOO.ASM there.
    file_paths: list[str] = []
    for root, _dirs, files in os.walk(directory):
        file_paths.extend(
            os.path.join(root, fname)
            for fname in files
            if fname.lower().endswith((".asm", ".txt"))
        )

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
        batch_result = snippet_add_batch(state.session, prepared)
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
            handle_prepared(_import_prepare_file((fp, ngram_size)))

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
                in_flight: deque = deque()

                def _submit_next() -> bool:
                    fp = next(file_iter, None)
                    if fp is None:
                        return False
                    in_flight.append(
                        executor.submit(_import_prepare_file, (fp, ngram_size))
                    )
                    return True

                for _ in range(min(window, len(file_paths))):
                    _submit_next()

                def _completed_futures():
                    while in_flight:
                        done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                        for future in done:
                            in_flight.remove(future)
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
        except Exception:
            logger.warning(
                "Process pool unavailable; falling back to in-process import."
            )
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

    end_time = time.monotonic()
    time_elapsed = end_time - start_time
    stats = {
        "num_imported": added_total,
        "aliased": aliased_total,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": (time_elapsed / added_total) if added_total > 0 else 0,
    }
    if rejects:
        stats["skipped"] = rejects

    if state.format in ("json", "csv"):
        _echo_format(stats)
    else:
        table = Table(
            title="Import Complete", show_header=False, title_style="bold cyan"
        )
        table.add_column("Key", style="dim")
        table.add_column("Value")
        table.add_row("Snippets imported", str(stats["num_imported"]))
        if stats["aliased"]:
            table.add_row("Aliases updated", str(stats["aliased"]))
        if rejects:
            table.add_row("Files skipped", f"{rejects} (empty or unreadable)")
        table.add_row("Time elapsed", f"{stats['time_elapsed']:.4f}s")
        if stats["num_imported"] > 0:
            table.add_row(
                "Avg per snippet", f"{stats['avg_time_per_snippet'] * 1000:.4f}ms"
            )
        _echo(table)


@app.command("list")
def list_cmd(
    range_str: str | None = typer.Option(
        None, "--range", help="A range of snippets to list (e.g., 10-30)."
    ),
) -> None:
    """List all snippets."""
    start, end = 0, 0
    if range_str:
        parts = range_str.split("-")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            err_console.print(
                "[red]Error:[/red] Invalid range format. Use start-end (e.g., 10-30)."
            )
            raise typer.Exit(code=1)
        start, end = map(int, parts)
        if end < start:
            err_console.print(
                f"[red]Error:[/red] Invalid range '{range_str}': start must not "
                "exceed end."
            )
            raise typer.Exit(code=1)

    if start == 0 and end == 0:
        # Unbounded list: stream in batches so a large database never loads
        # every row (including the code column, which dominates the table)
        # into memory at once.  Use --range to page a specific window.
        _stream_list(state.session)
        return

    snippets = snippet_list(state.session, start, end)
    if state.format in ("json", "csv"):
        _echo_format([{"checksum": s.checksum, "names": s.name_list} for s in snippets])
    else:
        table = Table(title="Snippets", title_style="bold cyan")
        table.add_column("#", style="dim", justify="right")
        table.add_column("Checksum", style="bold")
        table.add_column("Names")
        for i, snippet in enumerate(snippets, 1):
            table.add_row(
                str(i), snippet.checksum[:12] + "…", ", ".join(snippet.name_list)
            )
        _echo(table)


def _stream_list(session: Session) -> None:
    """Render the unbounded ``list`` output in bounded-memory batches.

    Only the (checksum, names) columns are fetched (see
    ``snippet_names_stream``).  JSON output is written as a single array,
    CSV as a header plus streamed rows — byte-for-byte the same shape as
    the in-memory render — and table output is printed per batch so a huge
    listing never builds one giant table.
    """
    from .core import snippet_names_stream

    if state.quiet:
        return

    def names_list(raw: str) -> list[str]:
        return json.loads(raw)

    if state.format == "json":
        sys.stdout.write("[")
        first = True
        for batch in snippet_names_stream(session):
            for checksum, raw in batch:
                if not first:
                    sys.stdout.write(",")
                first = False
                sys.stdout.write(
                    "\n  "
                    + json.dumps({"checksum": checksum, "names": names_list(raw)})
                )
        sys.stdout.write("\n]\n" if not first else "]\n")
    elif state.format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=["checksum", "names"])
        writer.writeheader()
        for batch in snippet_names_stream(session):
            for checksum, raw in batch:
                writer.writerow(
                    {"checksum": checksum, "names": ", ".join(names_list(raw))}
                )
    else:
        offset = 0
        for batch in snippet_names_stream(session):
            table = Table(title="Snippets", title_style="bold cyan")
            table.add_column("#", style="dim", justify="right")
            table.add_column("Checksum", style="bold")
            table.add_column("Names")
            for i, (checksum, raw) in enumerate(batch, offset + 1):
                table.add_row(str(i), checksum[:12] + "…", ", ".join(names_list(raw)))
            _echo(table)
            offset += len(batch)


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
            f"[red]Error:[/red] Snippet with checksum {resolved} not found."
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
        syntax = Syntax(snippet.code, "nasm", theme="monokai", word_wrap=True)
        _echo(
            Panel(
                syntax,
                title=f"[bold]{', '.join(snippet.name_list)}[/bold]",
                subtitle=snippet.checksum[:16] + "…",
                border_style="cyan",
            )
        )


@app.command()
def rm(
    checksum: str = typer.Argument(help="The checksum of the snippet to remove."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Remove a snippet by its checksum (or prefix)."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        raise typer.Exit(code=1)
    if not force:
        typer.confirm(
            f"Are you sure you want to delete the snippet with checksum '{resolved}'?",
            abort=True,
        )
    if not snippet_delete(state.session, resolved, quiet=state.quiet):
        err_console.print(
            f"[red]Error:[/red] Snippet with checksum '{resolved}' not found."
        )
        raise typer.Exit(code=1)


@app.command()
def stats() -> None:
    """Show database statistics."""
    result = db_stats(state.session)
    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        table = Table(
            title="Database Statistics", show_header=False, title_style="bold cyan"
        )
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")
        table.add_row("Number of snippets", str(result["num_snippets"]))
        table.add_row("Avg snippet size", f"{result['avg_snippet_size']:.2f} chars")
        table.add_row("Vocabulary size", f"{result['vocabulary_size']} tokens")
        table.add_row(
            "Avg Jaccard similarity", f"{result['avg_jaccard_similarity']:.2f}"
        )
        _echo(table)


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
    else:
        table = Table(
            title="Database Health", show_header=False, title_style="bold cyan"
        )
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
            _echo(f"[yellow]• {warning}[/yellow]")
        if result["issues"]:
            for issue in result["issues"]:
                _echo(f"[red]• {issue}[/red]")
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
    if not force:
        typer.confirm(
            "Are you sure you want to re-index the entire database? This may take a while.",
            abort=True,
        )

    if jobs is None:
        jobs = max(1, os.cpu_count() or 1)
    result = db_reindex(
        state.session,
        ngram_size=state.config.ngram_size,
        jobs=jobs,
        progress=(
            _build_progress_printer()
            if state.format == "table" and not state.quiet
            else None
        ),
    )
    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        table = Table(
            title="Re-indexing Complete", show_header=False, title_style="bold cyan"
        )
        table.add_column("Key", style="dim")
        table.add_column("Value")
        table.add_row("Snippets re-indexed", str(result["num_reindexed"]))
        table.add_row("Time elapsed", f"{result['time_elapsed']:.4f}s")
        if result["num_reindexed"] > 0:
            table.add_row(
                "Avg per snippet", f"{result['avg_time_per_snippet'] * 1000:.4f}ms"
            )
        _echo(table)


@app.command()
def find(
    query: str | None = typer.Option(
        None, "--query", help="The query string to search for."
    ),
    file: typer.FileText | None = typer.Option(
        None, "--file", help="Path to a file containing the query. Use '-' for stdin."
    ),
    top_n: int | None = typer.Option(
        None, "--top-n", help="Number of top matches to return."
    ),
    threshold: float | None = typer.Option(
        None, "--threshold", help="LSH threshold override (0.0-1.0)."
    ),
    no_normalization: bool = typer.Option(
        False, "--no-normalization", help="Disable token normalization for this query."
    ),
) -> None:
    """Find similar snippets."""
    effective_top_n = top_n if top_n is not None else state.config.top_n
    effective_threshold = (
        threshold if threshold is not None else state.config.lsh_threshold
    )

    # The banding requires b >= 2 bands; at 128 permutations that caps the
    # buildable threshold near 0.98 (0.981 gives b=1).  Reject unbuildable
    # values up front instead of a silent zero-result build failure.
    _validate_find_threshold(
        effective_threshold,
        state.config.num_permutations,
    )

    query_string: str | None = None
    if query:
        query_string = query
        # Inline ``--query`` strings use ';' as a statement separator (the
        # documented convenience format).  For single-line input, split on ';'
        # so the lexer does not silently treat the rest of the query as a
        # comment.  Multi-line input (and ``--file``) keep normal NASM
        # semantics, where ';' starts a comment.
        if ";" in query_string and "\n" not in query_string:
            query_string = query_string.replace(";", "\n")
    elif file:
        query_string = file.read()

    if not query_string:
        err_console.print(
            "[red]Error:[/red] No query provided. Use --query, --file, or stdin."
        )
        raise typer.Exit(code=1)

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
        _build_progress_printer()
        if state.format == "table" and not state.quiet
        else None
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
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1)

    _render_find_payload(
        {
            "lsh_candidates": num_candidates,
            "matches": [
                {"checksum": s.checksum, "names": s.name_list, "score": score}
                for s, score in matches
            ],
        }
    )


@app.command()
def find_batch(
    file: typer.FileText = typer.Option(
        ..., "--file", help="File of queries, one per line ('#' = comment)."
    ),
    top_n: int | None = typer.Option(
        None, "--top-n", help="Number of top matches to return per query."
    ),
    threshold: float | None = typer.Option(
        None, "--threshold", help="LSH threshold override (0.0-1.0)."
    ),
) -> None:
    """Find matches for many queries in one process.

    Processes every query line in *file* in a single invocation, amortizing
    interpreter startup and the LSH index load across the whole batch — for
    N queries this is roughly N times faster than N separate ``find`` calls.
    The first query pays the one-time index build; the rest are warm.
    """
    effective_top_n = top_n if top_n is not None else state.config.top_n
    effective_threshold = (
        threshold if threshold is not None else state.config.lsh_threshold
    )
    ngram_size = state.config.ngram_size
    _validate_find_threshold(
        effective_threshold,
        state.config.num_permutations,
    )

    queries = [
        line.strip()
        for line in file
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not queries:
        err_console.print("[red]Error:[/red] No queries found in the file.")
        raise typer.Exit(code=1)

    results: list[dict] = []
    converted = []
    for raw_query in queries:
        # Same convenience as `find --query`: single-line ';' separates
        # statements (in a multi-line batch entry ';' stays a comment).
        converted.append(
            raw_query.replace(";", "\n")
            if ";" in raw_query and "\n" not in raw_query
            else raw_query
        )

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
                        "lsh_candidates": num_candidates,
                        "matches": [
                            {"checksum": s.checksum, "names": s.name_list, "score": score}
                            for s, score in matches
                        ],
                    }
                )
        except IndexBuildError as e:
            err_console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(code=1)

    if state.format in ("json", "csv"):
        _echo_format(results)
    else:
        for i, result in enumerate(results, 1):
            _echo(f"[bold]{i}. {result['query']}[/bold]")
            _render_find_payload(result)
            _echo("")


@app.command()
def search(
    pattern: str = typer.Argument(help="The name pattern to search for."),
    limit: int = typer.Option(
        50, "--limit", help="Maximum number of results to return (default: 50)."
    ),
) -> None:
    """Search for snippets by matching their names."""
    snippets = snippet_search_by_name(state.session, pattern, limit=limit)

    if state.format in ("json", "csv"):
        _echo_format([{"checksum": s.checksum, "names": s.name_list} for s in snippets])
    else:
        found = f"{len(snippets)}+" if len(snippets) >= limit else str(len(snippets))
        _echo(f"[dim]Found {found} snippets matching '{pattern}'.[/dim]")
        if snippets:
            table = Table(title="Search Results", title_style="bold cyan")
            table.add_column("#", style="dim", justify="right")
            table.add_column("Checksum", style="bold")
            table.add_column("Names")
            for i, snippet in enumerate(snippets, 1):
                table.add_row(
                    str(i), snippet.checksum[:12] + "…", ", ".join(snippet.name_list)
                )
            _echo(table)


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
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1)
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
            f"[bold]Snippet 1:[/bold] {s1['names']} [dim]({s1['checksum'][:12]}…)[/dim]\n"
            f"[bold]Snippet 2:[/bold] {s2['names']} [dim]({s2['checksum'][:12]}…)[/dim]",
            title="Snippet Comparison",
            border_style="cyan",
        )
    )

    table = Table(title="Similarity Metrics", title_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row(
        "Jaccard Similarity (Structure)",
        f"[magenta]{comp['jaccard_similarity']:.2f}[/magenta]",
    )
    table.add_row(
        "Levenshtein Score (Code)", f"[yellow]{comp['levenshtein_score']:.2f}[/yellow]"
    )
    table.add_row(
        "Hybrid Score", f"[bold green]{comp['hybrid_score']:.2f}[/bold green]"
    )
    table.add_row("CFG Similarity", f"[blue]{comp['cfg_similarity']:.2f}[/blue]")
    table.add_row(
        "Shared Normalized Tokens", f"[cyan]{comp['shared_normalized_tokens']}[/cyan]"
    )
    _echo(table)

    _echo("")
    diff = list(
        difflib.unified_diff(
            # Both snippets are guaranteed to exist (guarded above).
            snippet_get(state.session, resolved1).code.splitlines(keepends=True),  # type: ignore[union-attr]
            snippet_get(state.session, resolved2).code.splitlines(keepends=True),  # type: ignore[union-attr]
            fromfile=s1["checksum"][:12],
            tofile=s2["checksum"][:12],
            n=3,
        )
    )
    if diff:
        diff_text = "".join(diff)
        syntax = Syntax(diff_text, "diff", theme="monokai", word_wrap=True)
        _echo(Panel(syntax, title="[bold]Code Diff[/bold]", border_style="cyan"))
    else:
        _echo(
            Panel(
                "[italic]Code is identical.[/italic]",
                title="[bold]Code Diff[/bold]",
                border_style="cyan",
            )
        )


@app.command()
def clean() -> None:
    """Clean the LSH cache and vacuum the database."""
    result = db_clean(state.session)
    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        table = Table(
            title="Database and Cache Cleaned",
            show_header=False,
            title_style="bold cyan",
        )
        table.add_column("Key", style="dim")
        table.add_column("Value")
        if result.get("vacuum_success"):
            table.add_row("Database", "[green]Vacuumed successfully[/green]")
        table.add_row("Cache", "[green]Invalidated[/green]")
        table.add_row("Time elapsed", f"{result['time_elapsed']:.4f}s")
        _echo(table)


@app.command()
def merge(
    source: str = typer.Argument(
        help="Path to the source database file, or a full DATABASE_URL "
        "(e.g. duckdb:///file.db, postgresql+pg8000://...)."
    ),
) -> None:
    """Merge snippets from another resembl database into this one."""
    source_path = source
    if "://" not in source:
        source_path = os.path.abspath(source)
        if not os.path.exists(source_path):
            err_console.print(f"[red]Error:[/red] File not found: {source_path}")
            raise typer.Exit(code=1)

    if state.format not in ("json", "csv"):
        _echo(f"Merging from [bold]{source_path}[/bold]...")
    result = db_merge(state.session, source_path)

    if "error" in result:
        err_console.print(f"[red]Error:[/red] {result['error']}")
        raise typer.Exit(code=1)

    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        table = Table(
            title="Merge Complete", show_header=False, title_style="bold cyan"
        )
        table.add_column("Key", style="dim")
        table.add_column("Value")
        table.add_row("Added", f"[green]{result['added']}[/green] new snippets")
        table.add_row(
            "Updated",
            f"[yellow]{result['updated']}[/yellow] snippets (merged names/tags)",
        )
        table.add_row("Skipped", f"[dim]{result['skipped']}[/dim] already present")
        table.add_row("Total in source", str(result["total_source"]))
        table.add_row("Time elapsed", f"{result['time_elapsed']:.4f}s")
        _echo(table)


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
    snippet = snippet_name_add(state.session, resolved, name, quiet=state.quiet)
    if snippet:
        if state.format in ("json", "csv"):
            _echo_format({"checksum": snippet.checksum, "names": snippet.name_list})
        else:
            _echo(
                f"[green]✓[/green] Snippet [bold]{snippet.checksum[:12]}…[/bold] "
                f"now has names: {snippet.name_list}"
            )
    else:
        if state.format in ("json", "csv"):
            _echo_format({"error": "Failed to add name to snippet."})
        elif not state.quiet:
            err_console.print("[red]Error:[/red] Failed to add name to snippet.")
            raise typer.Exit(code=1)


@name_app.command("remove")
def name_remove_cmd(
    checksum: str = typer.Argument(help="The checksum of the snippet."),
    name: str = typer.Argument(help="The name to remove."),
) -> None:
    """Remove a name from a snippet (supports checksum prefixes)."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        raise typer.Exit(code=1)
    snippet = snippet_name_remove(state.session, resolved, name, quiet=state.quiet)
    if snippet:
        if state.format in ("json", "csv"):
            _echo_format({"checksum": snippet.checksum, "names": snippet.name_list})
        else:
            _echo(
                f"[green]✓[/green] Snippet [bold]{snippet.checksum[:12]}…[/bold] "
                f"now has names: {snippet.name_list}"
            )
    else:
        if state.format in ("json", "csv"):
            _echo_format({"error": "Failed to remove name from snippet."})
        elif not state.quiet:
            err_console.print("[red]Error:[/red] Failed to remove name from snippet.")
            raise typer.Exit(code=1)


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
    snippet = snippet_tag_add(state.session, resolved, tag, quiet=state.quiet)
    if snippet:
        if state.format in ("json", "csv"):
            _echo_format({"checksum": snippet.checksum, "tags": snippet.tag_list})
        else:
            _echo(
                f"[green]✓[/green] Snippet [bold]{snippet.checksum[:12]}…[/bold] "
                f"now has tags: {snippet.tag_list}"
            )
    else:
        if state.format in ("json", "csv"):
            _echo_format({"error": "Failed to add tag to snippet."})
        elif not state.quiet:
            err_console.print("[red]Error:[/red] Failed to add tag to snippet.")
            raise typer.Exit(code=1)


@tag_app.command("remove")
def tag_remove_cmd(
    checksum: str = typer.Argument(help="The checksum of the snippet."),
    tag: str = typer.Argument(help="The tag to remove."),
) -> None:
    """Remove a tag from a snippet (supports checksum prefixes)."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        raise typer.Exit(code=1)
    snippet = snippet_tag_remove(state.session, resolved, tag, quiet=state.quiet)
    if snippet:
        if state.format in ("json", "csv"):
            _echo_format({"checksum": snippet.checksum, "tags": snippet.tag_list})
        else:
            _echo(
                f"[green]✓[/green] Snippet [bold]{snippet.checksum[:12]}…[/bold] "
                f"now has tags: {snippet.tag_list}"
            )
    else:
        if state.format in ("json", "csv"):
            _echo_format({"error": "Failed to remove tag from snippet."})
        elif not state.quiet:
            err_console.print("[red]Error:[/red] Failed to remove tag from snippet.")
            raise typer.Exit(code=1)


# --- Collection sub-commands ---


@collection_app.command("create")
def collection_create_cmd(
    name: str = typer.Argument(help="Name for the new collection."),
    description: str = typer.Option(
        "", "--description", "-d", help="Description of the collection."
    ),
) -> None:
    """Create a new snippet collection."""
    try:
        col = collection_create(state.session, name, description)
        _echo(f"[green]✓[/green] Created collection [bold]{col.name}[/bold]")
    except Exception as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(code=1)


@collection_app.command("delete")
def collection_delete_cmd(
    name: str = typer.Argument(help="Name of the collection to delete."),
) -> None:
    """Delete a collection (snippets are kept but unassigned)."""
    if collection_delete(state.session, name, quiet=state.quiet):
        _echo(f"[green]✓[/green] Deleted collection [bold]{name}[/bold]")
    else:
        err_console.print(f"[red]Error:[/red] Collection '{name}' not found.")
        raise typer.Exit(code=1)


@collection_app.command("list")
def collection_list_cmd() -> None:
    """List all collections."""
    cols = collection_list(state.session)
    if not cols:
        _echo("[dim]No collections found.[/dim]")
        return

    if state.format != "table":
        _echo_format(cols)
        return

    table = Table(title="Collections", title_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Snippets", justify="right")
    table.add_column("Created", style="dim")
    for col in cols:
        table.add_row(
            col["name"],
            col["description"],
            str(col["snippet_count"]),
            col["created_at"][:10],
        )
    _echo(table)


@collection_app.command("show")
def collection_show_cmd(
    name: str = typer.Argument(help="Name of the collection to show."),
) -> None:
    """Show all snippets in a collection."""
    from .models import Snippet as SnippetModel  # noqa: F811

    snippets = SnippetModel.get_by_collection(state.session, name)
    if not snippets:
        _echo(f"[dim]No snippets in collection '{name}'.[/dim]")
        return

    if state.format != "table":
        _echo_format(
            [
                {
                    "checksum": s.checksum,
                    "names": s.name_list,
                    "collection": s.collection,
                }
                for s in snippets
            ]
        )
        return

    table = Table(title=f"Collection: {name}", title_style="bold cyan")
    table.add_column("Checksum", style="dim")
    table.add_column("Names", style="bold")
    for s in snippets:
        table.add_row(s.checksum[:12] + "…", ", ".join(s.name_list))
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
    snippet = collection_add_snippet(
        state.session, collection_name, resolved, quiet=state.quiet
    )
    if snippet:
        _echo(
            f"[green]✓[/green] Added [bold]{', '.join(snippet.name_list)}[/bold] to collection [bold]{collection_name}[/bold]"
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
        _echo(
            f"[green]✓[/green] Removed [bold]{', '.join(snippet.name_list)}[/bold] from its collection"
        )
    else:
        if not state.quiet:
            err_console.print(
                "[red]Error:[/red] Failed to remove snippet from collection."
            )
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
    versions = snippet_version_list(state.session, resolved)
    if not versions:
        _echo("[dim]No version history for this snippet.[/dim]")
        return

    if state.format != "table":
        _echo_format(versions)
        return

    table = Table(title="Version History", title_style="bold cyan")
    table.add_column("ID", justify="right")
    table.add_column("Created At")
    for v in versions:
        table.add_row(str(v["id"]), v["created_at"])
    _echo(table)


# --- Config sub-commands ---


@config_app.command("path")
def config_path_cmd() -> None:
    """Show the path to the config file."""
    console.print(config_path_get())


@config_app.command("list")
def config_list_cmd() -> None:
    """List current settings."""
    full_config = load_config()
    if state.format in ("json", "csv"):
        _echo_format(dict(full_config.items()))
    else:
        table = Table(title="Configuration", title_style="bold cyan")
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
    value = load_config().get(key)
    if state.format in ("json", "csv"):
        _echo_format({key: value})
    else:
        _echo(str(value))


@config_app.command("set")
def config_set_cmd(
    key: str = typer.Argument(help="The configuration key to set."),
    value: str = typer.Argument(help="The value to set."),
) -> None:
    """Set a configuration value."""
    if key not in DEFAULTS:
        err_console.print(f"[red]Error:[/red] Invalid configuration key: '{key}'")
        raise typer.Exit(code=1)
    default_value = DEFAULTS[key]
    try:
        typed_value: int | float = type(default_value)(value)
    except (TypeError, ValueError):
        err_console.print(
            f"[red]Error:[/red] Invalid value for '{key}': expected "
            f"{type(default_value).__name__}, got '{value}'."
        )
        raise typer.Exit(code=1)
    new_config = update_config(key, typed_value)
    _echo(f"[green]✓[/green] Set [bold]{key}[/bold] to {new_config[key]}")
    state.config.update(new_config)


@config_app.command("unset")
def config_unset_cmd(
    key: str = typer.Argument(help="The configuration key to unset."),
) -> None:
    """Unset a configuration value."""
    new_config = remove_config_key(key)
    _echo(f"[green]✓[/green] Unset [bold]{key}[/bold], returning to default.")
    state.config.clear()
    state.config.update(new_config)


def main() -> None:
    """Entry point for the resembl command line interface."""
    app()


if __name__ == "__main__":
    main()
