# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import datetime
import re
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from gitcalver import _git
from gitcalver._errors import ExitError, IncompleteHistoryError

_LISTED_COMMIT = re.compile(rb"([0-9]+) commit ([0-9a-f]+)")
# What follows the last ">" of a person line, as git's split_ident_line reads it.
_IDENT_DATE = re.compile(rb"[ \t\r\n]*([0-9]+)[ \t\r\n]*[+-][0-9]")
_DAY = 86400
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
_MAX_EPOCH = (datetime.datetime.max.replace(tzinfo=datetime.UTC) - _EPOCH) // (
    datetime.timedelta(seconds=1)
)


@dataclass(frozen=True)
class _Commit:
    stamp: int
    parents: tuple[str, ...]


@dataclass(frozen=True)
class _Walk:
    commits: dict[str, _Commit]
    boundary: list[str]


def _unprovable() -> IncompleteHistoryError:
    return IncompleteHistoryError("local history cannot prove the target date cohort")


def _civil_date(epoch: int) -> str:
    if epoch > _MAX_EPOCH:
        return str(epoch)
    date = _EPOCH + datetime.timedelta(seconds=epoch)
    return f"{date.year:04}{date.month:02}{date.day:02}"


def _epoch_day(date: str) -> int:
    return (datetime.date.fromisoformat(date) - _EPOCH.date()).days


def _decreasing(found: int, after: int) -> ExitError:
    msg = (
        f"committer dates go backwards "
        f"(found {_civil_date(found)} after {_civil_date(after)} in history)"
    )
    return ExitError(msg)


def _rev_list(*args: str, dir: str | None, stdin: bytes = b"") -> _git.Listing:
    try:
        return _git.rev_list(*args, dir=dir, stdin=stdin)
    except _git.GitError as e:
        raise ExitError(str(e)) from e


def _malformed(line: bytes) -> ExitError:
    return ExitError(f"unexpected git rev-list output: {line[:80]!r}")


def _fields(line: bytes) -> list[str]:
    try:
        fields = [field.decode("ascii") for field in line.split()]
    except UnicodeDecodeError:
        raise _malformed(line) from None
    if not fields:
        raise _malformed(line)
    return fields


def _parse_line(line: bytes) -> tuple[int, str, tuple[str, ...]]:
    """Split a `--timestamp --parents` line into stamp, object ID and parents.

    A boundary commit's object ID keeps the leading `-`.
    """
    stamp, *ids = _fields(line)
    if not stamp.isdigit() or not ids:
        raise _malformed(line)
    oid, *parents = ids
    return int(stamp), oid, tuple(parents)


def _date_walk(since: int, *args: str, dir: str | None) -> _git.Listing:
    """List the commits dated at or after `since`, newest first.

    Without exclusions or ordering options, rev-list streams its walk and skips
    each commit older than --since without queueing its parents, so the only
    older commits it reads are those that bound the walk. A ^exclusion or an
    ordering option would switch git to a limited walk, which hides a
    same-date commit that is also behind an older one. --since=@<epoch> +0000
    is parsed exactly; git before 2.43.1 reads --max-age with atoi, which
    overflows after January 2038.
    """
    return _rev_list(
        "--timestamp", "--parents", f"--since=@{since} +0000", *args, dir=dir
    )


def _parse_walk(lines: Sequence[bytes]) -> _Walk:
    commits: dict[str, _Commit] = {}
    boundary: list[str] = []
    for line in lines:
        stamp, oid, parents = _parse_line(line)
        if oid.startswith("-"):
            boundary.append(oid[1:])
        else:
            commits[oid] = _Commit(stamp, parents)
    return _Walk(commits, boundary)


def _raw_listing(oids: Sequence[str], *options: str, dir: str | None) -> _git.Listing:
    return _rev_list(
        "--no-walk",
        "--stdin",
        "--encoding=none",
        "--pretty=raw",
        *options,
        dir=dir,
        stdin="".join(f"{oid}\n" for oid in oids).encode("ascii"),
    )


def _stored_parents_absent(oids: Sequence[str], *, dir: str | None) -> bool:
    """Return whether every commit in `oids` is stored locally with no parent.

    A commit that a bulk traversal treated as parentless is a genuine root only
    if its stored object is present and lists no parent; a missing object or a
    stored parent means the traversal stopped at a shallow or partial-clone
    cut instead. --pretty=raw prints the stored headers and indents the
    message; --encoding=none keeps git from re-encoding a commit that declares
    an encoding such as UTF-16, which would garble its parent lines. The match
    is anchored to the start of the line: header continuation lines (gpgsig,
    mergetag) begin with a single space, and one whose text starts with
    "parent" is not a parent header.
    """
    if not oids:
        return True
    listing = _raw_listing(oids, dir=dir)
    return listing.complete and not any(
        line.startswith(b"parent ") for line in listing.lines
    )


def _genuine_roots(oids: Sequence[str], *, dir: str | None) -> set[str]:
    if _stored_parents_absent(oids, dir=dir):
        return set(oids)
    if len(oids) == 1:
        return set()
    return {oid for oid in oids if _stored_parents_absent([oid], dir=dir)}


def _ident_stamp(ident: bytes) -> bytes | None:
    mail, end = ident.find(b"<"), ident.rfind(b">")
    found = _IDENT_DATE.match(ident, end + 1) if 0 <= mail < end else None
    return found[1] if found else None


def _malformed_date(oid: bytes) -> ExitError:
    return ExitError(
        f"malformed committer date in commit {oid.decode('ascii', 'replace')}"
    )


def _raw_commits(
    lines: Sequence[bytes],
) -> Iterator[tuple[bytes, bytes, bytes | None]]:
    oid: bytes | None = None
    stamp = b""
    committer: bytes | None = None
    for line in lines:
        if oid is None:
            if line and not line.startswith(b"    "):
                found = _LISTED_COMMIT.fullmatch(line)
                if found is None:
                    yield b"unknown", stamp, None
                    return
                stamp, oid = found[1], found[2]
                committer = None
        elif line:
            if line.startswith(b"committer "):
                committer = line.removeprefix(b"committer ")
        else:
            yield oid, stamp, committer
            oid = None
    if oid is not None:
        yield oid, stamp, None


def _check_dates(oids: Sequence[str], *, dir: str | None) -> None:
    """Require each commit's listed date to equal its committer date.

    The walk skips a commit as older by git's parse of its header, which reads
    a malformed commit's date (one with no author line, say) as 0 or a partial
    number. A commit whose date the result depends on, the target or one the
    walk skipped, must give the same date from its committer line as `%ct`
    does. The raw headers are read because a format placeholder sees the
    commit converted to UTF-8 from its declared encoding, which garbles the
    headers of one declaring UTF-16. --encoding=none keeps
    i18n.logOutputEncoding from re-encoding the listing.
    """
    if not oids:
        return
    listing = _raw_listing(oids, "--timestamp", dir=dir)
    if not listing.complete:
        raise _unprovable()
    for oid, stamp, committer in _raw_commits(listing.lines):
        if committer is None or _ident_stamp(committer) != stamp:
            raise _malformed_date(oid)


def _member_counts(
    walk: _Walk, members: Sequence[str], next_midnight: int, *, dir: str | None
) -> Iterator[tuple[str, int]]:
    """Yield each member and the size of its date cohort.

    Each member in `members` (oldest first) is on the first-parent chain of the
    next, so each cohort contains the preceding ones: the visited set carries
    over and counts each commit once. The walk must start from the newest
    member. A parent missing from the walk is one git skipped as older. Git
    lists a shallow cut as parentless, like a root; a cut in a cohort leaves
    its size unprovable, but only once the cohort is known to contain no
    newer commit, which takes precedence.
    """
    parentless = [oid for oid, commit in walk.commits.items() if not commit.parents]
    cuts = set(parentless) - _genuine_roots(parentless, dir=dir)
    seen: set[str] = set()
    count = 0
    for member in members:
        if member not in walk.commits:
            raise _unprovable()
        queue = deque([member])
        seen.add(member)
        unprovable = False
        while queue:
            current = queue.popleft()
            commit = walk.commits.get(current)
            if commit is None:
                continue
            if commit.stamp >= next_midnight:
                raise _decreasing(commit.stamp, next_midnight - _DAY)
            count += 1
            if current in cuts:
                unprovable = True
                continue
            for parent in commit.parents:
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)
        if unprovable:
            raise _unprovable()
        yield member, count


def cohort_version(rev: str, *, dir: str | None) -> tuple[str, int]:
    first = _date_walk(0, "--no-walk", rev, dir=dir)
    if not first.complete or not first.lines:
        raise _unprovable()
    start, _, _ = _parse_line(first.lines[0])
    if start > _MAX_EPOCH:
        msg = f"committer date out of range: {start}"
        raise ExitError(msg)
    date = _civil_date(start)
    midnight = start - start % _DAY

    listing = _date_walk(midnight, "--boundary", rev, dir=dir)
    if not listing.complete:
        raise _unprovable()
    walk = _parse_walk(listing.lines)
    _check_dates([rev, *walk.boundary], dir=dir)
    _, count = next(_member_counts(walk, [rev], midnight + _DAY, dir=dir))
    return date, count


def find_version_commit(
    date: str, count: str, branch_tip: str, *, version: str, dir: str | None
) -> str:
    day = _epoch_day(date)
    next_midnight = (day + 1) * _DAY
    # Git cannot store a commit date before 1970; walk the whole chain.
    start = max(day, 0) * _DAY
    not_found = ExitError(f"version not found: {version}")

    # Delimit the target date's block on the first-parent chain and prove its
    # older boundary: git read the first parent it skipped, or the chain ends
    # at the last commit listed. The commits git lists before failing on a
    # missing object still prove that dates go backwards.
    chain = _date_walk(start, "--first-parent", branch_tip, dir=dir)
    newer: int | None = None
    root: str | None = None
    skipped: str | None = None
    members: list[str] = []
    for line in chain.lines:
        stamp, oid, parents = _parse_line(line)
        if newer is not None and stamp // _DAY > newer // _DAY:
            raise _decreasing(stamp, newer)
        newer = stamp
        root = None if parents else oid
        skipped = parents[0] if parents else None
        if stamp < next_midnight:
            members.append(oid)
    if not chain.complete:
        raise _unprovable()
    if newer is None:
        _check_dates([branch_tip], dir=dir)
        raise not_found
    if root is not None and not _stored_parents_absent([root], dir=dir):
        raise _unprovable()
    if not members:
        if skipped is not None:
            _check_dates([skipped], dir=dir)
        raise not_found
    members.reverse()

    # A missing object fails git's whole walk, but cannot change versions whose
    # cohorts do not reach it. Each member's walk contains the walks of older
    # members, so failures are monotonic along the block: binary search,
    # starting from the newest member, finds the newest member whose walk
    # succeeds.
    total = len(members)
    known = 0
    failed = total + 1
    middle = total
    walk: _Walk | None = None
    while failed - known > 1:
        probe = _date_walk(start, "--boundary", members[middle - 1], dir=dir)
        if probe.complete:
            known = middle
            walk = _parse_walk(probe.lines)
        else:
            failed = middle
        middle = (known + failed) // 2
    if walk is None:
        raise _unprovable()
    _check_dates(walk.boundary, dir=dir)

    for member, size in _member_counts(walk, members[:known], next_midnight, dir=dir):
        text = str(size)
        if text == count:
            return member
        # Decimal text, since N may have more digits than int() converts. It has
        # no leading zeros, so length then text orders it exactly.
        if (len(text), text) > (len(count), count):
            raise not_found
    if known != total:
        raise _unprovable()
    raise not_found


def _reaches_all(commit: str, oids: Sequence[str], *, dir: str | None) -> bool:
    """Return whether `commit` reaches every commit in `oids`.

    A walk without exclusions lists a commit's whole ancestry whatever the
    dates.
    """
    listing = _rev_list(commit, dir=dir)
    reached = {_fields(line)[0] for line in listing.lines}
    return reached.issuperset(oids)


def find_reachable_branch_anchor(
    rev: str, branch_tip: str, *, dir: str | None
) -> str | None:
    """Return the newest first-parent commit of `branch_tip` that `rev` reaches.

    Reachability considers every parent of `rev`, so a feature branch that has
    merged the selected branch anchors at that newer selected-branch commit.
    Return None when `rev` reaches none of them, and raise
    IncompleteHistoryError when local history cannot prove either answer.
    """
    unprovable = IncompleteHistoryError(
        "local history cannot prove the target's selected-branch relationship"
    )

    # Excluding rev lists the chain down to a candidate that rev reaches. The
    # candidate is not the anchor: git's limited walk stops marking rev's
    # ancestry early under date skew and skips its missing commits, so it can
    # still list newer chain commits that rev reaches. The listing runs from
    # the tip along first parents; the candidate is the first parent of the
    # last commit listed, and is absent when that commit has no parent.
    listing = _rev_list("--first-parent", "--parents", branch_tip, f"^{rev}", dir=dir)
    if not listing.complete:
        raise unprovable
    if not listing.lines:
        return branch_tip
    chain = [_fields(line)[0] for line in listing.lines]
    last_fields = _fields(listing.lines[-1])
    last = chain[-1]
    candidate = last_fields[1] if len(last_fields) > 1 else None
    if candidate == rev:
        return rev

    # Every listed chain commit is outside the candidate's ancestry. This walk
    # lists all of rev's ancestry outside it whatever the dates, and fails on a
    # missing commit there, so rev reaches a listed chain commit exactly when
    # the walk lists it. Git lists a commit only once it has read the commit's
    # parents, so a listed tip is the anchor even if the walk fails later.
    # Commits without parents are roots or shallow cuts; behind a cut rev could
    # reach a chain commit newer than the anchor, unless the anchor itself
    # reaches the cut.
    position = {oid: index for index, oid in enumerate(chain, start=1)}
    exclusion = [] if candidate is None else [f"^{candidate}"]
    walk = _rev_list("--parents", rev, *exclusion, dir=dir)
    best: int | None = None
    anchor: str | None = None
    roots: list[str] = []
    for line in walk.lines:
        fields = _fields(line)
        oid = fields[0]
        if oid in position and (best is None or position[oid] < best):
            best = position[oid]
            anchor = oid
        if len(fields) == 1:
            roots.append(oid)
    if best == 1:
        return anchor
    if not walk.complete:
        raise unprovable
    if best is None and candidate is not None:
        anchor = candidate
    elif best is None:
        roots = [last, *roots]
    if not _stored_parents_absent(roots, dir=dir) and not (
        anchor is not None and _reaches_all(anchor, roots, dir=dir)
    ):
        raise unprovable
    return anchor
