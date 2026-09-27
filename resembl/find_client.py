"""Thin client for ``resembl serve`` — instant warm finds.

This module deliberately imports only the standard library so the client
process starts in ~50 ms instead of ~450 ms.  It reads the same
``RESEMBL_DATABASE_URL`` / ``DATABASE_URL`` / ``RESEMBL_CACHE_DIR``
environment variables the CLI uses,
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
import re
import sys
import urllib.error
import urllib.request

_DEFAULT_DB_URL = "sqlite:///assembly.db"
_DEFAULT_CACHE_DIR = "~/.cache/resembl"

#: Mirrors resembl.database.DB_URL_ENV_VARS (this module stays stdlib-only):
#: the namespaced name wins, the unprefixed one is read next, and an empty
#: value counts as unset so a stray ``export DATABASE_URL=`` in a shell
#: profile does not send the client looking for a port file named after "".
_DB_URL_ENV_VARS = ("RESEMBL_DATABASE_URL", "DATABASE_URL")


#: Mirrors resembl.database.db_url_mask (this module stays stdlib-only):
#: hides an embedded password before a URL reaches stderr.
_DB_URL_CREDENTIALS = re.compile(r"(://[^:/?#\s]+:)([^@/\s]+)(@)")


def _mask_db_url(url: str) -> str:
    """Return *url* with any embedded password replaced by ``***``."""
    return _DB_URL_CREDENTIALS.sub(r"\1***\3", url)


def _db_url_get() -> str:
    """Return the database URL from the environment (see ``_DB_URL_ENV_VARS``)."""
    for var in _DB_URL_ENV_VARS:
        url = os.environ.get(var)
        if url:
            return url
    return _DEFAULT_DB_URL


def server_port_path(db_url: str, cache_dir: str) -> str:
    """Return the port-file path for a database URL (matches ``serve``)."""
    digest = hashlib.sha1(db_url.encode("utf-8")).hexdigest()[:12]
    return os.path.join(cache_dir, f"server_{digest}.port")


def _load_config() -> dict:
    """Read the whole CLI config.toml, if present.

    The thin client must produce the same results as `resembl find`, which
    honors these settings — ignoring them would silently change matches.
    Returns the raw table; the caller picks the find keys (``top_n``,
    ``lsh_threshold``, ``ngram_size``, ``num_permutations``,
    ``jaccard_weight``).
    """
    # Mirrors resembl.config.config_dir_get: RESEMBL_CONFIG_DIR wins, then
    # $XDG_CONFIG_HOME (freedesktop base-dir spec), then the historical
    # ~/.config/resembl default.
    override = os.environ.get("RESEMBL_CONFIG_DIR")
    if override:
        config_dir = os.path.expanduser(override)
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        config_dir = (
            os.path.join(xdg, "resembl") if xdg else os.path.expanduser("~/.config/resembl")
        )
    path = os.path.join(config_dir, "config.toml")
    try:
        import tomllib

        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError):
        return {}


#: The find defaults this client falls back to, mirroring
#: ``ResemblConfig`` (resembl.config cannot be imported here: this module is
#: stdlib-only so a warm find starts in ~50 ms).
_CFG_DEFAULTS: dict[str, float] = {
    "top_n": 5,
    "lsh_threshold": 0.5,
    "ngram_size": 3,
    "num_permutations": 128,
    "jaccard_weight": 0.4,
}


def _cfg_number[Num: (int, float)](cfg: dict, key: str, cast: type[Num]) -> Num:
    """Return *key* from *cfg* as *cast*, or the default when it is unusable.

    The config file is hand-editable, so a mistyped value must not take the
    client down: ``int(cfg.get("top_n"))`` on a quoted ``"abc"`` raised an
    uncaught ValueError traceback before the query was ever sent.  The CLI
    handles the same file the same way (warn, keep the default).
    """
    default = _CFG_DEFAULTS[key]
    value = cfg.get(key, default)
    try:
        return cast(value)
    except (TypeError, ValueError):
        print(
            f"error: ignoring config value {key} = {value!r} (not a number)",
            file=sys.stderr,
        )
        return cast(default)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="resembl-find", description="Query a running resembl server."
    )
    parser.add_argument("--query", help="Query string (single-line ';' = separator).")
    parser.add_argument("--file", help="Path to a file containing the query ('-' for stdin).")
    parser.add_argument("--top-n", type=int, default=None)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--no-normalization", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a table.")
    args = parser.parse_args(argv)

    query = args.query
    if query is None and args.file == "-":
        query = sys.stdin.read()
    elif query is None and args.file:
        try:
            with open(args.file, encoding="utf-8") as f:
                query = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
            return 1
    elif query is None and not sys.stdin.isatty():
        # Same piping contract as `resembl find`: with neither --query nor
        # --file, a redirected stdin is the query.
        query = sys.stdin.read()
    if query and ";" in query and "\n" not in query:
        query = query.replace(";", "\n")
    if not query:
        print("error: no query provided (--query, --file, or stdin)", file=sys.stderr)
        return 2

    db_url = _db_url_get()
    # Mirrors resembl.cache.cache_dir_get: RESEMBL_CACHE_DIR wins, then
    # $XDG_CACHE_HOME, then the historical ~/.cache/resembl default.  The
    # port file must resolve to the exact path `resembl serve` wrote.
    cache_override = os.environ.get("RESEMBL_CACHE_DIR")
    if cache_override:
        cache_dir = os.path.expanduser(cache_override)
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        cache_dir = (
            os.path.join(xdg_cache, "resembl")
            if xdg_cache
            else os.path.expanduser(_DEFAULT_CACHE_DIR)
        )
    port_file = server_port_path(db_url, cache_dir)
    try:
        with open(port_file, encoding="utf-8") as f:
            port = int(f.read().strip())
    except (OSError, ValueError):
        print(
            f"error: no server running for {_mask_db_url(db_url)} (start `resembl serve`)",
            file=sys.stderr,
        )
        return 1

    # The fallbacks below must mirror ``ResemblConfig``'s defaults
    # (resembl.config); this client stays stdlib-only and cannot import it.
    cfg = _load_config()
    effective_top_n = args.top_n if args.top_n is not None else _cfg_number(cfg, "top_n", int)
    effective_threshold = (
        args.threshold if args.threshold is not None else _cfg_number(cfg, "lsh_threshold", float)
    )
    effective_ngram = _cfg_number(cfg, "ngram_size", int)
    effective_perm = _cfg_number(cfg, "num_permutations", int)
    effective_jw = _cfg_number(cfg, "jaccard_weight", float)

    body = json.dumps(
        {
            "query": query,
            "top_n": effective_top_n,
            "threshold": effective_threshold,
            "normalize": not args.no_normalization,
            "ngram_size": effective_ngram,
            "num_permutations": effective_perm,
            "jaccard_weight": effective_jw,
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
    except urllib.error.HTTPError as exc:
        # A 4xx answer carries the server's own JSON error envelope: report
        # it verbatim rather than the transport-level "HTTP Error 400", which
        # tells the caller nothing about which parameter was refused.
        try:
            detail = json.loads(exc.read()).get("error", "")
        except (ValueError, OSError, AttributeError):
            detail = ""
        print(f"error: {detail or exc.reason}", file=sys.stderr)
        return 1
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


def main() -> None:
    """Console-script entry point."""
    sys.exit(_main())


if __name__ == "__main__":
    main()
