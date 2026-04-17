# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

from __future__ import annotations

from typing import TYPE_CHECKING

from hatchling.plugin import hookimpl

if TYPE_CHECKING:
    from hatchling.version.source.plugin.interface import VersionSourceInterface


@hookimpl
def hatch_register_version_source() -> type[VersionSourceInterface]:
    from gitcalver._hatch_source import GitCalverSource

    return GitCalverSource
