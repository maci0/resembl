"""Command line interface for the resembl tool."""

from __future__ import annotations

import atexit

import glob
import json
import logging
import os
import sys
import time

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlmodel import Session

from .config import (
    DEFAULTS,
    config_path_get,
    load_config,
    remove_config_key,
    update_config,
)
from .core import (
    db_clean,
    db_reindex,
    db_stats,
    snippet_add,
    snippet_compare,
    snippet_delete,
    snippet_export,
    snippet_find_matches,
    snippet_get,
    snippet_list,
    snippet_name_add,
    snippet_name_remove,
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
app.add_typer(config_app, name="config")
app.add_typer(name_app, name="name")


# --- State ---


class State:
    """Shared state for all commands."""

    session: Session
    config: dict
    quiet: bool = False
    no_color: bool = False


state = State()


def _echo(message: object, **kwargs: object) -> None:
    """Print a message unless ``--quiet`` is set."""
    if not state.quiet:
        console.print(message, **kwargs)


def _echo_json(data: object) -> None:
    """Print data as JSON unless ``--quiet`` is set."""
    if not state.quiet:
        console.print_json(json.dumps(data, indent=2))


# --- Callbacks ---


@app.callback()
def app_callback(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress informational output."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Increase output verbosity."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colored output."),
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
    db_create()
    state.session = Session(engine)
    atexit.register(state.session.close)


# --- Snippet commands ---


@app.command()
def add(
    name: str = typer.Argument(help="The name or alias for the snippet."),
    code: str = typer.Argument(help="The assembly code of the snippet."),
    json_output: bool = typer.Option(False, "--json", help="Output results in JSON format."),
) -> None:
    """Add a new snippet or an alias to existing code."""
    snippet = snippet_add(state.session, name, code)
    if snippet:
        if json_output:
            _echo_json({"checksum": snippet.checksum, "names": snippet.name_list})
        else:
            _echo(
                f"[green]✓[/green] Snippet [bold]{snippet.checksum[:12]}…[/bold] "
                f"now has names: {snippet.name_list}"
            )
    else:
        if json_output:
            _echo_json({"error": "Failed to add snippet."})
        else:
            err_console.print("[red]Error:[/red] Snippet could not be added (empty code?).")
            raise typer.Exit(code=1)


@app.command("export")
def export_cmd(
    directory: str = typer.Argument(help="The directory to export snippets to."),
    json_output: bool = typer.Option(False, "--json", help="Output results in JSON format."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Export all snippets to a directory."""
    if not force:
        typer.confirm(
            f"Are you sure you want to export all snippets to '{directory}'?", abort=True
        )

    result = snippet_export(state.session, directory)

    if json_output:
        _echo_json(result)
    else:
        table = Table(title="Export Complete", show_header=False, title_style="bold cyan")
        table.add_column("Key", style="dim")
        table.add_column("Value")
        table.add_row("Snippets exported", str(result["num_exported"]))
        table.add_row("Time elapsed", f"{result['time_elapsed']:.4f}s")
        if result["num_exported"] > 0:
            table.add_row("Avg per snippet", f"{result['avg_time_per_snippet'] * 1000:.4f}ms")
        _echo(table)


@app.command("import")
def import_cmd(
    directory: str = typer.Argument(help="The directory containing .asm or .txt files."),
    json_output: bool = typer.Option(False, "--json", help="Output results in JSON format."),
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

    for file_path in file_paths:
        fname = os.path.splitext(os.path.basename(file_path))[0]
        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()
        existing = snippet_get(state.session, string_checksum(code))
        snippet = snippet_add(state.session, fname, code)
        if snippet and not existing:
            snippets_added += 1

    end_time = time.time()
    time_elapsed = end_time - start_time
    stats = {
        "num_imported": snippets_added,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": (time_elapsed / snippets_added) if snippets_added > 0 else 0,
    }

    if json_output:
        _echo_json(stats)
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
    json_output: bool = typer.Option(False, "--json", help="Output in JSON format."),
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
    if json_output:
        _echo_json([{"checksum": s.checksum, "names": s.name_list} for s in snippets])
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
    checksum: str = typer.Argument(help="The checksum of the snippet."),
    json_output: bool = typer.Option(False, "--json", help="Output in JSON format."),
) -> None:
    """Show detailed information for a specific snippet."""
    snippet = snippet_get(state.session, checksum)
    if not snippet:
        err_console.print(f"[red]Error:[/red] Snippet with checksum {checksum} not found.")
        raise typer.Exit(code=1)

    if json_output:
        _echo_json({"checksum": snippet.checksum, "names": snippet.name_list, "code": snippet.code})
    else:
        _echo(Panel(snippet.code, title=f"[bold]{', '.join(snippet.name_list)}[/bold]", subtitle=snippet.checksum[:16] + "…", border_style="cyan"))


@app.command()
def rm(
    checksum: str = typer.Argument(help="The checksum of the snippet to remove."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Remove a snippet by its checksum."""
    if not force:
        typer.confirm(
            f"Are you sure you want to delete the snippet with checksum '{checksum}'?",
            abort=True,
        )
    if not snippet_delete(state.session, checksum, quiet=state.quiet):
        err_console.print(f"[red]Error:[/red] Snippet with checksum '{checksum}' not found.")
        raise typer.Exit(code=1)


@app.command()
def stats(
    json_output: bool = typer.Option(False, "--json", help="Output in JSON format."),
) -> None:
    """Show database statistics."""
    result = db_stats(state.session)
    if json_output:
        _echo_json(result)
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
    json_output: bool = typer.Option(False, "--json", help="Output in JSON format."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts."),
) -> None:
    """Re-calculate all MinHashes in the database."""
    if not force:
        typer.confirm(
            "Are you sure you want to re-index the entire database? This may take a while.",
            abort=True,
        )

    result = db_reindex(state.session)
    if json_output:
        _echo_json(result)
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
    json_output: bool = typer.Option(False, "--json", help="Output results in JSON format."),
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
        state.session, query_string, effective_top_n, effective_threshold, not no_normalization
    )

    if json_output:
        _echo_json(
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
            table.add_column("Score", justify="right")
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
def compare(
    checksum1: str = typer.Argument(help="The checksum of the first snippet."),
    checksum2: str = typer.Argument(help="The checksum of the second snippet."),
    json_output: bool = typer.Option(False, "--json", help="Output results in JSON format."),
) -> None:
    """Compare two snippets directly."""
    comparison = snippet_compare(state.session, checksum1, checksum2)
    if not comparison:
        err_console.print("[red]Error:[/red] One or both snippets could not be found.")
        raise typer.Exit(code=1)

    if json_output:
        _echo_json(comparison)
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
    table.add_row("Shared Normalized Tokens", f"[cyan]{comp['shared_normalized_tokens']}[/cyan]")
    _echo(table)


@app.command()
def clean(
    json_output: bool = typer.Option(False, "--json", help="Output results in JSON format."),
) -> None:
    """Clean the LSH cache and vacuum the database."""
    result = db_clean(state.session)
    if json_output:
        _echo_json(result)
    else:
        table = Table(title="Database and Cache Cleaned", show_header=False, title_style="bold cyan")
        table.add_column("Key", style="dim")
        table.add_column("Value")
        if result.get("vacuum_success"):
            table.add_row("Database", "[green]Vacuumed successfully[/green]")
        table.add_row("Cache", "[green]Invalidated[/green]")
        table.add_row("Time elapsed", f"{result['time_elapsed']:.4f}s")
        _echo(table)


# --- Name sub-commands ---


@name_app.command("add")
def name_add_cmd(
    checksum: str = typer.Argument(help="The checksum of the snippet."),
    name: str = typer.Argument(help="The new name for the snippet."),
    json_output: bool = typer.Option(False, "--json", help="Output results in JSON format."),
) -> None:
    """Add a new name to a snippet."""
    snippet = snippet_name_add(state.session, checksum, name, quiet=state.quiet)
    if snippet:
        if json_output:
            _echo_json({"checksum": snippet.checksum, "names": snippet.name_list})
        else:
            _echo(
                f"[green]✓[/green] Snippet [bold]{snippet.checksum[:12]}…[/bold] "
                f"now has names: {snippet.name_list}"
            )
    else:
        if json_output:
            _echo_json({"error": "Failed to add name to snippet."})
        elif not state.quiet:
            err_console.print("[red]Error:[/red] Failed to add name to snippet.")
            raise typer.Exit(code=1)


@name_app.command("remove")
def name_remove_cmd(
    checksum: str = typer.Argument(help="The checksum of the snippet."),
    name: str = typer.Argument(help="The name to remove."),
    json_output: bool = typer.Option(False, "--json", help="Output results in JSON format."),
) -> None:
    """Remove a name from a snippet."""
    snippet = snippet_name_remove(state.session, checksum, name, quiet=state.quiet)
    if snippet:
        if json_output:
            _echo_json({"checksum": snippet.checksum, "names": snippet.name_list})
        else:
            _echo(
                f"[green]✓[/green] Snippet [bold]{snippet.checksum[:12]}…[/bold] "
                f"now has names: {snippet.name_list}"
            )
    else:
        if json_output:
            _echo_json({"error": "Failed to remove name from snippet."})
        elif not state.quiet:
            err_console.print("[red]Error:[/red] Failed to remove name from snippet.")
            raise typer.Exit(code=1)


# --- Config sub-commands ---


@config_app.command("path")
def config_path_cmd() -> None:
    """Show the path to the config file."""
    console.print(config_path_get())


@config_app.command("list")
def config_list_cmd() -> None:
    """List current settings."""
    full_config = load_config()
    table = Table(title="Configuration", title_style="bold cyan")
    table.add_column("Key", style="bold")
    table.add_column("Value", justify="right")
    for key, value in full_config.items():
        table.add_row(key, str(value))
    console.print(table)


@config_app.command("get")
def config_get_cmd(
    key: str = typer.Argument(help="The configuration key to get."),
) -> None:
    """Get a configuration value."""
    value = load_config().get(key)
    console.print(value)


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
