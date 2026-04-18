.PHONY: sync test lint fmt

sync:
	uv sync --frozen

test: sync
	uv run pytest

lint: sync
	uv run ruff check
	uv run ruff format --check
	uv run ty check

fmt: sync
	uv run ruff format
