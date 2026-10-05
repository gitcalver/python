.PHONY: sync test test-conformance lint fmt

CONFORMANCE_DIR ?= ../sh
# Renovate reads the version from the comment below. It must stay on its own
# line: Make keeps the space before a trailing comment in the value.
# gitcalver/sh v20260825.1
CONFORMANCE_SHA := 857287da052d1437703ead1f7d2adc76a95451ba

sync:
	uv sync --frozen

test: sync
	uv run pytest

test-conformance: sync
	@test "$$(git -C "$(CONFORMANCE_DIR)" rev-parse "$(CONFORMANCE_SHA)^{commit}")" = "$(CONFORMANCE_SHA)"
	@set -e; \
	tmp="$$(mktemp)"; \
	trap 'rm -f "$$tmp"' EXIT HUP INT TERM; \
	git -C "$(CONFORMANCE_DIR)" show "$(CONFORMANCE_SHA):test/test.sh" >"$$tmp"; \
	GITCALVER="$(CURDIR)/test/conformance-wrapper.sh" sh "$$tmp"

lint: sync
	uv run ruff check
	uv run ruff format --check
	uv run ty check

fmt: sync
	uv run ruff format
