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

# --- Typer apps ---

app = typer.Typer(
    help="A CLI for finding similar assembly code snippets.",
    add_completion=False,
)
config_app = typer.Typer(help="Manage user configuration.")
name_app = typer.Typer(help="Manage snippet names.")
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


def _echo(message: str) -> None:
    """Print a message unless ``--quiet`` is set."""
    if not state.quiet:
        typer.echo(message)


def _echo_json(data: object) -> None:
    """Print data as JSON unless ``--quiet`` is set."""
    if not state.quiet:
        typer.echo(json.dumps(data, indent=2))


# --- Callbacks ---


@app.callback()
def app_callback(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress informational output."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Increase output verbosity."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colored output."),
) -> None:
    """Set up logging and shared state."""
    state.quiet = quiet
    state.no_color = no_color

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
            _echo(f"Snippet with checksum {snippet.checksum} now has names: {snippet.name_list}")
    else:
        if json_output:
            _echo_json({"error": "Failed to add snippet."})
        else:
            typer.echo("Error: Snippet could not be added (empty code?).", err=True)
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

    stats = snippet_export(state.session, directory)

    if json_output:
        _echo_json(stats)
    else:
        _echo("--- Export Complete ---")
        _echo(f"  Snippets exported: {stats['num_exported']}")
        _echo(f"  Total time elapsed: {stats['time_elapsed']:.4f} seconds")
        if stats["num_exported"] > 0:
            _echo(f"  Average time per snippet: {stats['avg_time_per_snippet'] * 1000:.4f} ms")


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
        _echo("--- Import Complete ---")
        _echo(f"  Snippets imported: {stats['num_imported']}")
        _echo(f"  Total time elapsed: {stats['time_elapsed']:.4f} seconds")
        if stats["num_imported"] > 0:
            _echo(f"  Average time per snippet: {stats['avg_time_per_snippet'] * 1000:.4f} ms")


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
            typer.echo("Error: Invalid range format. Use start-end (e.g., 10-30).", err=True)
            raise typer.Exit(code=1)
        start, end = map(int, parts)

    snippets = snippet_list(state.session, start, end)
    if json_output:
        _echo_json([{"checksum": s.checksum, "names": s.name_list} for s in snippets])
    else:
        for snippet in snippets:
            _echo(f"Checksum: {snippet.checksum}, Names: {snippet.name_list}")


@app.command()
def show(
    checksum: str = typer.Argument(help="The checksum of the snippet."),
    json_output: bool = typer.Option(False, "--json", help="Output in JSON format."),
) -> None:
    """Show detailed information for a specific snippet."""
    snippet = snippet_get(state.session, checksum)
    if not snippet:
        typer.echo(f"Snippet with checksum {checksum} not found.", err=True)
        raise typer.Exit(code=1)

    if json_output:
        _echo_json({"checksum": snippet.checksum, "names": snippet.name_list, "code": snippet.code})
    else:
        _echo(f"Checksum: {snippet.checksum}")
        _echo(f"Names: {snippet.name_list}")
        _echo(f"Code:\n{snippet.code}")


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
        typer.echo(f"Error: Snippet with checksum '{checksum}' not found.", err=True)
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
        _echo("--- Database Statistics ---")
        _echo(f"  Number of snippets: {result['num_snippets']}")
        _echo(f"  Average snippet size: {result['avg_snippet_size']:.2f} characters")
        _echo(f"  Vocabulary size: {result['vocabulary_size']} unique tokens")
        _echo(f"  Average Jaccard similarity (sample): {result['avg_jaccard_similarity']:.2f}")


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
        _echo("--- Re-indexing Complete ---")
        _echo(f"  Snippets re-indexed: {result['num_reindexed']}")
        _echo(f"  Total time elapsed: {result['time_elapsed']:.4f} seconds")
        if result["num_reindexed"] > 0:
            _echo(f"  Average time per snippet: {result['avg_time_per_snippet'] * 1000:.4f} ms")


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
        typer.echo("Error: --threshold must be between 0.0 and 0.99 (exclusive).", err=True)
        raise typer.Exit(code=1)

    query_string: str | None = None
    if query:
        query_string = query
    elif file:
        query_string = file.read()

    if not query_string:
        typer.echo("Error: No query provided. Use --query, --file, or stdin.", err=True)
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
        _echo(f"Found {num_candidates} candidates via LSH.")
        if matches:
            _echo("--- Top Matches ---")
            for s, score in matches:
                _echo(f"  - Checksum: {s.checksum}, Names: {s.name_list}, Score: {score:.2f}")
        else:
            _echo("No matches found after ranking.")


@app.command()
def compare(
    checksum1: str = typer.Argument(help="The checksum of the first snippet."),
    checksum2: str = typer.Argument(help="The checksum of the second snippet."),
    json_output: bool = typer.Option(False, "--json", help="Output results in JSON format."),
) -> None:
    """Compare two snippets directly."""
    comparison = snippet_compare(state.session, checksum1, checksum2)
    if not comparison:
        typer.echo("Error: One or both snippets could not be found.", err=True)
        raise typer.Exit(code=1)

    if json_output:
        _echo_json(comparison)
        return

    s1 = comparison["snippet1"]
    s2 = comparison["snippet2"]
    comp = comparison["comparison"]

    def format_output(label: str, value: str, color: str = "") -> str:
        if state.no_color or not color:
            return f"{label}{value}"
        return f"\033[{color}m{label}{value}\033[0m"

    _echo("--- Snippet Comparison ---")
    _echo(format_output("Snippet 1: ", f"{s1['names']} ({s1['checksum'][:12]}...)", "1"))
    _echo(format_output("Snippet 2: ", f"{s2['names']} ({s2['checksum'][:12]}...)", "1"))

    _echo("\n--- Similarity Metrics ---")
    _echo(format_output("  Jaccard Similarity (Structure): ", f"{comp['jaccard_similarity']:.2f}", "92"))
    _echo(format_output("  Levenshtein Score (Code):       ", f"{comp['levenshtein_score']:.2f}", "93"))
    _echo(format_output("  Shared Normalized Tokens:       ", str(comp["shared_normalized_tokens"]), "94"))


@app.command()
def clean(
    json_output: bool = typer.Option(False, "--json", help="Output results in JSON format."),
) -> None:
    """Clean the LSH cache and vacuum the database."""
    result = db_clean(state.session)
    if json_output:
        _echo_json(result)
    else:
        _echo("--- Database and Cache Cleaned ---")
        if result.get("vacuum_success"):
            _echo("  Database vacuumed successfully.")
        _echo("  Cache invalidated.")
        _echo(f"  Total time elapsed: {result['time_elapsed']:.4f} seconds")


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
        if json_output:
            _echo_json({"error": "Failed to add name to snippet."})
        elif not state.quiet:
            typer.echo("Error: Failed to add name to snippet.", err=True)
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
        if json_output:
            _echo_json({"error": "Failed to remove name from snippet."})
        elif not state.quiet:
            typer.echo("Error: Failed to remove name from snippet.", err=True)
            raise typer.Exit(code=1)


# --- Config sub-commands ---


@config_app.command("path")
def config_path_cmd() -> None:
    """Show the path to the config file."""
    typer.echo(config_path_get())


@config_app.command("list")
def config_list_cmd() -> None:
    """List current settings."""
    full_config = load_config()
    for key, value in full_config.items():
        typer.echo(f"{key} = {value}")


@config_app.command("get")
def config_get_cmd(
    key: str = typer.Argument(help="The configuration key to get."),
) -> None:
    """Get a configuration value."""
    value = load_config().get(key)
    typer.echo(value)


@config_app.command("set")
def config_set_cmd(
    key: str = typer.Argument(help="The configuration key to set."),
    value: str = typer.Argument(help="The value to set."),
) -> None:
    """Set a configuration value."""
    if key not in DEFAULTS:
        typer.echo(f"Invalid configuration key: '{key}'", err=True)
        raise typer.Exit(code=1)
    default_value = DEFAULTS[key]
    typed_value: int | float = type(default_value)(value)
    new_config = update_config(key, typed_value)
    _echo(f"Set {key} to {new_config[key]}")
    state.config.update(new_config)


@config_app.command("unset")
def config_unset_cmd(
    key: str = typer.Argument(help="The configuration key to unset."),
) -> None:
    """Unset a configuration value."""
    new_config = remove_config_key(key)
    _echo(f"Unset {key}, returning to default.")
    state.config.clear()
    state.config.update(new_config)


def main() -> None:
    """Entry point for the resembl command line interface."""
    app()


if __name__ == "__main__":
    main()
