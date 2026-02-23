"""Top-level package for the resembl assembly code similarity library.

resembl can be used both as a CLI tool (``resembl``) and as a Python library::

    from resembl import snippet_add, snippet_find_matches, code_tokenize
"""

from .core import (
    code_create_minhash,
    code_create_minhash_batch,
    code_tokenize,
    snippet_add,
    snippet_compare,
    snippet_delete,
    snippet_find_matches,
    snippet_get,
    snippet_list,
    string_checksum,
    string_normalize,
)
from .models import Collection, Snippet, SnippetVersion

__all__ = [
    "code_create_minhash",
    "code_create_minhash_batch",
    "code_tokenize",
    "snippet_add",
    "snippet_compare",
    "snippet_delete",
    "snippet_find_matches",
    "snippet_get",
    "snippet_list",
    "string_checksum",
    "string_normalize",
    "Collection",
    "Snippet",
    "SnippetVersion",
]
