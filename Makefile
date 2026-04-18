.PHONY: sync test lint fmt typecheck

sync:
	uv sync --frozen

test: sync
	uv run pytest

lint: sync
	uv run ruff check
	uv run ruff format --check

fmt: sync
	uv run ruff format

typecheck: sync
	uv run ty check
