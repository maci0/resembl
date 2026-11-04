"""Environment and filesystem path resolution.

One owner for every path a resembl process derives from the environment:
the database URL, the cache directory, the config directory, and the port
file ``resembl serve`` writes.  The CLI, the server and the standalone
``resembl-find`` client all read them from here, so a change to the override
rules or the port-file naming cannot leave the client looking for a file the
server never wrote.  The output encoding of the process goes here too, for
the same reason: it is another environment-derived setting both entry points
have to agree on.

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
#: The password run stops at the first character a URL may not carry bare;
#: percent-encoded passwords round-trip untouched.
_DB_URL_CREDENTIALS = re.compile(r"(://[^:/?#\s]+:)([^@/\s]+)(@)")

#: Cache directory used when the environment names none.
DEFAULT_CACHE_DIR = "~/.cache/resembl"

#: Config directory used when the environment names none.
DEFAULT_CONFIG_DIR = "~/.config/resembl"


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
    URLs without credentials are returned unchanged.
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
    return expanded


def cache_dir_get() -> str:
    """Return the cache directory, respecting override environment variables.

    ``RESEMBL_CACHE_DIR`` wins outright.  Otherwise ``$XDG_CACHE_HOME`` is
    honored when set (freedesktop base-directory spec), falling back to the
    historical ``~/.cache/resembl`` default.  Per that spec a relative
    ``$XDG_CACHE_HOME`` is invalid and ignored.
    """
    override = os.environ.get("RESEMBL_CACHE_DIR")
    if override and override.strip():
        return _expand_home(override.strip())
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "resembl")
    return _expand_home(DEFAULT_CACHE_DIR)


def config_dir_get() -> str:
    """Return the config directory, respecting override environment variables.

    ``RESEMBL_CONFIG_DIR`` wins outright.  Otherwise ``$XDG_CONFIG_HOME`` is
    honored when set (freedesktop base-directory spec), falling back to the
    historical ``~/.config/resembl`` default.  Per that spec a relative
    ``$XDG_CONFIG_HOME`` is invalid and ignored.
    """
    override = os.environ.get("RESEMBL_CONFIG_DIR")
    if override and override.strip():
        return _expand_home(override.strip())
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "resembl")
    return _expand_home(DEFAULT_CONFIG_DIR)


def config_path_get() -> str:
    """Return the path to the config file."""
    return os.path.join(config_dir_get(), "config.toml")


def server_port_path(db_url: str, cache_dir: str) -> str:
    """Return the port file *serve* writes for *db_url*, under *cache_dir*.

    Both sides of the warm-find protocol hash the same string: the raw
    database URL, never :func:`db_url_mask`'s ``user:***@host`` rendering,
    which would name a different file for a URL that carries credentials.
    """
    digest = hashlib.sha1(db_url.encode("utf-8")).hexdigest()[:12]
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
        with open(port_file, encoding="utf-8") as f:
            if f.read().strip() != str(port):
                return
        os.remove(port_file)
    except OSError:
        pass


def _encoding_is_utf8(encoding: str) -> bool:
    """Whether *encoding* names UTF-8 under any of its usual spellings."""
    return encoding.casefold().replace("-", "").replace("_", "") == "utf8"


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
