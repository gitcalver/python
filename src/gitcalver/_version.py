# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import contextlib
import datetime
import re

from gitcalver import _git
from gitcalver._branch import detect_branch, is_on_branch
from gitcalver._errors import EXIT_DIRTY, EXIT_WRONG_BRANCH, ExitError
from gitcalver._format import Format, format_version

VERSION_RE = re.compile(r"^(\d{8})\.([1-9]\d*)$")


def is_version_string(s: str) -> bool:
    return VERSION_RE.match(s) is not None


def _date_went_backwards(older: str, newer: str) -> ExitError:
    return ExitError(
        f"committer date not monotonic: "
        f"older commit dated {older} has a later date than "
        f"newer commit dated {newer}",
    )


def _validate_repo(dir: str | None) -> None:
    try:
        is_repo = _git.is_git_repo(dir=dir)
    except _git.GitError as e:
        raise ExitError(str(e)) from e
    if not is_repo:
        raise ExitError("not a git repository")
    try:
        shallow = _git.is_shallow(dir=dir)
    except _git.GitError as e:
        raise ExitError(f"cannot determine repository state: {e}") from e
    if shallow:
        raise ExitError(
            "shallow clone detected; full history is required"
            " (use git fetch --unshallow)",
        )
    if not _git.has_commits(dir=dir):
        raise ExitError("no commits in repository")


def forward(
    *,
    dir: str | None,
    revision: str | None,
    fmt: Format,
    branch_override: str | None,
) -> str:
    _validate_repo(dir)

    is_head = revision is None
    rev_spec = f"{'HEAD' if is_head else revision}^{{commit}}"
    try:
        target_hash = _git.rev_parse(rev_spec, dir=dir)
    except _git.GitError:
        msg = (
            "no commits in repository"
            if is_head
            else f"not a gitcalver version or git revision: {revision}"
        )
        raise ExitError(msg) from None

    branch_name, branch_hash = detect_branch(dir=dir, override=branch_override)

    version_rev = target_hash
    off_branch = False

    if not is_on_branch(target_hash, branch_hash, dir=dir):
        if not is_head:
            raise ExitError(
                f"{revision} is not on the default branch ({branch_name})",
                EXIT_WRONG_BRANCH,
            )
        mb = _git.merge_base(target_hash, branch_hash, dir=dir)
        if mb is None:
            raise ExitError(
                f"HEAD is not traceable to the default branch ({branch_name})",
                EXIT_WRONG_BRANCH,
            )
        off_branch = True
        version_rev = mb

    dirty = False
    if off_branch or (is_head and _git.is_dirty(dir=dir)):
        if fmt.dirty_suffix is None:
            if off_branch:
                msg = (
                    f"HEAD is off the default branch ({branch_name});"
                    " use --dirty to produce a divergent version"
                )
            else:
                msg = "workspace is dirty; use --dirty to allow"
            raise ExitError(msg, EXIT_DIRTY)
        dirty = True

    date, count = walk_first_parent(dir=dir, rev=version_rev)

    short_hash = ""
    if dirty and fmt.dirty_hash:
        short_hash = _git.rev_parse_short(target_hash, dir=dir)

    return format_version(fmt, date, count, dirty, short_hash)


def walk_first_parent(*, dir: str | None, rev: str) -> tuple[str, int]:
    with contextlib.closing(_git.first_parent_log(rev, dir=dir)) as entries:
        first = next(entries, None)
        if first is None:
            raise ExitError("no commits found")
        date = first[1]
        count = 1

        # Only the first date transition matters: once the date changes,
        # we're done counting. Monotonicity is only checked within the
        # target date's run; earlier violations are not surfaced here.
        for _, entry_date in entries:
            if entry_date != date:
                if entry_date > date:
                    raise _date_went_backwards(entry_date, date)
                break
            count += 1

    return date, count


def reverse(
    *,
    dir: str | None,
    version_str: str,
    branch_override: str | None,
    short: bool,
) -> str:
    _validate_repo(dir)

    match = VERSION_RE.match(version_str)
    if not match:
        raise ExitError(f"not a gitcalver version or git revision: {version_str}")

    date_str = match.group(1)
    n = int(match.group(2))

    try:
        datetime.date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    except ValueError:
        raise ExitError(f"invalid date in version: {version_str}") from None

    _, branch_hash = detect_branch(dir=dir, override=branch_override)

    candidates: list[str] = []
    # git log is newest-first; a later date on an older commit is non-monotonic.
    newer_date: str | None = None
    with contextlib.closing(_git.first_parent_log(branch_hash, dir=dir)) as entries:
        for commit_hash, commit_date in entries:
            if newer_date is not None and commit_date > newer_date:
                raise _date_went_backwards(commit_date, newer_date)
            newer_date = commit_date
            if commit_date == date_str:
                candidates.append(commit_hash)
            elif commit_date < date_str:
                # Dates are non-increasing (checked above); no earlier matches.
                break

    if n > len(candidates):
        raise ExitError(f"version not found: {version_str}")

    # N=1 is oldest on that date; candidates are newest-first.
    target_hash = candidates[-n]

    if short:
        return _git.rev_parse_short(target_hash, dir=dir)
    return target_hash
