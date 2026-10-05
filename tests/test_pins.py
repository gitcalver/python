# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_USES = re.compile(
    r"uses: gitcalver/sh@(?P<currentDigest>[0-9a-f]{40}) # (?P<currentValue>v\S+)"
)
# A JSON5 string literal (group 1) or a comment, so that comments are skipped
# and "//" inside a string is not mistaken for one.
_STRING_OR_COMMENT = re.compile(r'("(?:[^"\\]|\\.)*")|//[^\n]*|/\*.*?\*/', re.DOTALL)


def _renovate_patterns() -> list[re.Pattern[str]]:
    text = (ROOT / ".github" / "renovate.json5").read_text()
    literals = [m[1] for m in _STRING_OR_COMMENT.finditer(text) if m[1]]
    patterns = [
        re.compile(re.sub(r"\(\?<(?=[A-Za-z])", "(?P<", json.loads(literal)))
        for literal in literals
        if "gitcalver/sh" in literal and "(?<currentDigest>" in literal
    ]
    assert patterns, (
        "renovate.json5: no double-quoted matchStrings regex for gitcalver/sh "
        "with a (?<currentDigest>) group"
    )
    return patterns


def _pins(patterns: list[re.Pattern[str]], path: str) -> list[tuple[str, str]]:
    text = (ROOT / path).read_text()
    return [
        (m["currentDigest"], m["currentValue"])
        for pattern in patterns
        for m in pattern.finditer(text)
    ]


def test_gitcalver_sh_pins_agree() -> None:
    renovate = _renovate_patterns()
    pins = {
        "Makefile CONFORMANCE_SHA": _pins(renovate, "Makefile"),
        "ci.yml conformance ref": _pins(renovate, ".github/workflows/ci.yml"),
        "ci.yml uses": _pins([_USES], ".github/workflows/ci.yml"),
        "release.yml uses": _pins([_USES], ".github/workflows/release.yml"),
    }

    for where, found in pins.items():
        assert found, f"{where}: pin not found in the format Renovate matches"
    assert len(pins["Makefile CONFORMANCE_SHA"]) == 1
    assert len(pins["ci.yml conformance ref"]) == 1
    distinct = {pin for found in pins.values() for pin in found}
    assert len(distinct) == 1, f"gitcalver/sh pins disagree: {pins}"


def test_makefile_pin_is_exactly_the_sha() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(r"^CONFORMANCE_SHA := [0-9a-f]{40}$", makefile, re.MULTILINE), (
        "Makefile: CONFORMANCE_SHA must be the bare 40-hex SHA; "
        "Make keeps trailing spaces and comments in the value"
    )
