# Thin wrapper over the commands the project already uses.  Every target
# runs a command documented in README.md / CONTRIBUTING.md and the same
# command CI runs, so `make check` reproduces the pipeline locally.
#
# `make help` lists the targets.

.PHONY: help install format lint types test db-test check

PYTEST_ARGS ?=

# The PostgreSQL and MySQL integration modules skip themselves unless
# RESEMBL_TEST_PG_URL / RESEMBL_TEST_MYSQL_URL are set.  CI sets both, so a
# plain `make test` runs strictly less than CI does; `db-test` runs the two
# modules and names the missing variables instead of reporting a silent skip.
DB_TESTS = tests/test_pg_integration.py tests/test_mysql_integration.py

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Create .venv from uv.lock and install the git hooks
	uv sync --locked --extra dev
	uv run pre-commit install

format:  ## Apply the auto-fixes black and ruff can make
	uv run black .
	uv run ruff check --fix .

lint:  ## Run ruff and pylint
	uv run ruff check .
	uv run pylint resembl/ tests/ fuzzers/

types:  ## Run mypy over resembl/, tests/ and fuzzers/
	uv run mypy

test:  ## Run the test suite (pass args through: make test PYTEST_ARGS="-k cache")
	uv run pytest -q $(PYTEST_ARGS)

db-test:  ## Run only the PostgreSQL and MySQL integration tests (needs both URLs set)
	@if [ -z "$$RESEMBL_TEST_PG_URL" ] || [ -z "$$RESEMBL_TEST_MYSQL_URL" ]; then \
		echo "make db-test needs RESEMBL_TEST_PG_URL and RESEMBL_TEST_MYSQL_URL,"; \
		echo "the two CI services (.github/workflows/tests.yml). Point them at your own servers:"; \
		echo "  RESEMBL_TEST_PG_URL=postgresql+pg8000://user:pass@host/db \\"; \
		echo "  RESEMBL_TEST_MYSQL_URL=mysql+pymysql://user:pass@host/db make db-test"; \
		exit 1; \
	fi
	uv run pytest -q $(DB_TESTS) $(PYTEST_ARGS)

# The order mirrors .github/workflows/tests.yml and pylint.yml: static
# checks first (cheap, fixable), then the suite.
check:  ## Run every check CI runs
	$(MAKE) types
	uv run ruff check .
	uv run black --check .
	uv run pylint resembl/ tests/ fuzzers/
	$(MAKE) test
	@if [ -z "$$RESEMBL_TEST_PG_URL" ] || [ -z "$$RESEMBL_TEST_MYSQL_URL" ]; then \
		echo "note: the PostgreSQL and MySQL integration tests were skipped (unset"; \
		echo "RESEMBL_TEST_PG_URL / RESEMBL_TEST_MYSQL_URL). CI runs them; 'make db-test' does too."; \
	fi
