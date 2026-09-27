# Agent rules: resembl

Scope: commands, conventions and invariants for changing this repository.
`CONTRIBUTING.md` is the human-facing narrative guide and holds the same
commands in prose; when the two disagree, the commands here are the ones that
are exercised, and both must be updated together.

## Environment

Requires Python 3.13 or newer and uv exactly 0.12.13: `pyproject.toml` pins it
under `[tool.uv] required-version`, so any other uv release fails at the first
command instead of after it has rewritten the lockfile.

```bash
make install   # uv sync --locked --extra dev, then the git hooks; same as CI
```

`uv.lock` is the source of truth. Never hand-edit it; only `uv add` / `uv lock`
write it. Every `uv run` in the hooks and the `Makefile` passes `--locked`, so a
check that needs a re-resolution fails instead of silently rewriting the lockfile
under the contributor; plain `uv run` re-resolves whenever `pyproject.toml` has
drifted and then gates the code against a lockfile nobody else has.

## Quality gates

The pre-commit hooks run, over the project rather than the staged files
(`pass_filenames: false`):

| Hook   | Command            | Scope                      |
| ------ | ------------------ | -------------------------- |
| black  | `uv run --locked black`     | project                    |
| ruff   | `uv run --locked ruff check --fix --exit-non-zero-on-fix` | project |
| mypy  | `uv run --locked mypy`      | `files` in `pyproject.toml` |
| pylint | `uv run --locked pylint`    | `resembl/ tests/ fuzzers/` |

The same config also runs three mirror hooks: `check-yaml`,
`end-of-file-fixer` and `trailing-whitespace`. No CI workflow runs them, so
`make hygiene` reproduces the two that touch repository files and `make check`
runs it first; a green `make check` is therefore a commit the hooks accept.
`check-yaml` stays hook-only, because validating the workflows is a job CI does
by running them.

The hooks run **no tests**. Before handing work over, run the full gate
yourself:

```bash
make check                         # hygiene, mypy, ruff, black --check, pylint, pytest
uv run pytest --cov=resembl --cov-report=term-missing
```

Coverage may rise, never fall; CI reports the number to Codecov from
`.github/workflows/coverage.yml`, so that file is the ratchet's record, not a
local threshold. mypy's incremental cache is not safe against concurrent
writers, which is why the mypy hook is `require_serial`; run these commands one
at a time.

`make check` covers the source tree, not the artifacts. The `Build` workflow
(`.github/workflows/build.yml`) is what covers those: it runs
`make dist-verify`, so the sdist and the wheel are built twice and must be
byte-identical, then installs each into a throwaway environment and runs
`resembl --help`. Run `make dist-verify` locally when a change touches
`pyproject.toml`'s packaging, the package's data files, or anything the
wheel's contents depend on; no other gate would notice.

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

Fuzzers live in `fuzzers/`; `make fuzz` runs each for `FUZZ_SECONDS` (60 by
default) and asks uv for the `fuzz` extra itself:

```bash
make fuzz FUZZER=fuzz_code_tokenize.py
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
`ci`, `build`, `chore`. A breaking change carries a `!` after the type/scope
in the subject (`fix(server)!: ...`), which is what release-drafter's
autolabeler reads to resolve the next major; a `BREAKING CHANGE:` footer
alone leaves the version on a patch. `.pre-commit-config.yaml`
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
  `flowcharts.md`, `custom_database.md`, `user_stories.md`, `tutorial.md`,
  `man.md`, `THREAT_MODEL.md`).
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
  `pyproject.toml` must not be lowered. The vendored pieces and
  `resembl/lsh.py`'s band slicing derive from MIT-licensed datasketch, so
  `NOTICE` carries that grant and is listed in `license-files`; any new
  vendored or derived third-party code adds its notice to `NOTICE` in the same
  change, or nothing downstream can trace the grant.
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

A release is a maintainer's one `release X.Y.Z` commit on `main`, the single
exception to "never commit on `main`", followed by tag `vX.Y.Z`; the tag is
what the published version is taken from. SemVer decides the
number: a removed or renamed public export, a changed signature, default,
stored format (fingerprint, database, cache) or `serve` response, or a raised
minimum Python version, is major; a new command, flag or config field is
minor; anything else is a patch. Any `**Breaking:**` bullet in `[Unreleased]`
makes the release a major whatever the commit subjects say: the drafter's
autolabeler reads subjects only, so a break committed without a `!` labels as
`fix` and the draft proposes a patch, and the changelog notes are the
authority that overrides it. The same commit turns `[Unreleased]` into
`## [X.Y.Z] - YYYY-MM-DD` and bumps `version` in `pyproject.toml`, then
`uv lock` (the lockfile records the project's own version) and the
supported-versions table in `SECURITY.md`; `tests/test_changelog.py` holds the
manifest, the sections, the break markers and that table to each other. Verify
`uv run pytest` green and a wheel that answers `resembl --help` before tagging;
the `Build` workflow runs both halves of that on the tag before it is cut.
The artifacts come from `make dist`, never a bare `uv build`: it pins the build
clock to the commit's `SOURCE_DATE_EPOCH` and normalizes the sdist archive
metadata, so the same commit always produces the same bytes. `make
dist-verify` builds twice and compares the sha256 sums.
Push the tag, then confirm the published version imports and runs. A published
version is immutable: something wrong means a new patch, never a re-upload.
