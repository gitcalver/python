# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

from gitcalver import _git
from gitcalver._errors import ExitError


def detect_branch(
    dir: str | None = None, override: str | None = None
) -> tuple[str, str]:
    if override is not None:
        if "/" in override:
            candidates = [override]
        else:
            candidates = [
                f"refs/remotes/origin/{override}",
                f"refs/heads/{override}",
            ]
        for ref in candidates:
            hash_ = _git.try_ref_hash(ref, dir=dir)
            if hash_ is not None:
                name = override.rsplit("/", 1)[-1]
                return name, hash_
        msg = f"branch not found: {override}"
        raise ExitError(msg)

    target = _git.symbolic_ref("refs/remotes/origin/HEAD", dir=dir)
    if target:
        hash_ = _git.try_ref_hash(target, dir=dir)
        if hash_ is not None:
            name = target.removeprefix("refs/remotes/origin/")
            return name, hash_

    for name in ("main", "master"):
        hash_ = _git.try_ref_hash(f"refs/remotes/origin/{name}", dir=dir)
        if hash_ is not None:
            return name, hash_

    for name in ("main", "master"):
        hash_ = _git.try_ref_hash(f"refs/heads/{name}", dir=dir)
        if hash_ is not None:
            return name, hash_

    msg = "cannot determine default branch"
    raise ExitError(msg)


def is_on_branch(
    target_hash: str,
    branch_hash: str,
    dir: str | None = None,
) -> bool:
    # Optimization: git treats a commit as its own ancestor, so the
    # is_ancestor call below would handle this case correctly, but
    # this avoids spawning git for the common same-hash case.
    if target_hash == branch_hash:
        return True

    return _git.is_ancestor(target_hash, branch_hash, dir=dir)
