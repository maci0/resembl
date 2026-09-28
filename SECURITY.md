# Security policy

Last reviewed: 2026-09-28, against the tree at version 3.1.0
(`pyproject.toml:3`).

This describes the current tree and the 3.1.0 release. The `serve`
controls below (the loopback `Host` check, `serve -v` request logging, the
`0700` cache directory, the `0600` port file, and the 1000-row `top_n` cap)
are documented in `CHANGELOG.md`.

## Reporting a vulnerability

The intended channel is GitHub's private vulnerability reporting: open the
repository's **Security** tab and choose **Report a vulnerability**. That
opens a private advisory visible only to the reporter and the maintainers,
which is the right channel for a working exploit or a proof-of-concept.

That option is not currently available. Private vulnerability reporting is
not enabled on this repository, so the Security tab does not offer it. Until
it is enabled, the only channel that reaches a maintainer is the public issue
tracker, which is the wrong channel for an unfixed vulnerability: an issue
there is visible to everyone from the moment it is opened. A reporter who
has a working exploit should therefore hold it until a maintainer contact
address is published here, and not open the issue in the meantime.

The repository is <https://github.com/maci0/resembl>.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 3.x (current release line) | yes |
| 2.x and earlier | no |

The current release is 3.1.0. Older lines receive no fixes; upgrade to the
current line before reporting an issue against them.

## Threat model

`docs/THREAT_MODEL.md` records the entry points, trust boundaries, assets,
and ranked risks for the code as it stands. Read it before reporting: it
names the mitigations that already exist, so a report of a
missing control is far more useful than a report of a known gap.

The one gap to be aware of while using `resembl serve` is that
`POST /find` is unauthenticated: it answers any caller that can reach the
port, so the corpus is readable a page at a time. A single response is
capped at 1000 rows, but a sequence of requests is not. On a loopback bind
that is a concern for other processes on the same host, and on a
non-loopback bind it is the same exposure as the bind itself. Run
`resembl serve -v` if the served queries have to be attributable after the
fact; nothing is recorded at the default log level.

## Deployment assumptions

resembl is a single-user local tool. It has no authentication, no
authorization, and no multi-tenancy. The following are assumptions the code
relies on, not configuration options.

- **`resembl serve` stays on loopback.** It binds `127.0.0.1` by default and
  answers every request that reaches the port. Passing `--host` with a
  non-loopback address publishes an unauthenticated read of the whole
  snippet corpus to that network and prints a warning that does not stop the
  bind. Do not put it behind a reverse proxy that does not authenticate.
- **The cache directory is private to the user.** The port file in it is
  read as an unauthenticated port number by every `find` client, which then
  dials that port on `127.0.0.1`. `resembl serve` creates that directory with
  mode `0700` and the port file with mode `0600`, so a directory the server
  made itself is not writable by anyone else. Nothing re-checks the mode, so
  a cache directory created earlier by an operator's own `mkdir`, by another
  tool, or with a different umask keeps that mode, and a shared or
  world-writable one lets another local user point queries at a listener of
  their own. The config directory (`RESEMBL_CONFIG_DIR`, else
  `$XDG_CONFIG_HOME/resembl`, else `~/.config/resembl`) is a separate directory
  with the same
  requirement: it supplies thresholds and match settings, and the tool does
  not report that it is running under a non-default configuration.
- **`DATABASE_URL` is trusted verbatim.** `RESEMBL_DATABASE_URL` takes
  precedence over it, and either may carry a password and name a remote
  host. Keep it in the process environment, not in a shell profile or a
  shared dotfile.
- **Imported directories are the operator's own.** `resembl import` walks a
  directory, keeps every `.asm`/`.txt` path in memory, and runs one worker
  process per CPU over the set. There is no file-count or file-size cap, so a
  large or hostile tree costs the machine memory and CPU, and the
  confirmation prompt is not a bound.
- **Merge sources are trusted.** `resembl merge` inserts another database's
  snippets, names, tags, and collections into the local corpus. Point it only
  at databases you control. Its one closed edge is fingerprint
  deserialization: blobs without the `RMLH` magic are rejected and
  recomputed rather than unpickled. The failure path is not closed: a source
  that will not open makes `merge` print the driver's own error text, which
  can name the host and port it dialled.
