# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import datetime
import os
import re
from dataclasses import dataclass

from gitcalver import _git
from gitcalver._branch import detect_branch
from gitcalver._errors import (
    EXIT_DIRTY,
    EXIT_WRONG_BRANCH,
    ExitError,
    IncompleteHistoryError,
)
from gitcalver._format import Format, format_version
from gitcalver._walk import (
    cohort_version,
    find_reachable_branch_anchor,
    find_version_commit,
)

VERSION_RE = re.compile(r"(\d{8})\.([1-9]\d*)", re.ASCII)


def is_version_string(s: str) -> bool:
    return VERSION_RE.fullmatch(s) is not None


@dataclass(frozen=True)
class _RepoState:
    is_bare: bool
    head_hash: str


def _check_git_arguments(*arguments: tuple[str, str | None]) -> None:
    """Reject text that cannot be a git argument or working directory.

    subprocess raises ValueError for a NUL byte and for a character that has no
    byte encoding.
    """
    for what, value in arguments:
        if value is None:
            continue
        try:
            encoded = os.fsencode(value)
        except UnicodeEncodeError as e:
            msg = (
                f"{what} contains a character that cannot be passed to git: "
                f"U+{ord(e.object[e.start]):04X}"
            )
            raise ExitError(msg) from None
        if b"\0" in encoded:
            msg = f"{what} must not contain a NUL byte"
            raise ExitError(msg)


def _validate_repo(dir: str | None) -> _RepoState:
    try:
        is_repo = _git.is_git_repo(dir=dir)
    except _git.GitError as e:
        msg = str(e)
        raise ExitError(msg) from e
    if not is_repo:
        msg = "not a git repository"
        raise ExitError(msg)
    try:
        common_dir = _git.common_dir(dir=dir)
    except _git.GitError as e:
        msg = f"cannot resolve git common directory: {e}"
        raise ExitError(msg) from e

    graft_file = common_dir / "info" / "grafts"
    if _git.path_exists(graft_file):
        msg = f"commit graft file is not supported: {graft_file}"
        raise IncompleteHistoryError(msg)

    try:
        has_commits = _git.has_commits(dir=dir)
    except _git.GitError as e:
        msg = f"cannot read HEAD: {e}"
        raise ExitError(msg) from e
    if not has_commits:
        msg = "no commits in repository"
        raise ExitError(msg)

    try:
        head_hash = _git.rev_parse("HEAD", dir=dir)
        is_bare = _git.is_bare(dir=dir)
    except _git.GitError as e:
        msg = f"cannot determine repository state: {e}"
        raise ExitError(msg) from e
    if not _git.object_exists(f"{head_hash}^{{commit}}", dir=dir):
        msg = "HEAD commit is missing from local history"
        raise IncompleteHistoryError(msg)

    return _RepoState(is_bare=is_bare, head_hash=head_hash)


def _peel_branch_tip(name: str, tip: str, *, dir: str | None) -> str:
    # A remote-tracking ref can name an annotated tag.
    try:
        return _git.rev_parse(f"{tip}^{{commit}}", dir=dir)
    except _git.GitError:
        msg = f"selected branch tip is missing from local history: {name}"
        raise IncompleteHistoryError(msg) from None


def forward(
    *,
    dir: str | None,
    revision: str | None,
    fmt: Format,
    branch_override: str | None,
    remote: str = "origin",
) -> str:
    _check_git_arguments(
        ("repository directory", dir),
        ("revision", revision),
        ("branch name", branch_override),
        ("remote name", remote),
    )
    state = _validate_repo(dir)

    is_head = revision is None
    if is_head:
        target_hash = state.head_hash
    else:
        try:
            target_hash = _git.rev_parse(f"{revision}^{{commit}}", dir=dir)
        except _git.GitError:
            try:
                resolved = _git.rev_parse(str(revision), dir=dir)
            except _git.GitError:
                resolved = None
            if resolved is not None and not _git.object_exists(resolved, dir=dir):
                msg = f"revision is missing from local history: {revision}"
                raise IncompleteHistoryError(msg) from None
            msg = f"not a gitcalver version or git revision: {revision}"
            raise ExitError(msg) from None

    branch_name, branch_hash = detect_branch(
        dir=dir, override=branch_override, remote=remote
    )
    branch_tip = _peel_branch_tip(branch_name, branch_hash, dir=dir)

    version_rev = find_reachable_branch_anchor(target_hash, branch_tip, dir=dir)
    if version_rev is None:
        target = "HEAD" if is_head else str(revision)
        msg = f"cannot trace {target} to the default branch ({branch_name})"
        raise ExitError(msg, EXIT_WRONG_BRANCH)
    off_branch = version_rev != target_hash

    dirty = False
    workspace_dirty = False
    if is_head and not off_branch and not state.is_bare:
        try:
            workspace_dirty = _git.is_dirty(dir=dir)
        except _git.GitError as e:
            msg = "local history cannot prove workspace state"
            raise IncompleteHistoryError(msg) from e

    if off_branch or workspace_dirty:
        if fmt.dirty_suffix is None:
            if off_branch:
                target = "HEAD" if is_head else str(revision)
                msg = (
                    f"{target} is off the default branch ({branch_name});"
                    " use --dirty to produce a divergent version"
                )
            else:
                msg = "workspace is dirty; use --dirty to allow"
            raise ExitError(msg, EXIT_DIRTY)
        dirty = True

    date, count = cohort_version(version_rev, dir=dir)

    short_hash = ""
    if dirty and fmt.dirty_hash:
        short_hash = _git.object_id_prefix(target_hash, dir=dir)

    return format_version(fmt, date, count, dirty, short_hash)


def reverse(
    *,
    dir: str | None,
    version_str: str,
    branch_override: str | None,
    short: bool,
    remote: str = "origin",
) -> str:
    _check_git_arguments(
        ("repository directory", dir),
        ("branch name", branch_override),
        ("remote name", remote),
    )
    _validate_repo(dir)

    match = VERSION_RE.fullmatch(version_str)
    if not match:
        msg = f"not a gitcalver version or git revision: {version_str}"
        raise ExitError(msg)

    date_str = match.group(1)
    n = match.group(2)

    try:
        datetime.date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    except ValueError:
        msg = f"invalid date in version: {version_str}"
        raise ExitError(msg) from None

    branch_name, branch_hash = detect_branch(
        dir=dir, override=branch_override, remote=remote
    )
    branch_tip = _peel_branch_tip(branch_name, branch_hash, dir=dir)

    found = find_version_commit(date_str, n, branch_tip, version=version_str, dir=dir)
    if short:
        return _git.object_id_prefix(found, dir=dir)
    return found
