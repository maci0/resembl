"""clispec 0.3 documents for the resembl CLIs.

Nothing here reads config, credentials, or the network. The version is the
installed distribution version.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

_RETRY = (
    " No idempotency key is accepted, so a retry is not a safe substitute for"
    " knowing whether the first run finished."
)

_ITEMS: list[dict[str, Any]] = [
    {"name": "items", "type": "array", "items": {"type": "object"}},
    {"name": "total", "type": "integer"},
]


def _package_version() -> str:
    try:
        return version("resembl")
    except PackageNotFoundError:
        return "unknown"


def _command(
    name: str,
    description: str,
    effects: str,
    *,
    collection: bool = False,
    confirmation: str | None = None,
) -> dict[str, Any]:
    text = description + _RETRY if effects == "non_idempotent" else description
    spec: dict[str, Any] = {
        "name": name,
        "description": text,
        "effects": effects,
        "output_kind": "data",
    }
    if collection:
        spec["cardinality"] = "bounded"
        spec["output_fields"] = _ITEMS
    else:
        spec["cardinality"] = "single"
        spec["stdout_schema"] = {}
    if confirmation is not None:
        spec["confirmation_bypass_arg"] = confirmation
    return spec


# Invocable commands only. Group names (config, name, tag, collection) are not entries.
_COMMANDS: tuple[dict[str, Any], ...] = (
    _command(
        "serve",
        "Serve find queries from one warm process until the process is stopped.",
        "non_idempotent",
    ),
    _command(
        "add",
        "Store one snippet, or add an alias when that code is already stored.",
        "non_idempotent",
    ),
    _command(
        "export",
        "Write every snippet into a directory.",
        "non_idempotent",
        confirmation="--force",
    ),
    _command(
        "export-yara",
        "Write snippets as YARA string patterns.",
        "non_idempotent",
        confirmation="--force",
    ),
    _command(
        "import",
        "Load snippets from a file or a directory of assembly files.",
        "non_idempotent",
        confirmation="--force",
    ),
    _command(
        "list",
        "List snippets. The document is the whole finite set, or the --range window.",
        "read_only",
        collection=True,
    ),
    _command(
        "show",
        "Show one snippet by checksum or unique prefix.",
        "read_only",
    ),
    _command(
        "rm",
        "Remove one snippet by checksum or unique prefix.",
        "non_idempotent",
        confirmation="--force",
    ),
    _command("stats", "Show database statistics.", "read_only"),
    _command(
        "verify",
        "Report index health. When issues are present the failure code is used"
        " and the report is still printed.",
        "read_only",
    ),
    _command(
        "reindex",
        "Recompute every MinHash in the database.",
        "non_idempotent",
        confirmation="--force",
    ),
    _command(
        "find",
        "Find snippets similar to one query.",
        "read_only",
    ),
    _command(
        "find-batch",
        "Find snippets for each query in a file.",
        "read_only",
        collection=True,
    ),
    _command(
        "search",
        "Search snippet names. --limit caps how many rows are returned.",
        "read_only",
        collection=True,
    ),
    _command("compare", "Compare two snippets.", "read_only"),
    _command("clean", "Drop the LSH index and vacuum the database.", "non_idempotent"),
    _command(
        "merge",
        "Copy snippets from another resembl database into this one.",
        "non_idempotent",
    ),
    _command("name add", "Attach one more name to a snippet.", "non_idempotent"),
    _command("name remove", "Detach one name from a snippet.", "non_idempotent"),
    _command("tag add", "Attach one tag to a snippet.", "non_idempotent"),
    _command("tag remove", "Detach one tag from a snippet.", "non_idempotent"),
    _command("collection create", "Create an empty collection.", "non_idempotent"),
    _command("collection delete", "Delete a collection.", "non_idempotent"),
    _command(
        "collection list",
        "List collections.",
        "read_only",
        collection=True,
    ),
    _command(
        "collection show",
        "List the snippets in one collection.",
        "read_only",
        collection=True,
    ),
    _command(
        "collection add",
        "Put a snippet into a collection.",
        "non_idempotent",
    ),
    _command(
        "collection remove",
        "Take a snippet out of its collection.",
        "non_idempotent",
    ),
    _command(
        "version",
        "Show the recorded history of one snippet.",
        "read_only",
        collection=True,
    ),
    _command("config path", "Print the config file path.", "read_only"),
    _command("config list", "Print the current settings.", "read_only"),
    _command("config get", "Print one setting.", "read_only"),
    _command("config set", "Change one setting.", "non_idempotent"),
    _command("config unset", "Return one setting to its default.", "non_idempotent"),
    _command("schema", "Print the clispec 0.3 JSON document for this CLI.", "read_only"),
)

_FIND_COMMANDS: tuple[dict[str, Any], ...] = (
    _command(
        "query",
        "Query a running resembl server. This is the default invocation:"
        " resembl-find takes the query flags directly and does not require the word query.",
        "read_only",
    ),
    _command(
        "schema",
        "Print the clispec 0.3 JSON document for resembl-find.",
        "read_only",
    ),
)


def _document(
    name: str,
    commands: tuple[dict[str, Any], ...],
    path: str | None,
    *,
    description: str,
    global_args: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    selected = list(commands)
    if path:
        selected = [
            command
            for command in selected
            if command["name"] == path or command["name"].startswith(path + " ")
        ]
    return {
        "clispec": "0.3",
        "name": name,
        "version": _package_version(),
        "description": description,
        "output": {"tty": "text", "piped": "text"},
        "global_args": global_args,
        "commands": selected,
        "errors": errors,
    }


_RESEMBL_ARGS: list[dict[str, Any]] = [
    {
        "name": "--output",
        "short": "-o",
        "type": "string",
        "enum": ["auto", "text", "json"],
        "default": "auto",
        "description": (
            "Format before the subcommand. auto stays text."
            " --format still selects table, json, or csv for the data document."
        ),
    },
    {
        "name": "--format",
        "type": "string",
        "enum": ["table", "json", "csv"],
        "description": "Data format. Overrides the config file. Independent of -o.",
    },
    {
        "name": "--quiet",
        "short": "-q",
        "type": "boolean",
        "description": "Suppress informational output.",
    },
    {
        "name": "--verbose",
        "short": "-v",
        "type": "boolean",
        "description": "Increase output verbosity.",
    },
    {"name": "--no-color", "type": "boolean", "description": "Disable colored output."},
    {"name": "--version", "type": "boolean", "description": "Show the resembl version and exit."},
    {"name": "--tz", "description": "IANA zone for printed timestamps.", "type": "string"},
]

_RESEMBL_ERRORS: list[dict[str, Any]] = [
    {
        "kind": "usage",
        "exit_code": 2,
        "retryable": False,
        "description": "The arguments are not a valid invocation.",
    },
    {
        "kind": "failure",
        "exit_code": 1,
        "retryable": False,
        "description": (
            "The command could not finish. verify uses this same code when"
            " the report contains issues, and still prints the report."
        ),
    },
    {
        "kind": "confirmation_required",
        "exit_code": 1,
        "retryable": False,
        "description": (
            "export, export-yara, import, rm, and reindex need a terminal"
            " to confirm. Pass --force to proceed without a prompt."
        ),
    },
]

_FIND_ARGS: list[dict[str, Any]] = [
    {
        "name": "--output",
        "short": "-o",
        "type": "string",
        "enum": ["auto", "text", "json"],
        "default": "auto",
        "description": "Format. auto stays text. --json remains a synonym for json.",
    },
    {
        "name": "--json",
        "type": "boolean",
        "description": "Print the result as JSON. Same as -o json. -o text wins.",
    },
    {
        "name": "--query",
        "type": "string",
        "description": "Query string. A single line uses ';' as a separator.",
    },
    {
        "name": "--file",
        "type": "path",
        "description": "File containing the query. '-' reads stdin.",
    },
    {"name": "--top-n", "type": "integer", "description": "How many matches to return."},
    {"name": "--threshold", "type": "number", "description": "LSH threshold override."},
    {
        "name": "--no-normalization",
        "type": "boolean",
        "description": "Do not fold registers and immediates.",
    },
]

_FIND_ERRORS: list[dict[str, Any]] = [
    {
        "kind": "usage",
        "exit_code": 2,
        "retryable": False,
        "description": "No query was provided.",
    },
    {
        "kind": "failure",
        "exit_code": 1,
        "retryable": False,
        "description": "The query file or the running server could not be used.",
    },
]


def build_schema(path: str | None = None) -> dict[str, Any]:
    """The clispec 0.3 document for ``resembl``, optionally filtered."""
    return _document(
        "resembl",
        _COMMANDS,
        path,
        description="Assembly code similarity search.",
        global_args=_RESEMBL_ARGS,
        errors=_RESEMBL_ERRORS,
    )


def build_find_schema(path: str | None = None) -> dict[str, Any]:
    """The clispec 0.3 document for ``resembl-find``, optionally filtered."""
    return _document(
        "resembl-find",
        _FIND_COMMANDS,
        path,
        description="Thin client for a running resembl server.",
        global_args=_FIND_ARGS,
        errors=_FIND_ERRORS,
    )
