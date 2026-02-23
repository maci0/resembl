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

import difflib
import glob
import csv
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import track
from rich.syntax import Syntax
from rich.table import Table
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
    collection_add_snippet,
    collection_create,
    collection_delete,
    collection_list,
    collection_remove_snippet,
    db_clean,
    db_merge,
    db_reindex,
    db_stats,
    snippet_add,
    snippet_compare,
    snippet_delete,
    snippet_export,
    snippet_export_yara,
    snippet_find_matches,
    snippet_get,
    snippet_list,
    snippet_name_add,
    snippet_name_remove,
    snippet_search_by_name,
    snippet_tag_add,
    snippet_tag_remove,
    snippet_version_list,
    string_checksum,
)
from .database import db_create, engine

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
collection_app = typer.Typer(help="Manage snippet collections.", rich_markup_mode="rich")
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


def _echo(message: object, **kwargs: object) -> None:
    """Print a message unless ``--quiet`` is set."""
    if not state.quiet:
        console.print(message, **kwargs)


def _echo_format(data: object) -> None:
    """Print data in the requested format (JSON/CSV) unless ``--quiet``."""
    if state.quiet:
        return
    if state.format == "csv":
        import sys
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
             for k, v in data.items():
                 if isinstance(v, list):
                     data[k] = ", ".join(v)
             writer = csv.DictWriter(sys.stdout, fieldnames=data.keys())
             writer.writeheader()
             writer.writerow(data)
        else:
             console.print(json.dumps(data, indent=2))
    else:
        # JSON is the default structured format
        console.print_json(json.dumps(data, indent=2))


def _resolve_checksum(prefix: str) -> str | None:
    """Resolve a checksum prefix to a full checksum.

    If *prefix* matches exactly one snippet, return its full checksum.
    If it matches zero or more than one, print an error and return ``None``.
    """
    from .models import Snippet as SnippetModel
    from sqlmodel import select  # Local import — only needed for prefix LIKE query
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
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress informational output."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Increase output verbosity."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colored output."),
    format_opt: str | None = typer.Option(None, "--format", help="Output format: table, json, csv. Overrides config."),
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
    state.format = format_opt or state.config.get("format", "table")
    db_create()
    state.session = Session(engine)
    atexit.register(state.session.close)


# --- Snippet commands ---


@app.command()
def add(
    name: str = typer.Argument(help="The name or alias for the snippet."),
    code: str = typer.Argument(help="The assembly code of the snippet."),
) -> None:
    """Add a new snippet or an alias to existing code."""
    snippet = snippet_add(state.session, name, code, ngram_size=state.config.get("ngram_size", 3))
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
            err_console.print("[red]Error:[/red] Snippet could not be added (empty code?).")
            raise typer.Exit(code=1)


@app.command("export")
def export_cmd(
    directory: str = typer.Argument(help="The directory to export snippets to."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Export all snippets to a directory."""
    if not force:
        typer.confirm(
            f"Are you sure you want to export all snippets to '{directory}'?", abort=True
        )

    result = snippet_export(state.session, directory)

    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        table = Table(title="Export Complete", show_header=False, title_style="bold cyan")
        table.add_column("Key", style="dim")
        table.add_column("Value")
        table.add_row("Snippets exported", str(result["num_exported"]))
        table.add_row("Time elapsed", f"{result['time_elapsed']:.4f}s")
        if result["num_exported"] > 0:
            table.add_row("Avg per snippet", f"{result['avg_time_per_snippet'] * 1000:.4f}ms")
        _echo(table)


@app.command("export-yara")
def export_yara_cmd(
    output_file: str = typer.Argument(help="The output file to save YARA rules to."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Export snippets as YARA string patterns."""
    if not force:
        typer.confirm(
            f"Are you sure you want to export YARA rules to '{output_file}'?", abort=True
        )

    result = snippet_export_yara(state.session, output_file)

    if state.format in ("json", "csv"):
        _echo_format(result)
        return

    table = Table(title="YARA Export Complete", show_header=False, title_style="bold cyan")
    table.add_column("Key", style="dim")
    table.add_column("Value")
    table.add_row("Rules exported", str(result["num_exported"]))
    table.add_row("Time elapsed", f"{result['time_elapsed']:.4f}s")
    if result["num_exported"] > 0:
        table.add_row("Avg per rule", f"{result['avg_time_per_snippet'] * 1000:.4f}ms")
    _echo(table)


@app.command("import")
def import_cmd(
    directory: str = typer.Argument(help="The directory containing .asm or .txt files."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Bulk import snippets from a directory."""
    if not force:
        typer.confirm(
            f"Are you sure you want to import all snippets from '{directory}'?", abort=True
        )

    start_time = time.time()
    snippets_added: int = 0

    file_paths = glob.glob(os.path.join(directory, "**", "*.asm"), recursive=True)
    file_paths += glob.glob(os.path.join(directory, "**", "*.txt"), recursive=True)
    ngram_size = state.config.get("ngram_size", 3)

    def process_file(file_path: str) -> bool:
        fname = os.path.splitext(os.path.basename(file_path))[0]
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
            with Session(engine) as session:
                checksum = string_checksum(code)
                existing = snippet_get(session, checksum)
                snippet = snippet_add(session, fname, code, ngram_size=ngram_size)
                if snippet and not existing:
                    return True
            return False
        except Exception:
            return False

    if state.quiet or state.format in ("json", "csv"):
        with ThreadPoolExecutor() as executor:
            futures = [executor.submit(process_file, fp) for fp in file_paths]
            for future in as_completed(futures):
                if future.result():
                    snippets_added += 1
    else:
        with ThreadPoolExecutor() as executor:
            futures = [executor.submit(process_file, fp) for fp in file_paths]
            for future in track(
                as_completed(futures),
                total=len(futures),
                description="Importing snippets...",
                console=err_console,
            ):
                if future.result():
                    snippets_added += 1

    end_time = time.time()
    time_elapsed = end_time - start_time
    stats = {
        "num_imported": snippets_added,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": (time_elapsed / snippets_added) if snippets_added > 0 else 0,
    }

    if state.format in ("json", "csv"):
        _echo_format(stats)
    else:
        table = Table(title="Import Complete", show_header=False, title_style="bold cyan")
        table.add_column("Key", style="dim")
        table.add_column("Value")
        table.add_row("Snippets imported", str(stats["num_imported"]))
        table.add_row("Time elapsed", f"{stats['time_elapsed']:.4f}s")
        if stats["num_imported"] > 0:
            table.add_row("Avg per snippet", f"{stats['avg_time_per_snippet'] * 1000:.4f}ms")
        _echo(table)


@app.command("list")
def list_cmd(
    range_str: str | None = typer.Option(None, "--range", help="A range of snippets to list (e.g., 10-30)."),
) -> None:
    """List all snippets."""
    start, end = 0, 0
    if range_str:
        parts = range_str.split("-")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            err_console.print("[red]Error:[/red] Invalid range format. Use start-end (e.g., 10-30).")
            raise typer.Exit(code=1)
        start, end = map(int, parts)

    snippets = snippet_list(state.session, start, end)
    if state.format in ("json", "csv"):
        _echo_format([{"checksum": s.checksum, "names": s.name_list} for s in snippets])
    else:
        table = Table(title="Snippets", title_style="bold cyan")
        table.add_column("#", style="dim", justify="right")
        table.add_column("Checksum", style="bold")
        table.add_column("Names")
        for i, snippet in enumerate(snippets, 1):
            table.add_row(str(i), snippet.checksum[:12] + "…", ", ".join(snippet.name_list))
        _echo(table)


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
        err_console.print(f"[red]Error:[/red] Snippet with checksum {resolved} not found.")
        raise typer.Exit(code=1)

    if state.format in ("json", "csv"):
        _echo_format({"checksum": snippet.checksum, "names": snippet.name_list, "code": snippet.code})
    else:
        syntax = Syntax(snippet.code, "nasm", theme="monokai", word_wrap=True)
        _echo(Panel(syntax, title=f"[bold]{', '.join(snippet.name_list)}[/bold]", subtitle=snippet.checksum[:16] + "…", border_style="cyan"))


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
        err_console.print(f"[red]Error:[/red] Snippet with checksum '{resolved}' not found.")
        raise typer.Exit(code=1)


@app.command()
def stats(
) -> None:
    """Show database statistics."""
    result = db_stats(state.session)
    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        table = Table(title="Database Statistics", show_header=False, title_style="bold cyan")
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")
        table.add_row("Number of snippets", str(result["num_snippets"]))
        table.add_row("Avg snippet size", f"{result['avg_snippet_size']:.2f} chars")
        table.add_row("Vocabulary size", f"{result['vocabulary_size']} tokens")
        table.add_row("Avg Jaccard similarity", f"{result['avg_jaccard_similarity']:.2f}")
        _echo(table)


@app.command()
def reindex(
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Re-calculate all MinHashes in the database."""
    if not force:
        typer.confirm(
            "Are you sure you want to re-index the entire database? This may take a while.",
            abort=True,
        )

    result = db_reindex(state.session, ngram_size=state.config.get("ngram_size", 3))
    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        table = Table(title="Re-indexing Complete", show_header=False, title_style="bold cyan")
        table.add_column("Key", style="dim")
        table.add_column("Value")
        table.add_row("Snippets re-indexed", str(result["num_reindexed"]))
        table.add_row("Time elapsed", f"{result['time_elapsed']:.4f}s")
        if result["num_reindexed"] > 0:
            table.add_row("Avg per snippet", f"{result['avg_time_per_snippet'] * 1000:.4f}ms")
        _echo(table)


@app.command()
def find(
    query: str | None = typer.Option(None, "--query", help="The query string to search for."),
    file: typer.FileText | None = typer.Option(None, "--file", help="Path to a file containing the query. Use '-' for stdin."),
    top_n: int | None = typer.Option(None, "--top-n", help="Number of top matches to return."),
    threshold: float | None = typer.Option(None, "--threshold", help="LSH threshold override (0.0-1.0)."),
    no_normalization: bool = typer.Option(False, "--no-normalization", help="Disable token normalization for this query."),
) -> None:
    """Find similar snippets."""
    effective_top_n = top_n if top_n is not None else state.config.get("top_n", 5)
    effective_threshold = threshold if threshold is not None else state.config.get("lsh_threshold", 0.5)

    if not 0.0 <= effective_threshold < 0.99:
        err_console.print("[red]Error:[/red] --threshold must be between 0.0 and 0.99 (exclusive).")
        raise typer.Exit(code=1)

    query_string: str | None = None
    if query:
        query_string = query
    elif file:
        query_string = file.read()

    if not query_string:
        err_console.print("[red]Error:[/red] No query provided. Use --query, --file, or stdin.")
        raise typer.Exit(code=1)

    num_candidates, matches = snippet_find_matches(
        state.session, query_string, effective_top_n, effective_threshold, not no_normalization, ngram_size=state.config.get("ngram_size", 3)
    )

    if state.format in ("json", "csv"):
        _echo_format(
            {
                "lsh_candidates": num_candidates,
                "matches": [
                    {"checksum": s.checksum, "names": s.name_list, "score": score}
                    for s, score in matches
                ],
            }
        )
    else:
        _echo(f"[dim]Found {num_candidates} candidates via LSH.[/dim]")
        if matches:
            table = Table(title="Top Matches", title_style="bold cyan")
            table.add_column("#", style="dim", justify="right")
            table.add_column("Checksum", style="bold")
            table.add_column("Names")
            table.add_column("Score (Hybrid)", justify="right")
            for i, (s, score) in enumerate(matches, 1):
                score_color = "green" if score >= 80 else "yellow" if score >= 50 else "red"
                table.add_row(
                    str(i),
                    s.checksum[:12] + "…",
                    ", ".join(s.name_list),
                    f"[{score_color}]{score:.2f}[/{score_color}]",
                )
            _echo(table)
        else:
            _echo("[yellow]No matches found after ranking.[/yellow]")


@app.command()
def search(
    pattern: str = typer.Argument(help="The name pattern to search for."),
) -> None:
    """Search for snippets by matching their names."""
    snippets = snippet_search_by_name(state.session, pattern)

    if state.format in ("json", "csv"):
        _echo_format([{"checksum": s.checksum, "names": s.name_list} for s in snippets])
    else:
        _echo(f"[dim]Found {len(snippets)} snippets matching '{pattern}'.[/dim]")
        if snippets:
            table = Table(title="Search Results", title_style="bold cyan")
            table.add_column("#", style="dim", justify="right")
            table.add_column("Checksum", style="bold")
            table.add_column("Names")
            for i, snippet in enumerate(snippets, 1):
                table.add_row(str(i), snippet.checksum[:12] + "…", ", ".join(snippet.name_list))
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

    comparison = snippet_compare(state.session, resolved1, resolved2)
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
    table.add_row("Jaccard Similarity (Structure)", f"[magenta]{comp['jaccard_similarity']:.2f}[/magenta]")
    table.add_row("Levenshtein Score (Code)", f"[yellow]{comp['levenshtein_score']:.2f}[/yellow]")
    table.add_row("Hybrid Score", f"[bold green]{comp['hybrid_score']:.2f}[/bold green]")
    table.add_row("CFG Similarity", f"[blue]{comp['cfg_similarity']:.2f}[/blue]")
    table.add_row("Shared Normalized Tokens", f"[cyan]{comp['shared_normalized_tokens']}[/cyan]")
    _echo(table)

    _echo("")
    diff = list(
        difflib.unified_diff(
            snippet_get(state.session, resolved1).code.splitlines(keepends=True),
            snippet_get(state.session, resolved2).code.splitlines(keepends=True),
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
        _echo(Panel("[italic]Code is identical.[/italic]", title="[bold]Code Diff[/bold]", border_style="cyan"))


@app.command()
def clean(
) -> None:
    """Clean the LSH cache and vacuum the database."""
    result = db_clean(state.session)
    if state.format in ("json", "csv"):
        _echo_format(result)
    else:
        table = Table(title="Database and Cache Cleaned", show_header=False, title_style="bold cyan")
        table.add_column("Key", style="dim")
        table.add_column("Value")
        if result.get("vacuum_success"):
            table.add_row("Database", "[green]Vacuumed successfully[/green]")
        table.add_row("Cache", "[green]Invalidated[/green]")
        table.add_row("Time elapsed", f"{result['time_elapsed']:.4f}s")
        _echo(table)


@app.command()
def merge(
    source: str = typer.Argument(help="Path to the source resembl database file."),
) -> None:
    """Merge snippets from another resembl database into this one."""
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
        table = Table(title="Merge Complete", show_header=False, title_style="bold cyan")
        table.add_column("Key", style="dim")
        table.add_column("Value")
        table.add_row("Added", f"[green]{result['added']}[/green] new snippets")
        table.add_row("Updated", f"[yellow]{result['updated']}[/yellow] snippets (merged names/tags)")
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
    description: str = typer.Option("", "--description", "-d", help="Description of the collection."),
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
        table.add_row(col["name"], col["description"], str(col["snippet_count"]), col["created_at"][:10])
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
        _echo_format([{"checksum": s.checksum, "names": s.name_list, "collection": s.collection} for s in snippets])
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
        return
    snippet = collection_add_snippet(state.session, collection_name, resolved, quiet=state.quiet)
    if snippet:
        _echo(f"[green]✓[/green] Added [bold]{', '.join(snippet.name_list)}[/bold] to collection [bold]{collection_name}[/bold]")
    else:
        if not state.quiet:
            err_console.print("[red]Error:[/red] Failed to add snippet to collection.")
        raise typer.Exit(code=1)


@collection_app.command("remove")
def collection_remove_cmd(
    checksum: str = typer.Argument(help="Checksum (or prefix) of the snippet to remove from its collection."),
) -> None:
    """Remove a snippet from its collection."""
    resolved = _resolve_checksum(checksum)
    if not resolved:
        return
    snippet = collection_remove_snippet(state.session, resolved, quiet=state.quiet)
    if snippet:
        _echo(f"[green]✓[/green] Removed [bold]{', '.join(snippet.name_list)}[/bold] from its collection")
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
        return
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
    typed_value: int | float = type(default_value)(value)
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
