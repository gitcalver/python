# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import contextlib
import datetime
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from gitcalver import _git
from gitcalver._branch import detect_branch
from gitcalver._errors import (
    EXIT_DIRTY,
    EXIT_WRONG_BRANCH,
    ExitError,
    IncompleteHistoryError,
)
from gitcalver._format import Format, format_version

VERSION_RE = re.compile(r"^(\d{8})\.([1-9]\d*)$")


def is_version_string(s: str) -> bool:
    return VERSION_RE.match(s) is not None


def _date_went_backwards(older: str, newer: str) -> ExitError:
    msg = (
        f"committer date not monotonic: "
        f"older commit dated {older} has a later date than "
        f"newer commit dated {newer}"
    )
    return ExitError(msg)


@dataclass(frozen=True)
class _RepoState:
    common_dir: Path
    shallow_file: Path
    is_bare: bool
    head_hash: str


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
    if graft_file.exists():
        msg = f"commit graft file is not supported: {graft_file}"
        raise IncompleteHistoryError(msg)

    if not _git.has_commits(dir=dir):
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

    return _RepoState(
        common_dir=common_dir,
        shallow_file=common_dir / "shallow",
        is_bare=is_bare,
        head_hash=head_hash,
    )


def _stored_first_parent(commit: str, *, dir: str | None) -> str | None:
    try:
        return _git.stored_first_parent(commit, dir=dir)
    except _git.GitError as e:
        msg = "local history ended before the result could be proved"
        raise IncompleteHistoryError(msg) from e


def _history_is_complete(rev: str, *, dir: str | None, state: _RepoState) -> None:
    try:
        _git.rev_list_is_complete(rev, dir=dir)
    except _git.GitError as e:
        msg = "local history ended before reachability could be proved"
        raise IncompleteHistoryError(msg) from e

    if not state.shallow_file.is_file():
        return

    try:
        boundaries = state.shallow_file.read_text().splitlines()
    except OSError as e:
        msg = f"cannot read shallow boundary: {e}"
        raise IncompleteHistoryError(msg) from e

    for boundary in boundaries:
        if not boundary:
            continue
        status = _git.ancestor_status(boundary, rev, dir=dir)
        if status == 0:
            if _stored_first_parent(boundary, dir=dir) is not None:
                msg = "local history ended before reachability could be proved"
                raise IncompleteHistoryError(msg)
        elif status != 1:
            msg = "local history ended before reachability could be proved"
            raise IncompleteHistoryError(msg)


def _find_reachable_branch_anchor(
    rev: str,
    branch_tip: str,
    *,
    dir: str | None,
    state: _RepoState,
) -> str | None:
    try:
        count = int(
            _git.git(
                "rev-list",
                "--count",
                "--first-parent",
                branch_tip,
                f"^{rev}",
                dir=dir,
            )
        )
    except (ValueError, _git.GitError) as e:
        msg = "local history cannot prove the target's branch relationship"
        raise IncompleteHistoryError(msg) from e

    if count == 0:
        return branch_tip

    try:
        return _git.rev_parse(f"{branch_tip}~{count}^{{commit}}", dir=dir)
    except _git.GitError:
        pass

    try:
        last = _git.rev_parse(f"{branch_tip}~{count - 1}^{{commit}}", dir=dir)
    except _git.GitError as e:
        msg = "local history cannot prove the target's branch relationship"
        raise IncompleteHistoryError(msg) from e

    if _stored_first_parent(last, dir=dir) is not None:
        msg = "local history cannot prove the target's branch relationship"
        raise IncompleteHistoryError(msg)

    _history_is_complete(rev, dir=dir, state=state)
    return None


def forward(
    *,
    dir: str | None,
    revision: str | None,
    fmt: Format,
    branch_override: str | None,
    remote: str = "origin",
) -> str:
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
    if not _git.object_exists(f"{branch_hash}^{{commit}}", dir=dir):
        msg = f"selected branch tip is missing from local history: {branch_name}"
        raise IncompleteHistoryError(msg)

    version_rev = _find_reachable_branch_anchor(
        target_hash, branch_hash, dir=dir, state=state
    )
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

    date, count = walk_cohort(dir=dir, rev=version_rev)

    short_hash = ""
    if dirty and fmt.dirty_hash:
        short_hash = _git.object_id_prefix(target_hash, dir=dir)

    return format_version(fmt, date, count, dirty, short_hash)


# One entry per object name handed to CommitReader.read: (oid, parents, date).
_CommitMemo = dict[str, tuple[str, list[str], str]]


def _read_commit(
    reader: _git.CommitReader, rev: str, memo: _CommitMemo
) -> tuple[str, list[str], str] | None:
    cached = memo.get(rev)
    if cached is not None:
        return cached
    try:
        entry = reader.read(rev)
    except _git.GitError as e:
        msg = "local history ended before the result could be proved"
        raise IncompleteHistoryError(msg) from e
    if entry is None:
        return None
    memo[rev] = entry
    memo[entry[0]] = entry
    return entry


def _shallow_set(dir: str | None) -> frozenset[str]:
    try:
        shallow_file = _git.common_dir(dir=dir) / "shallow"
    except _git.GitError as e:
        msg = "cannot resolve git common directory"
        raise ExitError(msg) from e
    if not shallow_file.is_file():
        return frozenset()
    try:
        lines = shallow_file.read_text().splitlines()
    except OSError as e:
        msg = f"cannot read shallow boundary: {e}"
        raise IncompleteHistoryError(msg) from e
    return frozenset(line for line in lines if line)


def _cohort_count(
    root: str,
    date: str,
    *,
    reader: _git.CommitReader,
    memo: _CommitMemo,
    shallow: frozenset[str],
) -> int:
    """Pruned BFS from root: count commits reachable through any parent
    whose UTC committer date equals `date` (the 0.3 date cohort).

    Same date: counted and traversed. Strictly older: excluded, not
    traversed -- its own ancestors need no proof, so objects are read on
    demand and cost stays O(cohort + frontier), and an object missing below
    the pruned frontier never fails the walk. Strictly newer: the committer
    date went backwards along an ancestry edge, an error. A same-date
    commit's parent whose object is absent, or a same-date commit recorded
    as a shallow boundary with stored parents, leaves the cohort
    unprovable. The shallow check is deliberate even when the boundary's
    stored parents happen to be present locally: the reference
    implementation cannot see past traversal grafts, and every
    implementation must agree. A true root is a commit with no stored
    parents (raw objects expose stored parents even at shallow
    boundaries) -- real clones list depth-cut roots in the shallow file,
    and those hide nothing.
    """
    root_entry = _read_commit(reader, root, memo)
    if root_entry is None:
        msg = "local history ended before the result could be proved"
        raise IncompleteHistoryError(msg)
    root_oid, root_parents, _root_date = root_entry
    visited = {root_oid}
    queue = deque([(root_oid, root_parents)])
    count = 0
    while queue:
        node, parents = queue.popleft()
        count += 1
        if parents and node in shallow:
            msg = f"local history ended inside the {date} date block"
            raise IncompleteHistoryError(msg)
        for parent in parents:
            if parent in visited:
                continue
            entry = _read_commit(reader, parent, memo)
            if entry is None:
                msg = f"local history ended inside the {date} date block"
                raise IncompleteHistoryError(msg)
            parent_oid, parent_parents, parent_date = entry
            if parent_date == date:
                visited.add(parent_oid)
                queue.append((parent_oid, parent_parents))
            elif parent_date > date:
                raise _date_went_backwards(parent_date, date)
            # else strictly older: prune, don't count, don't traverse.
    return count


def walk_cohort(*, dir: str | None, rev: str) -> tuple[str, int]:
    shallow = _shallow_set(dir)
    with contextlib.closing(_git.CommitReader(dir=dir)) as reader:
        memo: _CommitMemo = {}
        entry = _read_commit(reader, rev, memo)
        if entry is None:
            msg = "no commits found"
            raise ExitError(msg)
        oid, _parents, date = entry
        count = _cohort_count(oid, date, reader=reader, memo=memo, shallow=shallow)
    return date, count


def reverse(
    *,
    dir: str | None,
    version_str: str,
    branch_override: str | None,
    short: bool,
    remote: str = "origin",
) -> str:
    _validate_repo(dir)

    match = VERSION_RE.match(version_str)
    if not match:
        msg = f"not a gitcalver version or git revision: {version_str}"
        raise ExitError(msg)

    date_str = match.group(1)
    n = int(match.group(2))

    try:
        datetime.date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    except ValueError:
        msg = f"invalid date in version: {version_str}"
        raise ExitError(msg) from None

    branch_name, branch_hash = detect_branch(
        dir=dir, override=branch_override, remote=remote
    )
    if not _git.object_exists(f"{branch_hash}^{{commit}}", dir=dir):
        msg = f"selected branch tip is missing from local history: {branch_name}"
        raise IncompleteHistoryError(msg)

    candidates: list[str] = []
    # git log is newest-first; a later date on an older commit is non-monotonic.
    newer_date: str | None = None
    last_hash: str | None = None
    try:
        with contextlib.closing(_git.first_parent_log(branch_hash, dir=dir)) as entries:
            for commit_hash, commit_date in entries:
                last_hash = commit_hash
                if newer_date is not None and commit_date > newer_date:
                    raise _date_went_backwards(commit_date, newer_date)
                newer_date = commit_date
                if commit_date == date_str:
                    candidates.append(commit_hash)
                elif commit_date < date_str:
                    # Dates are non-increasing (checked above); no earlier matches.
                    return _select_reverse_candidate(
                        candidates,
                        n=n,
                        version_str=version_str,
                        date_str=date_str,
                        short=short,
                        dir=dir,
                    )
    except _git.GitError as e:
        msg = "local history ended before version could be proved"
        raise IncompleteHistoryError(msg) from e

    if last_hash is None or _stored_first_parent(last_hash, dir=dir) is not None:
        msg = "local history ended before version could be proved"
        raise IncompleteHistoryError(msg)

    return _select_reverse_candidate(
        candidates,
        n=n,
        version_str=version_str,
        date_str=date_str,
        short=short,
        dir=dir,
    )


def _select_reverse_candidate(
    candidates: list[str],
    *,
    n: int,
    version_str: str,
    date_str: str,
    short: bool,
    dir: str | None,
) -> str:
    if not candidates:
        msg = f"version not found: {version_str}"
        raise ExitError(msg)

    # candidates is newest-first; a member's cohort count strictly increases
    # oldest to newest (each newer member's cohort is a strict superset), so
    # scanning oldest-first and stopping once the count passes n is exact --
    # a gap can never be filled by a later (larger) member. One reader and
    # memo serve every member's walk: the memo is pure object data, so
    # sharing it cannot change any walk's outcome, only avoid re-reads.
    target_hash: str | None = None
    shallow = _shallow_set(dir)
    with contextlib.closing(_git.CommitReader(dir=dir)) as reader:
        memo: _CommitMemo = {}
        for candidate in reversed(candidates):
            count = _cohort_count(
                candidate, date_str, reader=reader, memo=memo, shallow=shallow
            )
            if count == n:
                target_hash = candidate
                break
            if count > n:
                break

    if target_hash is None:
        msg = f"version not found: {version_str}"
        raise ExitError(msg)

    if short:
        return _git.object_id_prefix(target_hash, dir=dir)
    return target_hash
