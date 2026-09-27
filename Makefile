# Thin wrapper over the commands the project already uses.  Every target
# runs a command documented in README.md / CONTRIBUTING.md and the same
# command CI runs, so `make check` reproduces the pipeline locally.  It also
# runs the two file-hygiene checks the commit gate runs and CI does not (see
# `hygiene`), so a green `make check` is a green commit.
#
# `make help` lists the targets.

# Recipe shells are bash with the usual strict flags, so a failing command in
# a pipeline (`tar | gzip`) aborts the target instead of leaving the last
# command's exit code as the verdict.  The Makefile writes no files itself,
# only checks and builds.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

# Every uv invocation runs with --locked, so no target can resolve the
# environment against anything but uv.lock.  Plain `uv run` re-resolves and
# rewrites uv.lock in the working tree when pyproject.toml has drifted, which
# would let a local gate pass against a lockfile CI never sees; --locked fails
# instead, and `uv lock` (or `make install`) is the deliberate way to move the
# lockfile forward.

.DEFAULT_GOAL := help

.PHONY: help install format lint types hygiene test db-test fuzz check dist dist-verify sbom

PYTEST_ARGS ?=

# Seconds atheris fuzzes for, per target.  A fuzzer run without a bound
# never returns, so the duration is an argument rather than a constant.
FUZZ_SECONDS ?= 60

# Artifact build epoch: the commit time of HEAD, never the build host's clock.
# setuptools stamps SOURCE_DATE_EPOCH into the wheel's zip entries; the sdist
# is normalized afterwards (see `dist`).
SOURCE_DATE_EPOCH ?= $(shell git log -1 --format=%ct 2>/dev/null || echo 0)

# The PostgreSQL and MySQL integration modules skip themselves unless
# RESEMBL_TEST_PG_URL / RESEMBL_TEST_MYSQL_URL are set.  CI sets both, so a
# plain `make test` runs strictly less than CI does; `db-test` runs the two
# modules and names the missing variables instead of reporting a silent skip.
DB_TESTS = tests/test_pg_integration.py tests/test_mysql_integration.py

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Create .venv from uv.lock and install the git hooks
	uv sync --locked --extra dev
	uv run pre-commit install

format:  ## Apply the auto-fixes black and ruff can make
	uv run --locked black .
	uv run --locked ruff check --fix .

lint:  ## Run ruff and pylint
	uv run --locked ruff check .
	uv run --locked pylint resembl/ tests/ fuzzers/

types:  ## Run mypy over resembl/, tests/ and fuzzers/
	uv run --locked mypy

# The tracked text files the pre-commit hygiene hooks rewrite.  The pathspecs
# mirror what the hooks see (files git tracks), which is why they are listed
# here instead of globbing the working tree: a build artifact in the tree must
# not fail a check the commit would not have run.
HYGIENE_FILES = $(shell git ls-files '*.py' '*.md' '*.yml' '*.yaml' '*.toml' '*.cfg' '*.txt' '*.asm' '*.json' Makefile .pre-commit-config.yaml .gitattributes .gitignore)

# The pre-commit config runs trailing-whitespace and end-of-file-fixer over
# every commit; no CI workflow runs them.  That leaves the commit gate and
# `make check` disagreeing: a contributor who installed the hooks has a commit
# refused for trailing whitespace that `make check` just called green, and one
# who skipped `make install`'s hook step has the same damage reach the branch
# unchecked.  These two checks are the whole of that gap, run here so the
# single documented verification step covers the commit as well as CI.
# check-yaml stays hook-only: it needs the mirror env pre-commit builds, and
# the workflows it would validate are parsed by every CI run anyway.
# The whitespace scan uses POSIX grep -E rather than ripgrep: `make check` is
# the documented entry point on macOS, where ripgrep is not a system tool.
hygiene:  ## Check the file hygiene the pre-commit hooks enforce (trailing whitespace, final newline)
	@if [ -z "$(HYGIENE_FILES)" ]; then \
		echo "make hygiene needs git: 'git ls-files' returned nothing, so there is"; \
		echo "nothing to check and the target would pass vacuously."; exit 1; \
	fi; \
	whitespace=$$(grep -nE ' +$$' $(HYGIENE_FILES) || true); \
	if [ -n "$$whitespace" ]; then \
		echo "trailing whitespace, which the pre-commit trailing-whitespace hook refuses to commit:"; \
		echo "$$whitespace"; exit 1; \
	fi; \
	unterminated=$$(for f in $(HYGIENE_FILES); do \
		if [ -s "$$f" ] && [ -n "$$(tail -c1 "$$f")" ]; then echo "$$f"; fi; \
	done); \
	if [ -n "$$unterminated" ]; then \
		echo "no newline at end of file, which the pre-commit end-of-file-fixer hook refuses to commit:"; \
		echo "$$unterminated"; exit 1; \
	fi

test:  ## Run the test suite (pass args through: make test PYTEST_ARGS="-k cache")
	uv run --locked pytest -q $(PYTEST_ARGS)

db-test:  ## Run only the PostgreSQL and MySQL integration tests (needs both URLs set)
	@if [ -z "$$RESEMBL_TEST_PG_URL" ] || [ -z "$$RESEMBL_TEST_MYSQL_URL" ]; then \
		echo "make db-test needs RESEMBL_TEST_PG_URL and RESEMBL_TEST_MYSQL_URL,"; \
		echo "the two CI services (.github/workflows/tests.yml). Point them at your own servers:"; \
		echo "  RESEMBL_TEST_PG_URL=postgresql+pg8000://user:pass@host/db \\"; \
		echo "  RESEMBL_TEST_MYSQL_URL=mysql+pymysql://user:pass@host/db make db-test"; \
		exit 1; \
	fi
	uv run --locked pytest -q $(DB_TESTS) $(PYTEST_ARGS)

# atheris lives in the `fuzz` extra, so the target asks uv for it instead of
# failing on an import the contributor has to guess at.  Naming one script
# (make fuzz FUZZER=fuzz_code_tokenize.py) is the common case while working
# on a single entry point; with no name, every fuzzer runs in turn.
FUZZERS = $(notdir $(wildcard fuzzers/fuzz_*.py))
FUZZER ?=

fuzz:  ## Fuzz every entry point for FUZZ_SECONDS, or one via FUZZER=<name> (installs the fuzz extra)
	@for f in $(if $(FUZZER),$(FUZZER),$(FUZZERS)); do \
		echo "== fuzzers/$$f for $(FUZZ_SECONDS)s"; \
		uv run --locked --extra fuzz ./fuzzers/$$f -max_total_time=$(FUZZ_SECONDS) || exit 1; \
	done

# The order mirrors .github/workflows/tests.yml and pylint.yml: the file
# hygiene the commit gate enforces, then the static checks (cheap, fixable),
# then the suite.
check:  ## Run every check CI runs, plus the commit-gate file checks
	$(MAKE) hygiene
	$(MAKE) types
	uv run --locked ruff check .
	uv run --locked black --check .
	uv run --locked pylint resembl/ tests/ fuzzers/
	$(MAKE) test
	@if [ -z "$$RESEMBL_TEST_PG_URL" ] || [ -z "$$RESEMBL_TEST_MYSQL_URL" ]; then \
		echo "note: the PostgreSQL and MySQL integration tests were skipped (unset"; \
		echo "RESEMBL_TEST_PG_URL / RESEMBL_TEST_MYSQL_URL). CI runs them; 'make db-test' does too."; \
	fi

# The release build.  `uv build` alone is not reproducible: setuptools honors
# SOURCE_DATE_EPOCH for the wheel, but the sdist keeps the source mtimes, the
# build user's uid/gid, the member order it happened to walk and the gzip
# header's wall clock, so two runs of the same commit produce different bytes.
# The tar normalization below pins all four (reproducible-builds.org), so
# `make dist` twice from the same commit yields identical sha256 sums.
#
# `build/` and `resembl.egg-info/` go first because setuptools stages the
# wheel in `build/lib` and would otherwise zip whatever an earlier build left
# there: a module deleted from the source tree survives in the artifact until
# someone cleans the directory by hand.  Both are gitignored build output, and
# uv build rewrites them.  The sdist is unpacked under `.scratch/` rather than
# `mktemp -d`'s default, which is a tmpfs on Linux: the normalization of a
# full source tree then costs page cache, not RAM.
dist:  ## Build the sdist and wheel into dist/ reproducibly
	@tar --sort=name --help >/dev/null 2>&1 || { \
		echo "make dist needs GNU tar (--sort=name); on macOS install gnu-tar"; exit 1; }
	rm -rf dist
	rm -rf build resembl.egg-info
	SOURCE_DATE_EPOCH="$(SOURCE_DATE_EPOCH)" LC_ALL=C TZ=UTC uv build --out-dir dist
	@set -e; mkdir -p .scratch; for sdist in dist/*.tar.gz; do \
		unpack=$$(mktemp -d .scratch/sdist-XXXXXX); \
		tar --extract --file "$$sdist" --directory "$$unpack"; \
		root=$$(ls "$$unpack"); \
		tar --create --sort=name \
			--mtime="@$(SOURCE_DATE_EPOCH)" \
			--owner=0 --group=0 --numeric-owner \
			--directory "$$unpack" --file - "$$root" | gzip -n -9 > "$$sdist.tmp"; \
		mv "$$sdist.tmp" "$$sdist"; \
		rm -rf "$$unpack"; \
	done

# Proves the claim above instead of asserting it: build twice, compare.  The
# second build gets no help from the first: `uv build` keeps no artifact cache
# and `dist` removes the output directory, so nothing but the source tree
# carries over, which is exactly what the comparison has to measure.  Touching
# `pyproject.toml` moves its mtime so the second build cannot be served by
# anything that keys on it.  `sha256sum` is coreutils; macOS ships
# `shasum -a 256` instead.  Each recipe line is its own shell, so the choice is
# repeated rather than exported.
dist-verify:  ## Build dist/ twice and fail unless the two builds are byte-identical
	@$(MAKE) --no-print-directory dist
	@if command -v sha256sum >/dev/null 2>&1; then \
		sha256sum dist/* > .dist-first.sha256; \
	else \
		shasum -a 256 dist/* > .dist-first.sha256; \
	fi
	@touch pyproject.toml
	@$(MAKE) --no-print-directory dist
	@if command -v sha256sum >/dev/null 2>&1; then \
		sha256sum --check .dist-first.sha256; \
	else \
		shasum -a 256 --check .dist-first.sha256; \
	fi
	@rm -f .dist-first.sha256

# The release inventory, exported by uv from the hash-pinned uv.lock rather
# than read back out of the built wheel, so it describes what the lock resolves
# to whether or not anything has been packed.  Same command the SBOM workflow
# runs, so the file a maintainer inspects before tagging is the file the
# release carries.  uv is the only tool involved: a third-party SBOM generator
# would be another dependency to keep current to answer a question the build
# tool already answers.
#
# It lands in build/ rather than dist/ because `dist` starts by removing that
# directory, and an inventory a release is assessed with should not be deleted
# by the next `make dist`.  Both are gitignored build output.
sbom:  ## Write build/sbom.cdx.json, the CycloneDX 1.5 inventory of the runtime dependencies
	@mkdir -p build
	uv export --preview-features sbom-export --format cyclonedx1.5 \
		--no-dev --quiet --output-file build/sbom.cdx.json
	@echo "wrote build/sbom.cdx.json"
