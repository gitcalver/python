# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

from gitcalver import _git
from gitcalver._errors import ExitError


def detect_branch(
    dir: str | None = None,
    override: str | None = None,
    remote: str = "origin",
) -> tuple[str, str]:
    if not remote:
        msg = "remote requires a non-empty string"
        raise ExitError(msg)

    if override is not None:
        if override.startswith("refs/"):
            hash_ = _git.try_ref_hash(override, dir=dir)
            if hash_ is not None:
                return override, hash_
        else:
            hash_ = _resolve_branch_tip(override, remote=remote, dir=dir)
            if hash_ is not None:
                return override, hash_
        msg = f"branch not found: {override}"
        raise ExitError(msg)

    remote_prefix = f"refs/remotes/{remote}/"
    target = _git.symbolic_ref(f"refs/remotes/{remote}/HEAD", dir=dir)
    if target and target.startswith(remote_prefix):
        name = target.removeprefix(remote_prefix)
        hash_ = _resolve_branch_tip(name, remote=remote, dir=dir)
        if hash_ is not None:
            return name, hash_

    for name in ("main", "master"):
        hash_ = _git.try_ref_hash(f"refs/remotes/{remote}/{name}", dir=dir)
        if hash_ is not None:
            return name, _resolve_branch_tip(name, remote=remote, dir=dir) or hash_

    for name in ("main", "master"):
        hash_ = _git.try_ref_hash(f"refs/heads/{name}", dir=dir)
        if hash_ is not None:
            return name, hash_

    msg = "cannot determine default branch"
    raise ExitError(msg)


def _resolve_branch_tip(
    branch: str, *, remote: str, dir: str | None = None
) -> str | None:
    return _git.try_ref_hash(f"refs/heads/{branch}", dir=dir) or _git.try_ref_hash(
        f"refs/remotes/{remote}/{branch}", dir=dir
    )
