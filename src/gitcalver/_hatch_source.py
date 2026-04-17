# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

from hatchling.version.source.plugin.interface import VersionSourceInterface

from gitcalver import ExitError, get_version


class GitCalverSource(VersionSourceInterface):
    PLUGIN_NAME = "gitcalver"

    def get_version_data(self) -> dict[str, str]:
        config = self.config
        if config.get("no-dirty-hash", False) and not config.get("dirty", ""):
            msg = "gitcalver: no-dirty-hash requires dirty"
            raise RuntimeError(msg)
        try:
            version = get_version(
                prefix=str(config.get("prefix", "")),
                dirty=str(config.get("dirty", "")),
                dirty_hash=not config.get("no-dirty-hash", False),
                branch=config.get("branch") or None,
                repo=self.root,
            )
        except ExitError as e:
            msg = f"gitcalver: {e.message}"
            raise RuntimeError(msg) from e
        return {"version": version}
