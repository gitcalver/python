.PHONY: sync test test-conformance lint fmt

CONFORMANCE_DIR ?= ../sh
CONFORMANCE_SHA := c89d24c9ac36f0672ecfff9727532e344bfa9af9

sync:
	uv sync --frozen

test: sync
	uv run pytest

test-conformance: sync
	@test "$$(git -C "$(CONFORMANCE_DIR)" rev-parse "$(CONFORMANCE_SHA)^{commit}")" = "$(CONFORMANCE_SHA)"
	@tmp="$$(mktemp)"; \
	trap 'rm -f "$$tmp"' EXIT HUP INT TERM; \
	git -C "$(CONFORMANCE_DIR)" show "$(CONFORMANCE_SHA):test/test.sh" >"$$tmp"; \
	GITCALVER="$(CURDIR)/test/conformance-wrapper.sh" sh "$$tmp"

lint: sync
	uv run ruff check
	uv run ruff format --check
	uv run ty check

fmt: sync
	uv run ruff format
