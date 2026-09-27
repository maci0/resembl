# Agent rules: resembl

Scope: commands, conventions and invariants for changing this repository.
`CONTRIBUTING.md` is the human-facing narrative guide and holds the same
commands in prose; when the two disagree, the commands here are the ones that
are exercised, and both must be updated together.

## Environment

Requires Python 3.13 or newer and uv.

```bash
uv sync --locked --extra dev   # installs .venv from the hash-pinned uv.lock, same as CI
uv run pre-commit install      # git hooks, run on commit
```

`uv.lock` is the source of truth. Never hand-edit it; only `uv add` / `uv lock`
write it.

## Quality gates

The pre-commit hooks run, over the project rather than the staged files
(`pass_filenames: false`):

| Hook   | Command            | Scope                      |
| ------ | ------------------ | -------------------------- |
| black  | `uv run black`     | project                    |
| ruff   | `uv run ruff check --fix --exit-non-zero-on-fix` | project |
| mypy  | `uv run mypy`      | `files` in `pyproject.toml` |
| pylint | `uv run pylint`    | `resembl/ tests/ fuzzers/` |

The hooks run **no tests**. Before handing work over, run the full gate
yourself:

```bash
make check                         # mypy, ruff, black --check, pylint, pytest: what CI runs
uv run pytest --cov=resembl --cov-report=term-missing
```

Coverage may rise, never fall. mypy's incremental cache is not safe against
concurrent writers, which is why the mypy hook is `require_serial`; run these
commands one at a time.

`tests/test_pg_integration.py` and `tests/test_mysql_integration.py` skip
themselves unless `RESEMBL_TEST_PG_URL` / `RESEMBL_TEST_MYSQL_URL` are set, so
the commands above cover less than CI does. A change touching either dialect
needs `make db-test` against real servers before it is handed over.

Never silence a finding to get a gate green: fix the code, or scope a single
inline `# noqa` / `# pylint: disable=` that names why. The global `disable` list
in `pyproject.toml` is for checks owned by another tool or blocked by a
deliberate convention; do not extend it to quiet a one-off.

## Tests

TDD: write the failing test first and watch it fail for the right reason. A
test drives the real entry point and asserts shipped output (stdout, a JSON
field, a written file). Re-doing the logic inside the test, feeding it a
finished result, or asserting only exit code 0 is not a test. Do not delete a
test to make a suite pass; change it so it still asserts the behavior.

Fuzzers live in `fuzzers/` and need the `fuzz` extra:

```bash
uv sync --locked --extra dev --extra fuzz
uv run ./fuzzers/fuzz_code_tokenize.py -max_total_time=60
```

A crash writes `crash-<hash>` in the repository root; attach it to the bug
report.

## Branches and commits

Never commit on `main`. Cut `feat/`, `fix/`, `refactor/`, `docs/`, `test/`,
`chore/` branches named `<type>/<issue-number>-short-description`.

Conventional Commits v1.0.0, one type per commit, description in the
imperative, lowercase, no trailing period:

```
fix: answer 400 with one JSON error envelope on the serve endpoints
```

Allowed types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `perf`,
`ci`, `build`, `chore`. A breaking change is either a `!` after the
type/scope or a `BREAKING CHANGE:` footer; release-drafter resolves either to
the next major, never to a patch. `.pre-commit-config.yaml`
carries a header explaining why the hooks run through `uv run` rather than
pinned mirror hooks; keep that reasoning in sync with any change there.

## Function naming

`noun_verb` or `noun_noun_verb`, so the name states the object before the
action.

- `db_verb` for the database as a whole (`db_clean`, `db_reindex`, `db_merge`)
- `snippet_verb` for one snippet (`snippet_add`, `snippet_delete`)
- `snippet_name_verb` for a snippet's names (`snippet_name_add`)

## Documentation

Untested code is broken; an undocumented feature is incomplete. A change
touches the matching docs in the same change:

- Docstrings on every new module, class and function (purpose, arguments,
  returns).
- `README.md` when installation, a core concept, or basic usage changes. A new
  flag also lands in the tool's own `--help`.
- The relevant file in `docs/` (`api_reference.md`, `http_api.md`,
  `flowcharts.md`, `custom_database.md`, `user_stories.md`, `tutorial.md`).
- `CHANGELOG.md` under `## [Unreleased]`, grouped `Added` / `Changed` /
  `Fixed`, written for a consumer: what changed for you, and the migration step
  for anything breaking.

Decisions are recorded by kind:

- **PRD**: product requirements, what to build and why (problem, requirements,
  acceptance).
- **RFC**: request for comments, the technical proposal, circulated before the
  decision locks. A decision still being made is an RFC, never a "proposed
  ADR".
- **ADR**: a decision already made (Context, Decision, Consequences), in
  `docs/adr/`, indexed in `docs/adr/README.md`. A reversal is a new ADR that
  supersedes the old one; an accepted record is amended only for provable
  factual drift, never rewritten in place.

## Invariants

Non-obvious, and cheap to break without noticing:

- `resembl/minhash.py` is a vendored, bit-compatible MinHash. `datasketch` is a
  test oracle only: never import it from package code. The oracle tests pin
  `scheme="legacy"`, a 2.0-only keyword, so the `datasketch>=2` floor in
  `pyproject.toml` must not be lowered.
- The `pygments>=2.20.0` floor clears CVE-2026-4539; `pylint>=4` is required
  because the tree is adapted to pylint 4's checks, including the two mutable
  module-level singletons in `resembl/database.py` and `resembl/scoring.py` that
  are scope-disabled inline. Lowering either floor makes CI fail.
- CLI commands import heavy modules lazily on use; the package is import-light
  by design, so do not hoist those imports to module scope.
- `resembl/__init__.py` resolves exports through a lazy `__getattr__` (PEP 562)
  so `import resembl` stays cheap. A new public symbol goes in `__all__` and in
  the `_CORE_EXPORTS` or `_MODEL_EXPORTS` set it dispatches through, never at
  module top level. `resembl/core.py` and
  `resembl/models.py` keep re-export blocks for external compatibility; treat
  them as the compatibility surface, not as the internal call path.

## Dependencies

Check the standard library and the existing dependency set first; a dependency
is a permanent upkeep and security cost. Open an issue before adding one.
Record it with uv, never by editing `pyproject.toml` by hand:

```bash
uv add <package-name>              # runtime
uv add --optional dev <package-name>  # development only
```

`uv pip install` touches only the environment, so a dependency added that way
never reaches `pyproject.toml` or the lockfile and is invisible to every other
contributor.

## Releasing

A release is one `release X.Y.Z` commit from `main`, followed by tag `vX.Y.Z`;
the tag is what the published version is taken from. SemVer decides the
number: a removed or renamed public export, a changed signature, default,
stored format (fingerprint, database, cache) or `serve` response, or a raised
minimum Python version, is major; a new command, flag or config field is
minor; anything else is a patch. The same commit turns `[Unreleased]` into
`## [X.Y.Z] - YYYY-MM-DD` and bumps `version` in `pyproject.toml`, then
`uv lock` (the lockfile records the project's own version). Verify
`uv run pytest` green and a wheel that answers `resembl --help` before tagging.
Push the tag, then confirm the published version imports and runs. A published
version is immutable: something wrong means a new patch, never a re-upload.
