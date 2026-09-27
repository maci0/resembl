# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities through GitHub's private vulnerability
reporting: open the repository's **Security** tab and choose **Report a
vulnerability**. That opens a private advisory visible only to the reporter
and the maintainers, which is the right channel for a working exploit or a
proof-of-concept.

Do not open a public issue for an unfixed vulnerability.

The repository is <https://github.com/maci0/resembl>.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 2.x (current release line) | yes |
| 1.x and earlier | no |

The current release is 2.0.0. Older lines receive no fixes; upgrade to the
current line before reporting an issue against them.

## Threat model

`docs/THREAT_MODEL.md` records the entry points, trust boundaries, assets,
and ranked risks for the code as it stands. Read it before reporting: it
names the mitigations that already exist, so a report of a
missing control is far more useful than a report of a known gap.

The one gap to be aware of while using `resembl serve` is that `POST /find`
accepts `top_n` without an upper bound, so a single request can return the
whole corpus. On a loopback bind that is a concern for other processes on the
same host, and on a non-loopback bind it is the same exposure as the bind
itself.

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
  read as an unauthenticated port number by every `find` client, and the
  config directory it sits beside supplies thresholds and match settings
  without saying so. A shared or world-readable cache directory lets another
  user redirect queries or change results.
- **`DATABASE_URL` is trusted verbatim.** It may carry a password and it may
  name a remote host. Keep it in the process environment, not in a shell
  profile or a shared dotfile.
- **Merge sources are trusted.** `resembl merge` inserts another database's
  snippets, names, tags, and collections into the local corpus. Point it only
  at databases you control. Its one closed edge is fingerprint
  deserialization: blobs without the `RMLH` magic are rejected and
  recomputed rather than unpickled.
