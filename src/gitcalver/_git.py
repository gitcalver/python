# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import datetime
import os
import subprocess
from collections.abc import Generator
from pathlib import Path


class GitError(Exception):
    pass


_HASH_PREFIX_LEN = 7
_GIT_DIE_STATUS = 128


def _os_error_message(e: OSError) -> str:
    if e.filename == "git":
        return "git not found on PATH"
    return str(e)


def _env(*, utc: bool = False) -> dict[str, str]:
    env = {
        **os.environ,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    if utc:
        env["TZ"] = "UTC"
    return env


def _run(*args: str, dir: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            errors="surrogateescape",
            cwd=dir,
            env=_env(),
            check=False,
        )
    except OSError as e:
        raise GitError(_os_error_message(e)) from e


def git(*args: str, dir: str | None = None) -> str:
    result = _run(*args, dir=dir)
    if result.returncode != 0:
        raise GitError(result.stderr.strip())
    return result.stdout.strip()


def git_ok(*args: str, dir: str | None = None) -> bool:
    return _run(*args, dir=dir).returncode == 0


def rev_parse(rev: str, dir: str | None = None) -> str:
    return git("rev-parse", "--verify", rev, dir=dir)


def object_id_prefix(rev: str, dir: str | None = None) -> str:
    """Return the contract-defined, fixed-width object-ID prefix.

    This is a version component, not an unambiguous Git revision. GitCalVer
    requires exactly the first seven lowercase object-ID characters even when
    Git's repository-dependent abbreviation would be longer.
    """
    return rev_parse(rev, dir=dir)[:_HASH_PREFIX_LEN].lower()


# os.path, not pathlib: Path.exists(), is_file(), is_dir() and is_symlink() raise
# PermissionError on some Python versions when an ancestor cannot be searched,
# while os.path treats any OSError as "no".
def _looks_like_repository(path: Path) -> bool:
    head = path / "HEAD"
    return os.path.lexists(path / ".git") or (
        # A symlink to a ref that does not exist yet is a valid HEAD.
        os.path.lexists(head)
        and not os.path.isdir(head)  # noqa: PTH112
        and os.path.isdir(path / "objects")  # noqa: PTH112
        and os.path.isdir(path / "refs")  # noqa: PTH112
    )


def _repository_may_exist(dir: str | None) -> bool:
    """Return whether git's discovery from `dir` could have found a repository.

    A bad GIT_DIR, an unreadable working directory or a directory on the path
    that cannot be searched counts as possible, since none of them proves a
    repository absent. GIT_CEILING_DIRECTORIES and filesystem boundaries are
    not modeled: a repository they hide above `dir` still counts as present.
    """
    if "GIT_DIR" in os.environ:
        return True
    try:
        start = Path(os.path.realpath(dir if dir is not None else Path.cwd()))
    except OSError:
        return True
    return any(
        _looks_like_repository(path) or not os.access(path, os.X_OK)
        for path in (start, *start.parents)
    )


def is_git_repo(dir: str | None = None) -> bool:
    """Return whether `dir` is inside a repository; raise GitError if git refuses.

    Git exits 128 both when no repository is found and when one is found but
    unusable (unknown extension, dubious ownership, corrupt config), so the
    two are told apart by markers on the path, not by git's message, which
    varies by version and locale. Any other status, such as a crash or a
    wrapper's refusal, is git failing rather than finding no repository.
    """
    result = _run("rev-parse", "--git-dir", dir=dir)
    if result.returncode == 0:
        return True
    if result.returncode != _GIT_DIE_STATUS or _repository_may_exist(dir):
        raise GitError(result.stderr.strip() or "git rev-parse --git-dir failed")
    return False


def has_commits(dir: str | None = None) -> bool:
    head = _run("rev-parse", "--verify", "--quiet", "HEAD", dir=dir)
    if head.returncode == 0:
        return True
    # An unborn branch and a broken ref both exit 1 without a message; only the
    # unborn branch leaves HEAD readable by symbolic-ref.
    if head.returncode == 1 and git_ok("symbolic-ref", "--quiet", "HEAD", dir=dir):
        return False
    raise GitError(head.stderr.strip() or "the reference HEAD names is broken")


def is_dirty(dir: str | None = None) -> bool:
    return git("status", "--porcelain", dir=dir) != ""


def is_bare(dir: str | None = None) -> bool:
    return git("rev-parse", "--is-bare-repository", dir=dir) == "true"


def common_dir(dir: str | None = None) -> Path:
    value = git("rev-parse", "--git-common-dir", dir=dir)
    path = Path(value)
    if path.is_absolute():
        return path
    return (Path(dir) if dir is not None else Path.cwd()).joinpath(path).resolve()


def symbolic_ref(ref: str, dir: str | None = None) -> str | None:
    try:
        return git("symbolic-ref", ref, dir=dir)
    except GitError:
        return None


def try_ref_hash(ref: str, dir: str | None = None) -> str | None:
    try:
        return git("rev-parse", "--verify", ref, dir=dir)
    except GitError:
        return None


def ancestor_status(commit: str, ancestor_of: str, dir: str | None = None) -> int:
    return _run("merge-base", "--is-ancestor", commit, ancestor_of, dir=dir).returncode


def object_exists(object_spec: str, dir: str | None = None) -> bool:
    return git_ok("cat-file", "-e", object_spec, dir=dir)


def stored_first_parent(commit: str, dir: str | None = None) -> str | None:
    data = git("cat-file", "commit", commit, dir=dir)
    for line in data.splitlines():
        if not line:
            break
        if line.startswith("parent "):
            return line.removeprefix("parent ")
    return None


def rev_list_is_complete(rev: str, dir: str | None = None) -> None:
    try:
        result = subprocess.run(
            ["git", "rev-list", rev],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="surrogateescape",
            cwd=dir,
            env=_env(),
            check=False,
        )
    except OSError as e:
        raise GitError(_os_error_message(e)) from e
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git rev-list {rev} failed")


def first_parent_log(
    rev: str, dir: str | None = None
) -> Generator[tuple[str, str], None, None]:
    try:
        proc = subprocess.Popen(
            [
                "git",
                "log",
                rev,
                "--first-parent",
                "--format=%H %cd",
                "--date=format-local:%Y%m%d",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=dir,
            env=_env(utc=True),
        )
    except OSError as e:
        raise GitError(_os_error_message(e)) from e
    with proc:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            hash_, _, date = line.strip().partition(" ")
            if date:
                yield hash_, date
        returncode = proc.wait()
        if returncode != 0:
            msg = f"git log {rev} failed"
            raise GitError(msg)


class CommitReader:
    """Read commit objects one at a time through `git cat-file --batch`.

    The date-cohort walk only ever needs the cohort itself plus its
    immediate frontier, so objects are read on demand instead of dumping the
    whole history. Raw objects also expose stored parents even at
    shallow-clone boundaries (the grafts that hide them apply to traversal,
    not object storage), so a true root is exactly a commit with no parent
    lines, and a hidden or missing parent is exactly an absent object.
    """

    def __init__(self, dir: str | None = None) -> None:
        try:
            self._proc = subprocess.Popen(
                ["git", "cat-file", "--batch"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=dir,
                env=_env(),
            )
        except OSError as e:
            raise GitError(_os_error_message(e)) from e

    def close(self) -> None:
        proc = self._proc
        if proc.stdin is not None:
            proc.stdin.close()
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait()

    def read(self, rev: str) -> tuple[str, list[str], str] | None:
        """Return (oid, parent oids, UTC committer date) or None if absent.

        One request and exactly one response per call, so the pipe cannot
        deadlock. The date is derived from the committer epoch seconds,
        ignoring the stored timezone offset.
        """
        proc = self._proc
        if proc.stdin is None or proc.stdout is None or proc.poll() is not None:
            msg = "git cat-file exited unexpectedly"
            raise GitError(msg)
        try:
            proc.stdin.write(rev.encode() + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline()
        except OSError as e:
            raise GitError(str(e)) from e
        match header.split():
            case [_, b"missing" | b"ambiguous"]:
                return None
            case [oid_field, b"commit", size_field]:
                oid = oid_field.decode()
                size = int(size_field)
            case _:
                msg = f"unexpected cat-file response for {rev}"
                raise GitError(msg)
        body = proc.stdout.read(size + 1)[:size]
        parents: list[str] = []
        committer_epoch: int | None = None
        for line in body.split(b"\n"):
            if not line:
                break
            key, _, value = line.partition(b" ")
            if key == b"parent":
                parents.append(value.decode())
            elif key == b"committer":
                committer_epoch = int(value.rsplit(b" ", 2)[-2])
        if committer_epoch is None:
            msg = f"cannot parse commit {oid}"
            raise GitError(msg)
        date = datetime.datetime.fromtimestamp(
            committer_epoch, datetime.timezone.utc
        ).strftime("%Y%m%d")
        return oid, parents, date
