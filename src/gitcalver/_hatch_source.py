# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

from hatchling.version.source.plugin.interface import VersionSourceInterface

from gitcalver import ExitError, get_version


def _string_option(config: dict[str, object], name: str, default: str) -> str:
    value = config.get(name, default)
    if not isinstance(value, str):
        msg = f"gitcalver: {name} must be a string"
        raise TypeError(msg)
    return value


class GitCalverSource(VersionSourceInterface):
    PLUGIN_NAME = "gitcalver"

    def get_version_data(self) -> dict[str, str]:
        config = self.config
        prefix = _string_option(config, "prefix", "")
        dirty = _string_option(config, "dirty", "")
        branch = _string_option(config, "branch", "")
        remote = _string_option(config, "remote", "origin")
        no_dirty_hash = config.get("no-dirty-hash", False)
        if not isinstance(no_dirty_hash, bool):
            msg = "gitcalver: no-dirty-hash must be a boolean"
            raise TypeError(msg)
        if no_dirty_hash and not dirty:
            msg = "gitcalver: no-dirty-hash requires dirty"
            raise RuntimeError(msg)
        try:
            version = get_version(
                prefix=prefix,
                dirty=dirty,
                dirty_hash=not no_dirty_hash,
                branch=branch or None,
                remote=remote,
                repo=self.root,
            )
        except ExitError as e:
            msg = f"gitcalver: {e.message}"
            raise RuntimeError(msg) from e
        return {"version": version}
