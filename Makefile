# Usage prévu : machine locale + uv (https://docs.astral.sh/uv/).
.PHONY: sync install test lint check hooks pre-commit-all deadcode cov

install: sync

sync:
	uv sync --frozen --group dev

test: sync
	uv run pytest

deadcode: sync
	uv run vulture

cov: sync
	uv run pytest -q --cov-report=term-missing:skip-covered

lint: sync
	uv run ruff check .

check: lint test deadcode
	uv run python -m compileall -q main.py malt_crm

hooks: sync
	uv run pre-commit install

pre-commit-all: sync
	uv run pre-commit run --all-files
