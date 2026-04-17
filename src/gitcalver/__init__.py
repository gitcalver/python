# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

from gitcalver._errors import EXIT_DIRTY, EXIT_ERROR, EXIT_WRONG_BRANCH, ExitError
from gitcalver._format import Format
from gitcalver._version import forward, reverse


def get_version(
    *,
    revision: str | None = None,
    prefix: str = "",
    dirty: str = "",
    dirty_hash: bool = True,
    branch: str | None = None,
    repo: str | None = None,
) -> str:
    fmt = Format(prefix=prefix, dirty_suffix=dirty or None, dirty_hash=dirty_hash)
    return forward(
        dir=repo,
        revision=revision,
        fmt=fmt,
        branch_override=branch or None,
    )


def find_commit(
    version: str,
    *,
    prefix: str = "",
    branch: str | None = None,
    repo: str | None = None,
    short: bool = False,
) -> str:
    return reverse(
        dir=repo,
        version_str=version.removeprefix(prefix),
        branch_override=branch or None,
        short=short,
    )


__all__ = [
    "EXIT_DIRTY",
    "EXIT_ERROR",
    "EXIT_WRONG_BRANCH",
    "ExitError",
    "find_commit",
    "get_version",
]
