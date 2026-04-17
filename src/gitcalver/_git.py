# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import os
import subprocess
from collections.abc import Generator


class GitError(Exception):
    pass


_SHORT_HASH_LEN = 7


def _os_error_message(e: OSError) -> str:
    if e.filename == "git":
        return "git not found on PATH"
    return str(e)


def _run(*args: str, dir: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=dir,
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
    return git("rev-parse", rev, dir=dir)


def rev_parse_short(rev: str, dir: str | None = None) -> str:
    # Pin the minimum length so output doesn't vary with core.abbrev.
    return git("rev-parse", f"--short={_SHORT_HASH_LEN}", rev, dir=dir)


def is_git_repo(dir: str | None = None) -> bool:
    return git_ok("rev-parse", "--git-dir", dir=dir)


def is_shallow(dir: str | None = None) -> bool:
    return git("rev-parse", "--is-shallow-repository", dir=dir) == "true"


def has_commits(dir: str | None = None) -> bool:
    return git_ok("rev-parse", "--verify", "HEAD", dir=dir)


def is_dirty(dir: str | None = None) -> bool:
    try:
        return git("status", "--porcelain", dir=dir) != ""
    except GitError:
        return False


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


def is_ancestor(commit: str, ancestor_of: str, dir: str | None = None) -> bool:
    return git_ok("merge-base", "--is-ancestor", commit, ancestor_of, dir=dir)


def merge_base(rev1: str, rev2: str, dir: str | None = None) -> str | None:
    try:
        return git("merge-base", rev1, rev2, dir=dir)
    except GitError:
        return None


def first_parent_log(
    rev: str, dir: str | None = None
) -> Generator[tuple[str, str], None, None]:
    env = {**os.environ, "TZ": "UTC"}
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
            env=env,
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
        if proc.wait() != 0:
            msg = f"git log {rev} failed"
            raise GitError(msg)
