.PHONY: sync test test-conformance lint fmt

CONFORMANCE_DIR ?= ../sh
# Renovate reads the version from the comment below. It must stay on its own
# line: Make keeps the space before a trailing comment in the value.
# gitcalver/sh v20261128.2
CONFORMANCE_SHA := a21b54bf962eded7179fe736e2d7f794d6c0696f

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

lint: actions-lock-verify-local sync
	uv run ruff check
	uv run ruff format --check
	uv run ty check

fmt: sync
	uv run ruff format

.PHONY: actions-lock-install actions-lock actions-lock-verify-local actions-lock-verify

actions-lock-install:
	gh extension install github/gh-actions-lock --pin v0.5.1 --force

actions-lock:
	gh actions-lock --no-interactive

actions-lock-verify-local:
	gh actions-lock --verify-local

actions-lock-verify:
	gh actions-lock --verify
