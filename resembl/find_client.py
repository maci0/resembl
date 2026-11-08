"""Thin client for ``resembl serve`` — instant warm finds.

This module deliberately imports nothing but the standard library (and
:mod:`resembl.paths`, which is itself standard-library-only) so the client
process starts in ~50 ms instead of ~450 ms.  It reads the same
``RESEMBL_DATABASE_URL`` / ``DATABASE_URL`` / ``RESEMBL_CACHE_DIR``
environment variables the CLI uses, locates the port file written by
``resembl serve``, and POSTs the query.

Usage::

    resembl serve            # once, in another terminal
    python -m resembl.find_client --query "push ebx; ret"
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tomllib
import urllib.error
import urllib.request
from typing import NoReturn

from .paths import (
    cache_dir_get,
    config_path_get,
    console_utf8_reconfigure,
    db_url_get,
    db_url_mask,
    server_port_path,
)

#: The find defaults this client falls back to, mirroring
#: ``ResemblConfig`` (resembl.config cannot be imported here: it pulls in
#: ``tomli_w`` and the scoring module, which this client must not pay for).
#: Hard ceiling on a find response the client will hold in memory.  The
#: server's own result payloads are bounded by its body cap (8 MiB for the
#: request, and the response is the matches list); 16 MiB is the documented
#: ceiling — a larger body is a misbehaving or hostile endpoint, not a
#: triage result, and reading it unbounded would let a local attacker pin
#: gigabytes via a port-file swap (see _read_bounded_response).
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _read_bounded_response(response: object, limit: int = _MAX_RESPONSE_BYTES) -> bytes:
    """Read up to *limit* bytes from *response*, refusing an oversize body.

    ``response.read()`` with no size would buffer an unbounded body
    (a hostile or compromised loopback server could stream indefinitely).
    Read in chunks and stop once the limit is exceeded, raising ValueError
    so the caller reports "server unreachable" instead of OOM.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(65536)  # type: ignore[attr-defined]
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError("response exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


_CFG_DEFAULTS: dict[str, float] = {
    "top_n": 5,
    "lsh_threshold": 0.5,
    "ngram_size": 3,
    "num_permutations": 128,
    "jaccard_weight": 0.4,
}


def _load_config() -> dict:
    """Read the whole CLI config.toml, if present.

    The thin client must produce the same results as `resembl find`, which
    honors these settings — ignoring them would silently change matches.
    Returns the raw table; the caller picks the find keys (``top_n``,
    ``lsh_threshold``, ``ngram_size``, ``num_permutations``,
    ``jaccard_weight``).
    """
    path = config_path_get()
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}  # no config file: the documented default run
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError, RecursionError) as exc:
        # A malformed or unreadable file must not be mistaken for an absent
        # one: the client answers on defaults, so a `top_n` the user set would
        # silently stop applying and the query would return a different result
        # set than `resembl find` on the same database.  Report it the way
        # ``resembl.config.load_config`` does and keep going.
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return {}


def _cfg_number[Num: (int, float)](cfg: dict, key: str, cast: type[Num]) -> Num:
    """Return *key* from *cfg* as *cast*, or the default when it is unusable.

    The config file is hand-editable, so a mistyped value must not take the
    client down: ``int(cfg.get("top_n"))`` on a quoted ``"abc"`` raised an
    uncaught ValueError traceback before the query was ever sent.  The CLI
    handles the same file the same way (warn, keep the default).

    The ``resembl.config`` loader also bounds-checks each value
    (``validate_value``: ``top_n >= 1``, ``lsh_threshold < 0.99``, ...).
    This client cannot import that module without paying for
    ``tomli_w``/``sqlmodel``, so it enforces only the low/high bounds here —
    an out-of-range value must never leave this process, where it would
    change the result set (or be refused by the server with whichever
    bound *it* enforces).
    """
    default = _CFG_DEFAULTS[key]
    value = cfg.get(key, default)
    if isinstance(value, bool) or (
        isinstance(value, str) and value.strip().casefold() in ("true", "false")
    ):
        print(
            f"error: ignoring config value {key} = {value!r} (not a number)",
            file=sys.stderr,
        )
        return cast(default)
    try:
        number = cast(value)
    except TypeError, ValueError, OverflowError:
        print(
            f"error: ignoring config value {key} = {value!r} (not a number)",
            file=sys.stderr,
        )
        return cast(default)
    if cast is int and isinstance(value, float) and not float(value).is_integer():
        print(
            f"error: ignoring config value {key} = {value!r} (not an integer)",
            file=sys.stderr,
        )
        return cast(default)
    if not _within_bounds(key, number):
        print(
            f"error: ignoring config value {key} = {value!r} (out of range)",
            file=sys.stderr,
        )
        return cast(default)
    return number


def _within_bounds(key: str, number: float) -> bool:
    """True when *number* lies inside the range ``resembl.config`` enforces for *key*."""
    if not math.isfinite(number):
        return False
    if key == "lsh_threshold":
        return 0.0 <= number < 0.99
    if key == "num_permutations":
        return 2.0 <= number <= float(1 << 12)  # scoring.MAX_NUM_PERM
    if key == "jaccard_weight":
        return 0.0 <= number <= 1.0
    if key in {"top_n", "ngram_size"}:
        return number >= 1.0
    return True


_VALUE_FLAGS = frozenset({"-o", "--output", "--query", "--file", "--top-n", "--threshold"})


def _schema_words(argv: list[str]) -> list[str] | None:
    """Return the words after a ``schema`` command, or None when it is not one."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            index += 1
            continue
        if token in {"--json", "--no-normalization", "-h", "--help"}:
            index += 1
            continue
        if token == "schema":
            return argv[index + 1 :]
        return None
    return None


def _json_on(output: str, json_flag: bool) -> bool:
    """True when the caller asked for JSON. ``-o text`` wins over ``--json``."""
    if output == "text":
        return False
    return output == "json" or json_flag


def _fail(
    message: str,
    code: int,
    *,
    json_on: bool,
    kind: str | None = None,
    hint: str = "",
) -> int:
    """Print one error. JSON mode writes the stderr envelope and returns *code*."""
    resolved = kind or ("usage" if code == 2 else "failure")
    if json_on:
        payload = {"error": {"kind": resolved, "message": message, "hint": hint}}
        sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


def _print_schema(words: list[str], *, json_on: bool) -> int:
    """Print the clispec document. No config read and no server connection."""
    from .clispec import build_find_schema

    if words and words[0] in {"-h", "--help"}:
        print(
            "usage: resembl-find schema [command]\n"
            "\nPrint the clispec 0.3 JSON document for resembl-find.\n"
            "With a command path (e.g. 'query'), only that command is included.\n"
            "\noptions:\n  -h, --help  show this help message and exit",
        )
        return 0
    path = " ".join(words)
    document = build_find_schema(path or None)
    if path and not document["commands"]:
        return _fail(f"no command matches {path!r}", 2, json_on=json_on, kind="usage")
    sys.stdout.write(json.dumps(document, separators=(",", ":")) + "\n")
    return 0


def _main(argv: list[str] | None = None) -> int:
    # Match names are printed below; a locale-encoded stdout would raise
    # UnicodeEncodeError on the first one the platform's code page cannot
    # represent, losing the whole result table.
    console_utf8_reconfigure()
    args_list = list(sys.argv[1:] if argv is None else argv)
    schema_words = _schema_words(args_list)
    if schema_words is not None:
        output = "auto"
        json_flag = "--json" in args_list
        for index, token in enumerate(args_list):
            if token in {"-o", "--output"} and index + 1 < len(args_list):
                output = args_list[index + 1]
            elif token.startswith("--output="):
                output = token.split("=", 1)[1]
        if output not in ("auto", "text", "json"):
            return _fail(
                f"--output must be one of auto, text, json, got {output!r}",
                2,
                json_on=False,
                kind="usage",
            )
        return _print_schema(schema_words, json_on=_json_on(output, json_flag))

    # JSON mode must be known before argparse reports a usage error, so
    # argparse's own error path can emit the stderr envelope instead of
    # its usage text. Scan argv the same way the schema path does.
    _pre_output = "auto"
    _pre_json = "--json" in args_list
    for _i, _t in enumerate(args_list):
        if _t in {"-o", "--output"} and _i + 1 < len(args_list):
            _pre_output = args_list[_i + 1]
        elif _t.startswith("--output="):
            _pre_output = _t.split("=", 1)[1]
    _pre_json_on = _json_on(_pre_output, _pre_json)

    class _Parser(argparse.ArgumentParser):
        def error(self, message: str) -> NoReturn:
            if _pre_json_on:
                _fail(message, 2, json_on=True, kind="usage")
                raise SystemExit(2)
            super().error(message)
            raise SystemExit(2)

    parser = _Parser(
        prog="resembl-find",
        description=(
            "Query a running resembl server. `resembl-find schema` prints the clispec 0.3 document."
        ),
    )
    parser.add_argument("--query", help="Query string (single-line ';' = separator).")
    parser.add_argument("--file", help="Path to a file containing the query ('-' for stdin).")
    parser.add_argument("--top-n", type=int, default=None, help="How many matches to return.")
    parser.add_argument("--threshold", type=float, default=None, help="LSH threshold override.")
    parser.add_argument(
        "--no-normalization",
        action="store_true",
        help="Do not fold registers and immediates.",
    )
    parser.add_argument(
        "--output",
        "-o",
        choices=("auto", "text", "json"),
        default="auto",
        help="Output mode: auto, text, or json. auto stays text. --json is json.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a table.")
    parser.add_argument("--version", action="store_true", help="Show the resembl version and exit.")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress informational output.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Increase output verbosity.")
    args = parser.parse_args(args_list)
    if args.version:
        from importlib.metadata import PackageNotFoundError, version

        try:
            version_str = version("resembl")
        except PackageNotFoundError:
            version_str = "unknown"
        print(f"resembl-find {version_str}")
        return 0
    json_on = _json_on(args.output, args.json)

    query = args.query
    if query is None and args.file == "-":
        try:
            query = sys.stdin.buffer.read().decode("utf-8")
        except UnicodeDecodeError:
            return _fail("query input is not valid UTF-8", 2, json_on=json_on, kind="usage")
    elif query is None and args.file:
        try:
            with open(args.file, encoding="utf-8") as f:
                query = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            return _fail(f"cannot read {args.file}: {exc}", 1, json_on=json_on)
    elif query is None and not sys.stdin.isatty():
        # Same piping contract as `resembl find`: with neither --query nor
        # --file, a redirected stdin is the query.
        try:
            query = sys.stdin.buffer.read().decode("utf-8")
        except UnicodeDecodeError:
            return _fail("query input is not valid UTF-8", 2, json_on=json_on, kind="usage")
    if query and any(0xD800 <= ord(ch) <= 0xDFFF for ch in query):
        return _fail("query input is not valid UTF-8", 2, json_on=json_on, kind="usage")
    if query and ";" in query and "\n" not in query:
        query = query.replace(";", "\n")
    if not query:
        return _fail(
            "no query provided (--query, --file, or stdin)",
            2,
            json_on=json_on,
            kind="usage",
        )

    db_url = db_url_get()
    # The port file must resolve to the exact path `resembl serve` wrote.
    port_file = server_port_path(db_url, cache_dir_get())
    try:
        with open(port_file, encoding="utf-8") as f:
            port = int(f.read().strip())
    except OSError, ValueError:
        return _fail(
            f"no server running for {db_url_mask(db_url)} (start `resembl serve`)",
            1,
            json_on=json_on,
        )
    # Only a TCP-bindable port can name a serve process: ``serve`` binds
    # the port it writes here (``--port 0`` lets the OS assign one, so a
    # written 0 is never a live server), and a value outside 1-65535
    # cannot be bound at all.  Ports below 1024 are not excluded: the
    # documented trust model for the port file is that its contents are
    # trusted to name the loopback server (THREAT_MODEL R3), and a
    # legitimately bound low port (e.g. ``serve --port 80``) must be
    # dialable, matching ``resembl.cli._server_request``.
    if not 1 <= port <= 65535:
        return _fail(
            f"no server running for {db_url_mask(db_url)} (start `resembl serve`)",
            1,
            json_on=json_on,
        )

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
        # Loopback must not go through a proxy (http_proxy): the server is
        # on this host, and a proxied request would either fail or send the
        # corpus query to a third party.  ProxyHandler({}) disables proxies.
        # urlopen()'s context is an SSLContext, not an opener: passing an
        # opener there only wrapped it as an HTTPSHandler context, so the
        # default opener's env ProxyHandler still proxied the http request.
        # OpenerDirector.open() is the opener-aware path.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            payload = json.loads(_read_bounded_response(response).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # A 4xx answer carries the server's own JSON error envelope: report
        # it verbatim rather than the transport-level "HTTP Error 400", which
        # tells the caller nothing about which parameter was refused.
        try:
            detail = json.loads(_read_bounded_response(exc).decode("utf-8")).get("error", "")
        except ValueError, OSError, AttributeError:
            detail = ""
        return _fail(str(detail or exc.reason), 1, json_on=json_on)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _fail(f"server unreachable: {exc}", 1, json_on=json_on)
    if "error" in payload:
        return _fail(str(payload["error"]), 1, json_on=json_on)
    if "lsh_candidates" not in payload or "matches" not in payload:
        # A server answer without the promised fields is a malformed
        # response, not an empty result: indexing into it would raise
        # KeyError and lose the result table entirely.
        return _fail("server returned a malformed find response", 1, json_on=json_on)

    if json_on:
        print(json.dumps(payload, separators=(",", ":")))
        return 0

    if not args.quiet:
        print(f"Found {payload['lsh_candidates']} candidates via LSH.", file=sys.stderr)
    for i, match in enumerate(payload["matches"], 1):
        names = ", ".join(match["names"])
        print(f"{i}. {match['checksum'][:12]}…  {names}  {match['score']:.2f}")
    return 0


def main() -> None:
    """Console-script entry point."""
    sys.exit(_main())


if __name__ == "__main__":
    main()
