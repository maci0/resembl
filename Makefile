# Thin wrapper over the commands the project already uses.  Every target
# runs a command documented in README.md / CONTRIBUTING.md and the same
# command CI runs, so `make check` reproduces the pipeline locally.
#
# `make help` lists the targets.

.PHONY: help install format lint types test check

PYTEST_ARGS ?=

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

# The order mirrors .github/workflows/tests.yml and pylint.yml: static
# checks first (cheap, fixable), then the suite.
check:  ## Run every check CI runs
	$(MAKE) types
	uv run ruff check .
	uv run black --check .
	uv run pylint resembl/ tests/ fuzzers/
	$(MAKE) test
