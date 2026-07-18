# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

from gitcalver._errors import (
    EXIT_DIRTY,
    EXIT_ERROR,
    EXIT_INCOMPLETE_HISTORY,
    EXIT_WRONG_BRANCH,
    ExitError,
    IncompleteHistoryError,
)
from gitcalver._format import Format
from gitcalver._version import forward, reverse


def get_version(
    *,
    revision: str | None = None,
    prefix: str = "",
    dirty: str = "",
    dirty_hash: bool = True,
    branch: str | None = None,
    remote: str = "origin",
    repo: str | None = None,
) -> str:
    _validate_prefix(prefix)
    _validate_dirty(dirty)
    fmt = Format(prefix=prefix, dirty_suffix=dirty or None, dirty_hash=dirty_hash)
    return forward(
        dir=repo,
        revision=revision,
        fmt=fmt,
        branch_override=branch or None,
        remote=remote,
    )


def find_commit(
    version: str,
    *,
    prefix: str = "",
    branch: str | None = None,
    remote: str = "origin",
    repo: str | None = None,
    short: bool = False,
) -> str:
    _validate_prefix(prefix)
    if prefix and not version.startswith(prefix):
        msg = f'version {version} is missing required prefix "{prefix}"'
        raise ExitError(msg)
    return reverse(
        dir=repo,
        version_str=version.removeprefix(prefix),
        branch_override=branch or None,
        short=short,
        remote=remote,
    )


def _validate_prefix(prefix: str) -> None:
    if "\n" in prefix or "\r" in prefix:
        msg = "prefix must not contain a newline"
        raise ExitError(msg)


def _validate_dirty(dirty: str) -> None:
    if "\n" in dirty or "\r" in dirty:
        msg = "dirty suffix must not contain a newline"
        raise ExitError(msg)


__all__ = [
    "EXIT_DIRTY",
    "EXIT_ERROR",
    "EXIT_INCOMPLETE_HISTORY",
    "EXIT_WRONG_BRANCH",
    "ExitError",
    "IncompleteHistoryError",
    "find_commit",
    "get_version",
]
