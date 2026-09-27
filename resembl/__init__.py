"""Top-level package for the resembl assembly code similarity library.

resembl can be used both as a CLI tool (``resembl``) and as a Python library::

    from resembl import snippet_add, snippet_find_matches, code_tokenize

The package is import-light: ``import resembl`` does not eagerly import the
heavy dependencies (pygments, sqlmodel, numpy).  Exports are resolved
lazily via ``__getattr__`` (PEP 562), which keeps the thin ``resembl-find``
client and any import-time-sensitive consumer fast.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "Collection",
    "Snippet",
    "SnippetVersion",
    "code_create_minhash",
    "code_create_minhash_batch",
    "code_tokenize",
    "snippet_add",
    "snippet_add_batch",
    "snippet_compare",
    "snippet_delete",
    "snippet_find_matches",
    "snippet_get",
    "snippet_list",
    "snippet_prepare",
    "string_checksum",
    "string_normalize",
]

CORE_EXPORTS = frozenset(
    (
        "code_create_minhash",
        "code_create_minhash_batch",
        "code_tokenize",
        "snippet_add",
        "snippet_add_batch",
        "snippet_compare",
        "snippet_delete",
        "snippet_find_matches",
        "snippet_get",
        "snippet_list",
        "snippet_prepare",
        "string_checksum",
        "string_normalize",
    )
)
MODEL_EXPORTS = frozenset(("Collection", "Snippet", "SnippetVersion"))
#: Every importable module in the package.  Listing all of them is what makes
#: ``resembl.<name>`` resolve after a bare ``import resembl``; an omission is
#: a silently missing attribute rather than an import error.
SUBMODULES = frozenset(
    (
        "cache",
        "cli",
        "config",
        "core",
        "database",
        "diagnostics",
        "find_client",
        "lsh",
        "minhash",
        "models",
        "paths",
        "scoring",
        "server",
        "theme",
    )
)


def __getattr__(name: str) -> Any:
    """Resolve public exports (and submodules) lazily."""
    import importlib

    if name in CORE_EXPORTS:
        return getattr(importlib.import_module(".core", __name__), name)
    if name in MODEL_EXPORTS:
        return getattr(importlib.import_module(".models", __name__), name)
    if name in SUBMODULES:
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
