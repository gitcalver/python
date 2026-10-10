# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import os
import subprocess
from pathlib import Path
from typing import NamedTuple


class GitError(Exception):
    pass


_HASH_PREFIX_LEN = 7
_GIT_DIE_STATUS = 128


def _os_error_message(e: OSError) -> str:
    if e.filename == "git":
        return "git not found on PATH"
    return str(e)


# A caller's pathspec settings would make the :/ pathspec that lists gitlinks
# match nothing or fail, and a shallow, graft or commit-graph source other than
# the repository's own would change the history that is counted.
_IGNORED_VARIABLES = frozenset(
    {
        "GIT_LITERAL_PATHSPECS",
        "GIT_GLOB_PATHSPECS",
        "GIT_NOGLOB_PATHSPECS",
        "GIT_ICASE_PATHSPECS",
        "GIT_SHALLOW_FILE",
        "GIT_GRAFT_FILE",
        "GIT_TEST_COMMIT_GRAPH",
    }
)


def _env() -> dict[str, str]:
    return {
        **{k: v for k, v in os.environ.items() if k not in _IGNORED_VARIABLES},
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _spawn(
    *args: str,
    dir: str | None,
    env: dict[str, str] | None = None,
    stdin: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    # Standard input is empty unless given because a hook git runs, such as
    # core.fsmonitor, must not consume the caller's input.
    try:
        return subprocess.run(
            ["git", *args],
            input=stdin,
            capture_output=True,
            cwd=dir,
            env=_env() if env is None else env,
            check=False,
        )
    except OSError as e:
        raise GitError(_os_error_message(e)) from e


def _run(
    *args: str, dir: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # The output is bytes decoded as a file name, since paths in it are used as
    # paths: text mode turns a CR into a line feed and decodes with the
    # locale's charset where os.path encodes with the file system's.
    result = _spawn(*args, dir=dir, env=env)
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        os.fsdecode(result.stdout),
        os.fsdecode(result.stderr),
    )


def git(*args: str, dir: str | None = None) -> str:
    result = _run(*args, dir=dir)
    if result.returncode != 0:
        raise GitError(result.stderr.strip())
    return result.stdout.removesuffix("\n")


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
def path_exists(path: Path) -> bool:
    return os.path.exists(path)  # noqa: PTH110


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


def _checked(*args: str, dir: str | None, env: dict[str, str]) -> str:
    result = _run(*args, dir=dir, env=env)
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _gitlinks(dir: str | None, env: dict[str, str]) -> list[str]:
    listing = _checked(
        "-c",
        "core.quotePath=false",
        "ls-files",
        "--stage",
        "--full-name",
        "--",
        ":/",
        dir=dir,
        env=env,
    )
    gitlinks = []
    # Not splitlines(), which also breaks at U+0085, U+2028 and U+2029: git
    # leaves them unquoted in a path.
    for line in listing.split("\n"):
        if not line.startswith("160000 "):
            continue
        path = line.partition("\t")[2]
        # Git quotes a path containing a control character, quote or backslash
        # even with core.quotePath=false; such a gitlink cannot be entered.
        if path.startswith('"'):
            msg = f"cannot enter submodule with a quoted path: {path}"
            raise GitError(msg)
        gitlinks.append(path)
    return gitlinks


def _is_populated(dot_git: Path) -> bool:
    # A path that cannot be searched may hide a populated submodule, so only a
    # missing one reads as absent.
    try:
        dot_git.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as e:
        raise GitError(str(e)) from e
    return True


def _worktree_changes(dir: str | None, env: dict[str, str]) -> bool:
    # --ignore-submodules=none governs only the submodules directly below the
    # repository it is given to, so the check repeats inside every populated
    # one. `git submodule foreach` is no substitute: it fails on a gitlink that
    # .gitmodules does not map.
    #
    # status.showUntrackedFiles=no, in any config scope, would make a tree
    # whose only change is an untracked file read clean. Settings given with -c
    # also reach the status that runs in each submodule, which does not inherit
    # GIT_NO_REPLACE_OBJECTS. Rename detection would read blobs a partial clone
    # may lack, and the optional index lock would block other git commands for
    # the whole scan.
    status = _checked(
        "--no-optional-locks",
        "-c",
        "core.useReplaceRefs=false",
        "-c",
        "status.showUntrackedFiles=normal",
        "-c",
        "status.renames=false",
        "status",
        "--porcelain",
        "--ignore-submodules=none",
        dir=dir,
        env=env,
    )
    if status:
        return True
    gitlinks = _gitlinks(dir, env)
    if not gitlinks:
        return False
    top = _checked("rev-parse", "--show-toplevel", dir=dir, env=env).removesuffix("\n")
    local_vars = _checked("rev-parse", "--local-env-vars", dir=dir, env=env).split()
    # Like git's own submodule commands, clear repository variables such as
    # GIT_INDEX_FILE, which a hook may set for the superproject, and name each
    # gitlink's repository with GIT_DIR. Discovery would apply safe.directory,
    # and from a gitlink whose .git is not a repository it would climb back to
    # the superproject and recurse without end.
    base = {
        k: v
        for k, v in env.items()
        if k not in local_vars or k in {"GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"}
    }
    for gitlink in gitlinks:
        sub = Path(top, gitlink)
        dot_git = sub / ".git"
        if not _is_populated(dot_git):
            continue
        if _worktree_changes(str(sub), {**base, "GIT_DIR": str(dot_git)}):
            return True
    return False


def is_dirty(dir: str | None = None) -> bool:
    return _worktree_changes(dir, _env())


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


def object_exists(object_spec: str, dir: str | None = None) -> bool:
    return git_ok("cat-file", "-e", object_spec, dir=dir)


class Listing(NamedTuple):
    complete: bool
    lines: list[bytes]


def rev_list(*args: str, dir: str | None = None, stdin: bytes = b"") -> Listing:
    """Run `git rev-list` over the repository's own commit objects.

    The commit-graph lets git walk past commits whose objects are missing, so
    it is off and every walk reads each commit from its object. A walk that
    fails still returns the lines printed before the failure. The final `--`
    keeps a work-tree path named like a revision from making it ambiguous.
    """
    result = _spawn(
        "-c", "core.commitGraph=false", "rev-list", *args, "--", dir=dir, stdin=stdin
    )
    output = result.stdout.removesuffix(b"\n")
    return Listing(result.returncode == 0, output.split(b"\n") if output else [])
