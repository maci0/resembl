"""Environment and filesystem path resolution.

One owner for every path a resembl process derives from the environment:
the database URL, the cache directory, the config directory, and the port
file ``resembl serve`` writes.  The CLI, the server and the standalone
``resembl-find`` client all read them from here, so a change to the override
rules or the port-file naming cannot leave the client looking for a file the
server never wrote.  The output encoding of the process goes here too, for
the same reason: it is another environment-derived setting both entry points
have to agree on. Terminal-bound text also shares one control-character filter.

Standard library only, and importing it pulls in nothing from this package:
``resembl.find_client`` resolves its paths through this module instead of
copying the rules, which keeps the client off the sqlmodel / SQLAlchemy /
numpy import graph and its ~50 ms startup.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import sys
import tempfile

#: Database URL used when the environment names none.
DEFAULT_DB_URL = "sqlite:///assembly.db"

#: Environment variables consulted for the database URL, most specific first.
#: ``RESEMBL_DATABASE_URL`` is the namespaced name every other resembl
#: override uses (``RESEMBL_CONFIG_DIR``, ``RESEMBL_CACHE_DIR``) and wins
#: outright.  The unprefixed ``DATABASE_URL`` stays supported and is read
#: next: it is the name other Python and hosting stacks already export, so
#: dropping it would break a working deployment.  An empty value counts as
#: unset in both, matching the directory overrides, rather than handing
#: SQLAlchemy an empty URL to fail on obscurely.
DB_URL_ENV_VARS = ("RESEMBL_DATABASE_URL", "DATABASE_URL")

#: Matches the credentials component of ``scheme://user:password@host/...``.
#: Match SQLAlchemy's user-info syntax, including literal password whitespace
#: and slashes; percent-encoded passwords are hidden the same way.
_DB_URL_CREDENTIALS = re.compile(r"(://[^:/]*:)([^@]*)(@)")

#: Cache directory used when the environment names none.
DEFAULT_CACHE_DIR = "~/.cache/resembl"

#: Config directory used when the environment names none.
DEFAULT_CONFIG_DIR = "~/.config/resembl"

#: Environment variable that authorizes a non-loopback ``serve`` bind
#: without the ``--allow-unauthenticated`` flag, for deployments that
#: configure the process through the environment (systemd, containers).
#: A non-blank value counts as "allow"; the flag wins when both are set
#: (either one opts in -- the check is an OR, never a veto).  Blank
#: counts as unset, matching every other ``RESEMBL_*`` override.
SERVE_ALLOW_UNAUTHENTICATED_ENV_VAR = "RESEMBL_SERVE_ALLOW_UNAUTHENTICATED"

#: Environment variable that sets the log level for every command,
#: for deployments that configure the process through the environment
#: instead of ``-v``/``-q``.  Accepted values are the standard level
#: names (``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``),
#: case-insensitive; a blank value counts as unset.  An explicit
#: ``-v``/``-q`` flag wins over the variable when both are set.
LOG_LEVEL_ENV_VAR = "RESEMBL_LOG_LEVEL"

#: Environment variable naming the runtime manifest ``serve`` checks for
#: ``/find-pinned`` and ``backup`` copies beside each backup.  Blank counts
#: as unset, matching every other ``RESEMBL_*`` override.
RUNTIME_MANIFEST_ENV_VAR = "RESEMBL_RUNTIME_MANIFEST"

#: Translation table folding ASCII a-z to A-Z only, for protocol tokens
#: whose case folding must not map non-ASCII letters into ASCII (U+0131→I,
#: U+017F→S under ``str.upper``).
_ASCII_UPPER_TABLE = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")

#: Translation table folding ASCII A-Z to a-z only, for the same reason.
_ASCII_LOWER_TABLE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def ascii_fold_lower(text: str) -> str:
    """Fold ASCII A-Z to a-z, leaving every non-ASCII character alone.

    Protocol tokens (host names, media types, log levels) compare
    case-insensitively, and ``str.casefold``/``str.lower`` map some non-ASCII
    spellings into ASCII (long s U+017F -> s, Kelvin sign U+212A -> k,
    sharp s U+00DF -> ss under casefold). A Host spelling of localhost
    with the long s in place of the last s would then pass as
    ``localhost`` here while a byte-wise peer refuses it, and a
    Content-Type spelled with the long s would pass as JSON. Folding
    ASCII only keeps the non-ASCII spelling non-ASCII, so the membership
    check rejects it instead of silently accepting a different token.
    """
    return text.translate(_ASCII_LOWER_TABLE)


def is_ascii(text: str) -> bool:
    """Whether *text* is entirely ASCII, for protocol-token gates."""
    return text.isascii()


def db_url_get() -> str:
    """Return the database URL configured in the environment.

    Read at call time, not at import time: an embedder that sets
    ``RESEMBL_DATABASE_URL`` after importing resembl (and a test that points
    the engine at a temporary database) gets the value it set, instead of
    whatever the environment happened to hold when the module was first
    imported.
    """
    for var in DB_URL_ENV_VARS:
        url = os.environ.get(var)
        if url and url.strip():
            return url.strip()
    return DEFAULT_DB_URL


def db_url_mask(url: str) -> str:
    """Return *url* with any embedded password replaced by ``***``.

    Display helper for messages that echo a database URL (connection
    failures, merge progress): URLs like
    ``postgresql+pg8000://user:pass@host/db`` must not print the password.
    An empty username is allowed. URLs without credentials are returned unchanged.
    """
    return _DB_URL_CREDENTIALS.sub(r"\1***\3", url)


def _expand_home(path: str) -> str:
    """Return *path* with a leading ``~`` expanded, or a temp fallback.

    ``os.path.expanduser`` returns the literal ``~``-prefixed string when it
    cannot resolve a home directory — on Windows when neither ``USERPROFILE``
    nor ``HOME`` is set (a service account or a scrubbed environment), and on
    POSIX when the login name has no password-database entry.  Writing into a
    directory literally named ``~`` in the working directory is never what the
    user asked for, so a per-process temp directory name is used instead.
    """
    expanded = os.path.expanduser(path)
    if expanded == path and path.startswith("~"):
        rest = path[1:].lstrip(os.sep + "/")
        return (
            os.path.join(tempfile.gettempdir(), "resembl-home-fallback", rest)
            if rest
            else tempfile.gettempdir()
        )
    # ``expanduser`` keeps the forward slashes of the ``~/.cache`` spelling
    # verbatim, so on Windows the result mixes separators
    # (``C:\Users\u/.cache\resembl``).  The OS accepts both spellings, but a
    # caller comparing the returned path as a string against an
    # ``os.path.join``-built expectation sees a mismatch, so normalize to
    # the platform spelling before returning.
    return os.path.normpath(expanded)


def _xdg_dir(env_var: str, xdg_var: str, default: str) -> str:
    override = os.environ.get(env_var)
    if override and override.strip():
        return _expand_home(override.strip())
    xdg = os.environ.get(xdg_var)
    if xdg and xdg.strip() and os.path.isabs(xdg.strip()):
        return os.path.join(xdg.strip(), "resembl")
    return _expand_home(default)


def cache_dir_get() -> str:
    """Return the cache directory, respecting override environment variables.

    ``RESEMBL_CACHE_DIR`` wins outright.  Otherwise ``$XDG_CACHE_HOME`` is
    honored when set (freedesktop base-directory spec), falling back to the
    historical ``~/.cache/resembl`` default.  Per that spec a relative
    ``$XDG_CACHE_HOME`` is invalid and ignored.
    """
    return _xdg_dir("RESEMBL_CACHE_DIR", "XDG_CACHE_HOME", DEFAULT_CACHE_DIR)


def config_dir_get() -> str:
    """Return the config directory, respecting override environment variables.

    ``RESEMBL_CONFIG_DIR`` wins outright.  Otherwise ``$XDG_CONFIG_HOME`` is
    honored when set (freedesktop base-directory spec), falling back to the
    historical ``~/.config/resembl`` default.  Per that spec a relative
    ``$XDG_CONFIG_HOME`` is invalid and ignored.
    """
    return _xdg_dir("RESEMBL_CONFIG_DIR", "XDG_CONFIG_HOME", DEFAULT_CONFIG_DIR)


def config_path_get() -> str:
    """Return the path to the config file."""
    return os.path.join(config_dir_get(), "config.toml")


def serve_allow_unauthenticated_get() -> bool:
    """Return True when ``RESEMBL_SERVE_ALLOW_UNAUTHENTICATED`` opts in.

    Read at call time, like :func:`db_url_get`: a deployment that
    exports the variable after import still gets the value it set.
    A blank value counts as unset, matching every other override.
    Case-insensitive ``0``/``false``/``no``/``off`` count as an
    explicit opt-out: a conventional ``RESEMBL_SERVE_ALLOW_UNAUTHENTICATED=0``
    exported by a base image or a previous deploy (or copied from a
    ``DATABASE_URL``-style boolean) must not silently authorize an
    unauthenticated non-loopback bind, which is the R1 corpus exposure.
    Every other non-blank value opts in, including ``1``/``true``/``yes``.
    """
    value = os.environ.get(SERVE_ALLOW_UNAUTHENTICATED_ENV_VAR)
    if value is None or not value.strip():
        return False
    return value.strip().casefold() not in ("0", "false", "no", "off")


def runtime_manifest_path_get() -> str | None:
    """Return the path named by ``RESEMBL_RUNTIME_MANIFEST``, or None when unset.

    Read at call time, like :func:`db_url_get`.  Blank counts as unset,
    matching every other ``RESEMBL_*`` override.  The path is returned
    verbatim (surrounding whitespace stripped); existence and readability
    are the caller's check, since ``backup`` and ``serve`` fail startup
    differently on a missing file.
    """
    value = os.environ.get(RUNTIME_MANIFEST_ENV_VAR)
    if value is None or not value.strip():
        return None
    return value.strip()


def log_level_get() -> str | None:
    """Return the log level named by ``RESEMBL_LOG_LEVEL``, or None.

    Read at call time, like :func:`db_url_get`.  The returned name is
    upper-cased; validity is checked by the caller against the
    standard level names, so an invalid value fails fast at startup
    instead of silently running at the default level.
    """
    value = os.environ.get(LOG_LEVEL_ENV_VAR)
    if value is None or not value.strip():
        return None
    # ASCII-only fold: ``str.upper`` maps the dotless U+0131 to I and
    # the long s (U+017F) to S, so U+0131nfo would validate as INFO.  A
    # protocol token must fold ASCII a-z only, leaving non-ASCII intact
    # so the caller's membership check rejects it.
    return value.strip().translate(_ASCII_UPPER_TABLE)


def server_port_path(db_url: str, cache_dir: str) -> str:
    """Return the port file *serve* writes for *db_url*, under *cache_dir*.

    Both sides of the warm-find protocol hash the same string: the raw
    database URL, never :func:`db_url_mask`'s ``user:***@host`` rendering,
    which would name a different file for a URL that carries credentials.
    SHA-256 replaces the historical SHA-1 here: the port-file name is
    derived from a credential-carrying URL, and SHA-1 is deprecated for
    new content-addressing uses.
    """
    digest = hashlib.sha256(db_url.encode("utf-8")).hexdigest()[:12]
    return os.path.join(cache_dir, f"server_{digest}.port")


def port_file_cleanup(port_file: str, port: int) -> None:
    """Remove the port file only when it still advertises our own *port*.

    Shared by the server's exit path and any client that verified the
    advertised port is dead: the double-serve check is not atomic across
    processes, two invocations started close together both pass it (neither
    has written yet), both bind, and the last writer owns the advertisement.
    An unconditional delete on exit would then orphan the surviving server
    for every ``find`` client, so the file is removed only while its
    content is still the known *port*.
    """
    try:
        with open(port_file, "rb") as f:
            if f.read().strip() != str(port).encode("ascii"):
                return
        os.remove(port_file)
    except OSError as e:
        # A missing port file means another process already removed it;
        # any other failure (read-only directory, permission) is worth
        # a visible record so a stale port file is not silently left
        # for the next serve to trip over — but never fail shutdown.
        if e.errno != 2:  # ENOENT
            import logging

            logging.getLogger(__name__).debug(
                "server port file cleanup failed for %s: %s", port_file, e
            )


def _encoding_is_utf8(encoding: str) -> bool:
    """Whether *encoding* names UTF-8 under any of its usual spellings."""
    # ASCII-only fold: an encoding name is a protocol token, and
    # ``casefold`` maps the long s to s, so "utf�" spellings that fold
    # into "utf8" must not read as UTF-8 here.
    return ascii_fold_lower(encoding).replace("-", "").replace("_", "") == "utf8"


def console_utf8_reconfigure() -> None:
    """Make the process's streams UTF-8 and LF-terminated for this run.

    Every string in the package is UTF-8 by the time it is printed (names
    and tags arrive from files, argv and JSON; code is stored NFC), but
    ``sys.stdout`` encodes with whatever the platform's locale says.  A
    Windows shell with a legacy code page hands the CLI a cp1252 stdout, and
    a snippet named ``日本語`` then raised ``UnicodeEncodeError`` and lost
    the whole report rather than one character, so the user saw a traceback
    where a list was due.  ``errors="replace"`` keeps a character the
    terminal's own code page cannot draw from escalating that into a crash.

    ``sys.stdin`` is the same boundary in the other direction: a query
    piped to ``resembl find`` under a C locale (``LC_ALL=C`` with
    ``PYTHONUTF8=0``) was decoded as ASCII with surrogateescape, so every
    UTF-8 byte became a lone surrogate and the query — checksummed with
    ``surrogatepass`` so it never crashed — silently searched for text
    that was never in the corpus.  Reconfiguring stdin to UTF-8 decodes
    the bytes the sender wrote; ``errors="surrogateescape"`` is kept so
    a genuinely invalid byte still cannot crash the read, it just
    cannot match, exactly as it could not before.

    The encoding is only replaced where it is not already UTF-8, so the
    Windows console's own writer keeps its Unicode path.  ``newline="\\n"``
    is pinned on every output stream, UTF-8 or not: ``newline=None``
    rewrites every ``"\\n"`` to ``os.linesep``, so the same command
    emitted LF-terminated output on POSIX and CRLF on Windows.  Every
    renderer here writes ``"\\n"`` (see ``_CSV_LINETERMINATOR`` in the
    CLI), and a report that a script reads should not differ by
    platform.  Stdin is not newline-pinned: universal-newline decoding
    there is what makes a CRLF-piped query compare equal to its LF
    spelling, and pinning it would reintroduce the platform difference
    on the input side.
    """
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        if not isinstance(stream, io.TextIOWrapper):
            continue
        is_input = stream is sys.stdin
        try:
            if _encoding_is_utf8(stream.encoding):
                if not is_input:
                    stream.reconfigure(newline="\n")
            elif is_input:
                stream.reconfigure(encoding="utf-8", errors="surrogateescape")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except ValueError:
            # A stream the caller already closed or detached cannot be
            # reconfigured, and nothing will be written to it either.
            continue


#: Code points dropped from text before it is shown on a terminal: C0 controls
#: except tab and newline (so a multi-line query still reads), carriage return
#: (it overwrites the line so far, which spoofs what was printed before it),
#: DEL, and the C1 range.  ESC goes with them, which is what stops a stored
#: name from smuggling ANSI sequences (clear screen, write a fake prompt, set
#: the window title) into the viewer's terminal.
_DISPLAY_STRIP = dict.fromkeys(
    [*range(0x09), 0x0B, 0x0C, 0x0D, *range(0x0E, 0x20), 0x7F, *range(0x80, 0xA0)],
    None,
)


def terminal_text_clean(text: str) -> str:
    """Return *text* with terminal control characters removed.

    Names and other database text are untrusted on the way out as well as in:
    a shared or merged database can carry a name with raw ESC bytes that would
    rewrite the viewer's screen.  Tab and newline survive (they are how
    multi-line queries render); everything else below a space, carriage
    return, DEL and the C1 range go.
    """
    return text.translate(_DISPLAY_STRIP)
