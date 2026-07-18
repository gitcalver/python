#!/bin/sh
# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)
exec "$ROOT/.venv/bin/python" -m gitcalver "$@"
