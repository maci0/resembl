"""Thin client for ``resembl serve`` — instant warm finds.

This module deliberately imports only the standard library so the client
process starts in ~50 ms instead of ~450 ms.  It reads the same
``DATABASE_URL`` / ``RESEMBL_CACHE_DIR`` environment variables the CLI uses,
locates the port file written by ``resembl serve``, and POSTs the query.

Usage::

    resembl serve            # once, in another terminal
    python -m resembl.find_client --query "push ebx; ret"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

_DEFAULT_DB_URL = "sqlite:///assembly.db"
_DEFAULT_CACHE_DIR = "~/.cache/resembl"


def server_port_path(db_url: str, cache_dir: str) -> str:
    """Return the port-file path for a database URL (matches ``serve``)."""
    digest = hashlib.sha1(db_url.encode("utf-8")).hexdigest()[:12]
    return os.path.join(cache_dir, f"server_{digest}.port")


def _load_config() -> dict:
    """Read the CLI config.toml (lsh_threshold / ngram_size), if present.

    The thin client must produce the same results as `resembl find`, which
    honors these settings — ignoring them would silently change matches.
    """
    config_dir = os.environ.get("RESEMBL_CONFIG_DIR") or os.path.expanduser(
        "~/.config/resembl"
    )
    path = os.path.join(config_dir, "config.toml")
    try:
        import tomllib

        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError, ImportError):
        return {}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="resembl-find", description="Query a running resembl server."
    )
    parser.add_argument("--query", help="Query string (single-line ';' = separator).")
    parser.add_argument("--file", help="Path to a file containing the query.")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--no-normalization", action="store_true")
    parser.add_argument(
        "--json", action="store_true", help="Print JSON instead of a table."
    )
    args = parser.parse_args(argv)

    query = args.query
    if query and ";" in query and "\n" not in query:
        query = query.replace(";", "\n")
    if query is None and args.file:
        with open(args.file, encoding="utf-8") as f:
            query = f.read()
    if not query:
        print("error: no query provided (--query or --file)", file=sys.stderr)
        return 2

    db_url = os.environ.get("DATABASE_URL", _DEFAULT_DB_URL)
    cache_dir = os.path.expanduser(
        os.environ.get("RESEMBL_CACHE_DIR", _DEFAULT_CACHE_DIR)
    )
    port_file = server_port_path(db_url, cache_dir)
    try:
        with open(port_file, encoding="utf-8") as f:
            port = int(f.read().strip())
    except (OSError, ValueError):
        print(
            f"error: no server running for {db_url} (start `resembl serve`)",
            file=sys.stderr,
        )
        return 1

    cfg = _load_config()
    effective_threshold = (
        args.threshold if args.threshold is not None else cfg.get("lsh_threshold")
    )
    effective_ngram = int(cfg.get("ngram_size", 3))
    effective_perm = int(cfg.get("num_permutations", 128))

    body = json.dumps(
        {
            "query": query,
            "top_n": args.top_n,
            "threshold": effective_threshold,
            "normalize": not args.no_normalization,
            "ngram_size": effective_ngram,
            "num_permutations": effective_perm,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/find",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"error: server unreachable: {exc}", file=sys.stderr)
        return 1
    if "error" in payload:
        print(f"error: {payload['error']}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload))
        return 0

    print(f"Found {payload['lsh_candidates']} candidates via LSH.")
    for i, match in enumerate(payload["matches"], 1):
        names = ", ".join(match["names"])
        print(f"{i}. {match['checksum'][:12]}…  {names}  {match['score']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())


def main() -> None:
    """Console-script entry point."""
    sys.exit(_main())
