# Thin wrapper over the commands the project already uses.  Every target
# runs a command documented in README.md / CONTRIBUTING.md and the same
# command CI runs, so `make check` reproduces the pipeline locally.  It also
# runs the two file-hygiene checks the commit gate runs and CI does not (see
# `hygiene`), so a green `make check` is a green commit.
#
# `make help` lists the targets.

# Recipes run under the platform's POSIX shell with the usual strict flags,
# so a failing command aborts the target instead of leaving a later command's
# exit code as the verdict.  The Makefile writes no files itself, only checks
# and builds.
#
# SHELL is left at make's own default (/bin/sh) rather than pinned to
# /bin/bash: CONTRIBUTING.md and docs/tutorial.md document this Makefile as the
# command list on Windows (GnuWin32 Make) as well as macOS and Linux, and
# neither a /bin/bash path nor a bash-only shell flag exists there.  The
# recipes below use no bashism (POSIX `[ ]`, `for`, `$${VAR:-}` throughout),
# so the default shell runs them unchanged.
#
# `-o pipefail` is not POSIX: dash and busybox `sh` reject the flag outright,
# which would abort every recipe on a system where /bin/sh is dash.  It is
# probed once here and prepended only when the shell accepts it, keeping the
# fail-fast pipeline semantics (`tar | gzip`) on bash while leaving a
# non-pipefail shell running the same commands with `-eu`.
SHELL ?= /bin/sh
.SHELLFLAGS := -eu -c
PIPEFAIL := $(shell (set -o pipefail) >/dev/null 2>&1 && echo pipefail)
ifneq ($(PIPEFAIL),)
.SHELLFLAGS := -eu -o pipefail -c
endif

# Every uv invocation runs with --locked, so no target can resolve the
# environment against anything but uv.lock.  Plain `uv run` re-resolves and
# rewrites uv.lock in the working tree when pyproject.toml has drifted, which
# would let a local gate pass against a lockfile CI never sees; --locked fails
# instead, and `uv lock` (or `make install`) is the deliberate way to move the
# lockfile forward.

.DEFAULT_GOAL := help

.PHONY: help install format lint types hygiene test bench coverage db-test service-check service-fuzz fuzz check dist dist-verify sbom backup backup-drill

PYTEST_ARGS ?=

# Seconds atheris fuzzes for, per target.  A fuzzer run without a bound
# never returns, so the duration is an argument rather than a constant.
FUZZ_SECONDS ?= 60

# Artifact build epoch: the commit time of HEAD, never the build host's clock.
# setuptools stamps SOURCE_DATE_EPOCH into the wheel's zip entries; the sdist
# is normalized afterwards (see `dist`).
# LC_ALL=C and TZ=UTC are pinned here so every recipe inherits them: locale
# affects sort order and message formatting, and the timezone affects
# formatted dates embedded in output (reproducible-builds.org).  The dist
# target re-pins them explicitly for the uv build invocation as well.
export LC_ALL := C
export TZ := UTC
# Pin umask for the `dist` recipe (see there): wheel zip permissions copy
# umask-affected source modes, so `umask 002` yields 664 and `umask 022`
# yields 644, and the bytes differ.
# The `?=` default only applies when the variable is unset: it does not
# override an inherited one, and GitHub Actions injects SOURCE_DATE_EPOCH=0
# into every step's environment (service.yml works around it for Docker).
# Left alone, CI `dist` builds stamp 1980 and a local build stamps the
# commit time, so the same commit yields different bytes in the two places.
# An explicit operator value still wins; only the injected 0 falls through
# to the commit time.
SOURCE_DATE_EPOCH ?= $(shell git log -1 --format=%ct 2>/dev/null || echo 0)
ifeq ($(SOURCE_DATE_EPOCH),0)
SOURCE_DATE_EPOCH := $(shell git log -1 --format=%ct 2>/dev/null || echo 0)
endif

# The PostgreSQL and MySQL integration modules skip themselves unless
# RESEMBL_TEST_PG_URL / RESEMBL_TEST_MYSQL_URL are set.  CI sets both, so a
# plain `make test` runs strictly less than CI does; `db-test` runs the two
# modules and names the missing variables instead of reporting a silent skip.
DB_TESTS = tests/test_pg_integration.py tests/test_mysql_integration.py

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Create .venv from uv.lock and install the git hooks
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "make install needs uv 0.12.13 or newer (see pyproject.toml [tool.uv] required-version)."; \
		echo "Install uv from https://docs.astral.sh/uv/getting-started/installation/ and re-run 'make install'."; \
		exit 1; \
	fi
	uv sync --locked --extra dev
	uv run --locked pre-commit install

format:  ## Apply the auto-fixes black and ruff can make
	uv run --locked black .
	uv run --locked ruff check --fix .

lint:  ## Run ruff and pylint
	uv run --locked ruff check .
	uv run --locked pylint resembl/ tests/ fuzzers/ benchmarks/

types:  ## Run mypy over resembl/, tests/, fuzzers/ and benchmarks/
	uv run --locked mypy

# The text files the pre-commit hygiene hooks rewrite.  The pathspecs mirror
# what the hooks see (files git tracks or would add), which is why they are
# listed here instead of globbing the working tree: `git ls-files --cached
# --others --exclude-standard` lists the tracked files plus the untracked
# ones git would actually add, so a tracked file matching an ignore pattern
# (the pre-delete backups that shipped before that ignore existed) still
# reaches the commit and still needs the check, while an ignored scratch
# file a contributor never commits no longer fails a gate that claims to
# reproduce the commit gate.
HYGIENE_FILES = $(shell git ls-files --cached --others --exclude-standard '*.py' '*.md' '*.yml' '*.yaml' '*.toml' '*.cfg' '*.txt' '*.asm' '*.json' '*.go' '*.typed' '*.in' '*.lock' '*Dockerfile*' '*dockerignore*' '*.version' 'LICENSE' 'NOTICE' 'brand-source.json' Makefile .pre-commit-config.yaml .gitattributes .gitignore)

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
	whitespace=$$(grep -nE '[[:space:]]+$$' $(HYGIENE_FILES) || true); \
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

# The microbenchmarks in tests/test_bench.py run disabled by default
# (pyproject.toml addopts --benchmark-disable), so a plain `make test`
# never measures them.  This target enables them explicitly; it is not
# part of `check` so the default gate stays fast.
bench:  ## Run the microbenchmarks in tests/test_bench.py
	uv run --locked pytest -q tests/test_bench.py --benchmark-enable $(PYTEST_ARGS)

# The command the Code Coverage workflow runs, so the number a contributor
# reads locally and the one Codecov stores come from the same invocation.
# term-missing names the uncovered lines a contributor acts on; the XML report
# lands in build/ with the other generated output rather than the repository
# root.  The suite is not quieter here than in `test`: a coverage run over a
# partial selection measures a partial codebase, so PYTEST_ARGS is deliberately
# not passed through.
coverage:  ## Run the suite under coverage (terminal report plus build/coverage.xml)
	@mkdir -p build
	uv run --locked pytest --cov=resembl --cov-report=term-missing --cov-report=xml:build/coverage.xml

db-test:  ## Run only the PostgreSQL and MySQL integration tests (needs both URLs set)
	@if [ -z "$${RESEMBL_TEST_PG_URL:-}" ] || [ -z "$${RESEMBL_TEST_MYSQL_URL:-}" ]; then \
		echo "make db-test needs RESEMBL_TEST_PG_URL and RESEMBL_TEST_MYSQL_URL,"; \
		echo "the two CI services (.github/workflows/tests.yml). Point them at your own servers:"; \
		echo "  RESEMBL_TEST_PG_URL=postgresql+pg8000://user:pass@host/db \\"; \
		echo "  RESEMBL_TEST_MYSQL_URL=mysql+pymysql://user:pass@host/db make db-test"; \
		exit 1; \
	fi
	uv run --locked pytest -q $(DB_TESTS) $(PYTEST_ARGS)

# Scheduled operator backup (docs/restore.md).  BACKUP_DIR must name a
# directory on different storage from the live database: a backup that
# shares the failure domain protects only against a deleted file, not
# instance loss.  The target fails the cron job loudly — a missing
# BACKUP_DIR, a non-zero backup exit, or an unverified copy — instead of
# leaving a silent gap in the restore points.  `resembl backup` already
# verifies the published copy and exits 1 when it cannot be trusted.
BACKUP_DIR ?=
BACKUP_KEEP ?= 7

backup:  ## Take a verified backup into BACKUP_DIR (cron entry point, docs/restore.md)
	@if [ -z "$(BACKUP_DIR)" ]; then \
		echo "make backup needs BACKUP_DIR=<off-host-dir> (docs/restore.md)."; \
		echo "Point it at different storage from the live database, e.g.:"; \
		echo "  make backup BACKUP_DIR=/mnt/backups/resembl"; \
		exit 1; \
	fi
	uv run --locked python -m resembl backup "$(BACKUP_DIR)" --keep "$(BACKUP_KEEP)" --force

# Restore drill (docs/restore.md): copies the newest verified backup to a
# scratch directory, proves it opens and verifies there, and records the
# result beside the backup directory so the next operator knows restore
# was last proven when.  A backup never restored is a hypothesis.
backup-drill:  ## Prove the newest backup restores (docs/restore.md)
	@if [ -z "$(BACKUP_DIR)" ]; then \
		echo "make backup-drill needs BACKUP_DIR=<off-host-dir> (docs/restore.md)."; \
		exit 1; \
	fi
	@set -e; \
	newest=$$(ls -t "$(BACKUP_DIR)"/assembly-*.db 2>/dev/null | head -n 1); \
	if [ -z "$$newest" ]; then echo "backup-drill: no assembly-*.db in $(BACKUP_DIR)"; exit 1; fi; \
	scratch=$$(mktemp -d); trap 'rm -rf "$$scratch"' EXIT; \
	cp "$$newest" "$$scratch/restored.db"; \
	uv run --locked python -m resembl verify-backup "$$scratch/restored.db"; \
	printf '%s\t%s\t%s\n' "$$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$(basename "$$newest")" "verify-backup ok (drill)" >> "$(BACKUP_DIR)/.restore-drill.log"; \
	echo "backup-drill: $$newest restored and verified in $$scratch"

# The experimental Go query service in service/ has its own module and its own
# CI job (.github/workflows/service.yml).  It is not part of `check` so a
# Python-only contributor needs no Go toolchain.
# The Docker build honors SOURCE_DATE_EPOCH (see service/Dockerfile), so the
# commit-time epoch is passed through for a reproducible image.
service-check:  ## Run gofmt, go vet and go test -race in service/ (needs Go 1.26+)
	@if ! command -v go >/dev/null 2>&1 || ! command -v gofmt >/dev/null 2>&1; then \
		echo "make service-check needs Go 1.26+ (go and gofmt on PATH; see service/go.mod)."; \
		echo "Install Go from https://go.dev/dl/ and re-run 'make service-check'."; \
		exit 1; \
	fi
	@unformatted=$$(gofmt -l service); \
	if [ -n "$$unformatted" ]; then echo "gofmt needed:"; echo "$$unformatted"; exit 1; fi
	cd service && GOFLAGS=-mod=readonly GOTOOLCHAIN=local go vet ./...
	cd service && GOFLAGS=-mod=readonly GOTOOLCHAIN=local go test -race -count=1 ./...

# Go fuzz targets (service/fuzz_service_test.go, service/internal/lambdaproxy).
# `go test -fuzz` is the native tool; the Python `fuzz` target below is the
# Atheris one. FUZZER names a target (e.g. FuzzServeHTTP); FUZZ_SECONDS bounds it.
service-fuzz:  ## Fuzz the Go service targets for FUZZ_SECONDS, or one via FUZZER=<name>
	@if ! command -v go >/dev/null 2>&1; then \
		echo "make service-fuzz needs Go 1.26+ (see service/go.mod)."; \
		exit 1; \
	fi
	@for t in $(if $(FUZZER),$(FUZZER),FuzzHandle FuzzServeHTTP); do \
		case $$t in FuzzHandle) d=service/internal/lambdaproxy ;; *) d=service ;; esac; \
		echo "== fuzz $$t in $$d for $(FUZZ_SECONDS)s"; \
		cd $$d && go test -run=$$t -fuzz=$$t -fuzztime=$(FUZZ_SECONDS)s . || exit $$?; \
	done

# Isolate the maintained Python 3.15 Atheris build from normal development.
# atheris lives in the `fuzz` extra, so the target asks uv for it instead of
# failing on an import the contributor has to guess at.  Naming one script
# (make fuzz FUZZER=fuzz_code_tokenize.py) is the common case while working
# on a single entry point; with no name, every fuzzer runs in turn.
FUZZERS = $(notdir $(wildcard fuzzers/fuzz_*.py))
FUZZER ?=

fuzz:  ## Fuzz every entry point for FUZZ_SECONDS, or one via FUZZER=<name> (installs the fuzz extra)
	@for f in $(if $(FUZZER),$(FUZZER),$(FUZZERS)); do \
		echo "== fuzzers/$$f for $(FUZZ_SECONDS)s"; \
		UV_PROJECT_ENVIRONMENT=.venv-fuzz uv run --python 3.15.2 --locked --extra fuzz ./fuzzers/$$f -max_total_time=$(FUZZ_SECONDS) || exit $$?; \
	done

# The order mirrors .github/workflows/tests.yml and pylint.yml: the file
# hygiene the commit gate enforces, then the static checks (cheap, fixable),
# then the suite.
check:  ## Run every check CI runs, plus the commit-gate file checks
	$(MAKE) hygiene
	$(MAKE) types
	uv run --locked ruff check .
	uv run --locked black --check .
	uv run --locked pylint resembl/ tests/ fuzzers/ benchmarks/
	$(MAKE) test
	@if [ -z "$${RESEMBL_TEST_PG_URL:-}" ] || [ -z "$${RESEMBL_TEST_MYSQL_URL:-}" ]; then \
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
# `build/lib`, `build/bdist.*` and `resembl.egg-info/` go first because
# setuptools stages the wheel in `build/lib` and would otherwise zip whatever
# an earlier build left there: a module deleted from the source tree survives
# in the artifact until someone cleans the directory by hand.  Only the
# staged-wheel rows go: `build/` also holds the sbom and coverage reports
# (`sbom`, `coverage`), which a dist run must not delete.  All three are
# gitignored build output, and uv build rewrites them.  The sdist is unpacked
# under `.scratch/` rather than `mktemp -d`'s default, which is a tmpfs on
# Linux: the normalization of a full source tree then costs page cache, not RAM.
dist:  ## Build the sdist and wheel into dist/ reproducibly
	@tar --sort=name --help >/dev/null 2>&1 || { \
		echo "make dist needs GNU tar (--sort=name); on macOS install gnu-tar"; exit 1; }
	rm -rf dist
	rm -rf build/lib build/bdist.* resembl.egg-info
	umask 022; \
	SOURCE_DATE_EPOCH="$(SOURCE_DATE_EPOCH)" LC_ALL=C TZ=UTC uv build --out-dir dist
	@set -e; mkdir -p .scratch; for sdist in dist/*.tar.gz; do \
		unpack=$$(mktemp -d .scratch/sdist-XXXXXX); \
		tar --extract --file "$$sdist" --directory "$$unpack"; \
		root=$$(ls "$$unpack"); \
		tar --create --sort=name \
			--mtime="@$(SOURCE_DATE_EPOCH)" \
			--owner=0 --group=0 --numeric-owner \
			--mode='a+rX' \
			--directory "$$unpack" --file - "$$root" | gzip -n -9 > "$$sdist.tmp"; \
		mv "$$sdist.tmp" "$$sdist"; \
		rm -rf "$$unpack"; \
	done
	@set -e; for whl in dist/*.whl; do \
		tmp=$${whl}.tmp; \
		python3 -c "import sys, zipfile; src, dst = sys.argv[1], sys.argv[2]; zi = zipfile.ZipFile(src); zo = zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED); [exec('ni = zipfile.ZipInfo(i.filename, i.date_time)\nni.compress_type = i.compress_type\nni.create_system = i.create_system\nni.external_attr = (0o100644 << 16)\nzo.writestr(ni, zi.read(i.filename))') for i in zi.infolist()]; zo.close(); zi.close()" "$$whl" "$$tmp"; \
		mv "$$tmp" "$$whl"; \
	done

# Proves the claim above instead of asserting it: build twice, compare.  The
# second build gets no help from the first: `uv build` keeps no artifact cache
# and `dist` removes the output directory, so nothing but the source tree
# carries over, which is exactly what the comparison has to measure.  Touching
# `pyproject.toml` moves its mtime so the second build cannot be served by
# anything that keys on it.  `sha256sum` is coreutils; macOS ships
# `shasum -a 256` instead.  Each recipe line is its own shell, so the choice is
# repeated rather than exported.  The sums sit under `.scratch/`, the
# repository's scratch directory, so a failed comparison leaves nothing behind
# in the tree a contributor would have to notice and delete.
dist-verify:  ## Build dist/ twice and fail unless the two builds are byte-identical
	@$(MAKE) --no-print-directory dist
	@mkdir -p .scratch
	@if command -v sha256sum >/dev/null 2>&1; then \
		sha256sum dist/* > .scratch/dist-first.sha256; \
	else \
		shasum -a 256 dist/* > .scratch/dist-first.sha256; \
	fi
	@touch pyproject.toml
	@$(MAKE) --no-print-directory dist
	@if command -v sha256sum >/dev/null 2>&1; then \
		sha256sum --check .scratch/dist-first.sha256; \
	else \
		shasum -a 256 --check .scratch/dist-first.sha256; \
	fi
	@rm -f .scratch/dist-first.sha256

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
	uv export --locked --preview-features sbom-export --format cyclonedx1.5 \
		--no-dev --quiet --output-file build/sbom.cdx.json
	@echo "wrote build/sbom.cdx.json"
