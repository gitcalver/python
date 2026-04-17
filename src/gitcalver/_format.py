# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

from dataclasses import dataclass


@dataclass(frozen=True)
class Format:
    prefix: str
    # None means dirty workspaces are not allowed; otherwise the string
    # is appended to versions built from a dirty workspace.
    dirty_suffix: str | None
    dirty_hash: bool


def format_version(
    fmt: Format, date: str, count: int, dirty: bool, short_hash: str
) -> str:
    version = f"{fmt.prefix}{date}.{count}"
    if dirty and fmt.dirty_suffix is not None:
        version += fmt.dirty_suffix
        if fmt.dirty_hash:
            version += f".{short_hash}"
    return version
