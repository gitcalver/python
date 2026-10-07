# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

from __future__ import annotations

import contextlib
import errno
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import gitcalver
from gitcalver._branch import detect_branch
from gitcalver._errors import ExitError, IncompleteHistoryError
from gitcalver._git import (
    CommitReader,
    GitError,
    _looks_like_repository,
    git,
    is_git_repo,
    rev_list_is_complete,
    stored_first_parent,
)
from gitcalver._hatch_hooks import hatch_register_version_source
from gitcalver._hatch_source import GitCalverSource
from gitcalver._version import reverse, walk_cohort
from gitcalver.cli import _parse_args, main, run

from _helpers import GitRepo

SENTINEL = "sentinel-4f2c9a7e git refused this directory"
NOT_A_REPO = "gitcalver: not a git repository"


def run_cmd(repo: GitRepo, *extra_args: str, branch: str = "main") -> tuple[str, int]:
    args = ["--branch", branch, *extra_args] if branch else [*extra_args]
    return run(args, dir=repo.dir)


# --- Basic version computation ---


def test_single_commit(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260410.1"


def test_three_commits_same_day(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.commit_at("2026-04-10T11:00:00Z")
    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260410.3"


def test_commits_across_days(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.commit_at("2026-04-11T09:00:00Z")
    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260411.1"


def test_day_rollover_multiple_per_day(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.commit_at("2026-04-11T09:00:00Z")
    git_repo.commit_at("2026-04-11T10:00:00Z")
    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260411.2"


# --- Prefix ---


@pytest.mark.parametrize(
    ("args", "want"),
    [
        pytest.param([], "20260410.1", id="no_prefix"),
        pytest.param(["--prefix", "0."], "0.20260410.1", id="semver"),
        pytest.param(["--prefix", "v0."], "v0.20260410.1", id="go"),
    ],
)
def test_prefix(git_repo: GitRepo, args: list[str], want: str) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo, *args)
    assert code == 0
    assert out == want


# --- Dirty workspace ---


def test_dirty_exits_2(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.write_file("dirty.txt")
    _, code = run_cmd(git_repo)
    assert code == 2


@pytest.mark.parametrize(
    ("extra_args", "want_exact", "want_prefix"),
    [
        pytest.param(["--dirty", "-dirty"], None, "20260410.1-dirty.", id="default"),
        pytest.param(
            ["--prefix", "0.", "--dirty", "-dirty"],
            None,
            "0.20260410.1-dirty.",
            id="semver",
        ),
        pytest.param(["--dirty", "+dirty"], None, "20260410.1+dirty.", id="pep440"),
        pytest.param(
            ["--prefix", "v0.", "--dirty", "-dirty"],
            None,
            "v0.20260410.1-dirty.",
            id="go",
        ),
        pytest.param(
            ["--dirty", "~dirty", "--no-dirty-hash"],
            "20260410.1~dirty",
            None,
            id="rpm",
        ),
        pytest.param(
            ["--dirty", "-SNAPSHOT", "--no-dirty-hash"],
            "20260410.1-SNAPSHOT",
            None,
            id="maven",
        ),
        pytest.param(
            ["--dirty", ".pre.dirty"], None, "20260410.1.pre.dirty.", id="ruby"
        ),
        pytest.param(["--dirty", "+dirty"], None, "20260410.1+dirty.", id="debian"),
    ],
)
def test_dirty(
    git_repo: GitRepo,
    extra_args: list[str],
    want_exact: str | None,
    want_prefix: str | None,
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.write_file("dirty.txt")
    out, code = run_cmd(git_repo, *extra_args)
    assert code == 0
    if want_exact is not None:
        assert out == want_exact
    else:
        assert want_prefix is not None
        assert out.startswith(want_prefix)


def test_gitignored_not_dirty(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.write_file(".gitignore", "ignored.txt\n")
    git_repo.git("add", ".gitignore")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.write_file("ignored.txt")
    _out, code = run_cmd(git_repo)
    assert code == 0


def _disable_show_untracked_files(
    repo: GitRepo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    scope: str,
) -> None:
    match scope:
        case "repository":
            repo.git("config", "status.showUntrackedFiles", "no")
        case "submodule":
            GitRepo(dir=str(Path(repo.dir, "sm"))).git(
                "config", "status.showUntrackedFiles", "no"
            )
        case "global":
            config = tmp_path_factory.mktemp("config") / "gitconfig"
            config.write_text("[status]\n\tshowUntrackedFiles = no\n")
            monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
        case "environment":
            monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
            monkeypatch.setenv("GIT_CONFIG_KEY_0", "status.showUntrackedFiles")
            monkeypatch.setenv("GIT_CONFIG_VALUE_0", "no")
        case _:
            raise AssertionError(scope)


@pytest.mark.parametrize("scope", ["repository", "global", "environment"])
def test_untracked_dirty_when_show_untracked_files_disabled(
    git_repo: GitRepo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    scope: str,
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.write_file("dirty.txt")
    _disable_show_untracked_files(git_repo, monkeypatch, tmp_path_factory, scope)
    _, code = run_cmd(git_repo)
    assert code == 2
    out, code = run_cmd(git_repo, "--dirty", "-dirty")
    assert code == 0
    assert out.startswith("20260410.1-dirty.")


@pytest.mark.parametrize(
    "scope", ["none", "repository", "submodule", "global", "environment"]
)
def test_submodule_untracked_dirty_when_show_untracked_files_disabled(
    git_repo: GitRepo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    scope: str,
) -> None:
    inner = tmp_path_factory.mktemp("inner")
    subprocess.run(
        ["git", "init", "-b", "main", str(inner)], capture_output=True, check=True
    )
    GitRepo(dir=str(inner)).commit_at("2026-04-10T08:00:00Z")
    git_repo.git(
        "-c", "protocol.file.allow=always", "submodule", "add", str(inner), "sm"
    )
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.write_file("sm/untracked.txt")
    if scope != "none":
        _disable_show_untracked_files(git_repo, monkeypatch, tmp_path_factory, scope)
    _, code = run_cmd(git_repo)
    assert code == 2
    out, code = run_cmd(git_repo, "--dirty", "-dirty")
    assert code == 0
    assert out.startswith("20260410.1-dirty.")


@pytest.mark.parametrize("setting", ["no", "normal", "all"])
@pytest.mark.parametrize(
    "name",
    [
        pytest.param("dirty.txt", id="file"),
        pytest.param("new/nested/dirty.txt", id="nested"),
    ],
)
def test_untracked_dirty_for_each_show_untracked_files(
    git_repo: GitRepo, setting: str, name: str
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.git("config", "status.showUntrackedFiles", setting)
    git_repo.write_file(name)
    _, code = run_cmd(git_repo)
    assert code == 2


@pytest.mark.parametrize("setting", ["no", "normal", "all"])
def test_gitignored_not_dirty_for_each_show_untracked_files(
    git_repo: GitRepo, setting: str
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.write_file(".gitignore", "ignored.txt\nignored-dir/\n")
    git_repo.git("add", ".gitignore")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.git("config", "status.showUntrackedFiles", setting)
    git_repo.write_file("ignored.txt")
    git_repo.write_file("ignored-dir/nested.txt")
    Path(git_repo.dir, "empty-dir").mkdir()
    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260410.2"


# --- Off-branch behavior ---


def test_off_branch_no_dirty_exits_2(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    out, code = run_cmd(git_repo)
    assert code == 2
    assert "off the default branch" in out


def test_off_branch_dirty_version(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    short = git_repo.head_hash()[:7]
    out, code = run_cmd(git_repo, "--dirty", "-dirty")
    assert code == 0
    assert out.startswith("20260410.1-dirty.")
    assert short in out


def test_off_branch_dirty_no_hash(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    out, code = run_cmd(git_repo, "--dirty", "-dirty", "--no-dirty-hash")
    assert code == 0
    assert out == "20260410.1-dirty"


def test_off_branch_version_from_merge_base(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-11T09:00:00Z")
    out, code = run_cmd(git_repo, "--dirty", "-dirty", "--no-dirty-hash")
    assert code == 0
    assert out == "20260410.2-dirty"


def test_off_branch_orphan_exits_3(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.git("checkout", "--orphan", "orphan")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    _, code = run_cmd(git_repo, "--dirty", "-dirty")
    assert code == 3


def test_off_branch_orphan_ignores_the_date_of_the_branch_root(
    git_repo: GitRepo,
) -> None:
    git_repo.commit_at("@253402300800 +0000")
    git_repo.git("checkout", "--orphan", "orphan")
    git_repo.commit_at("2026-04-10T10:00:00Z")

    out, code = run_cmd(git_repo, "--dirty", "-dirty")
    assert code == 3
    assert "cannot trace HEAD to the default branch" in out


# --- Error cases ---


def test_not_a_repo(tmp_path: Path) -> None:
    repo = GitRepo(dir=str(tmp_path))
    out, code = run_cmd(repo)
    assert code == 1
    assert out == NOT_A_REPO


def _git_stderr(directory: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=directory, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    return result.stderr.strip()


@pytest.mark.parametrize("git_dir", ["missing", ""], ids=["missing", "empty"])
def test_invalid_git_dir_reports_git_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_dir: str
) -> None:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / git_dir) if git_dir else "")
    said = _git_stderr(str(tmp_path), "rev-parse", "--git-dir")
    assert said

    out, code = run([], dir=str(tmp_path))
    assert code == 1
    assert out != NOT_A_REPO
    assert out == f"gitcalver: {said}"

    with pytest.raises(GitError) as raised:
        is_git_repo(dir=str(tmp_path))
    assert str(raised.value) == said


def _fake_failing_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str, status: int = 128
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    fake_git = bin_dir / "git"
    fake_git.write_text(
        f'#!/bin/sh\ncat "$(dirname "$0")/stderr.txt" >&2\nexit {status}\n'
    )
    fake_git.chmod(0o755)
    assert os.access(fake_git, os.X_OK), "tmp_path is on a noexec filesystem"
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)


# The probe walks the real filesystem above tmp_path, which can hold a repository
# when pytest runs with --basetemp or TMPDIR inside one.
@pytest.fixture
def markers_only_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gitcalver._git._looks_like_repository",
        lambda path: path.is_relative_to(tmp_path) and _looks_like_repository(path),
    )


def _add_repository_marker(directory: Path, marker: str) -> None:
    match marker:
        case "directory":
            (directory / ".git").mkdir()
        case "gitfile":
            (directory / ".git").write_text("gitdir: ../nowhere\n")
        case "broken_symlink":
            (directory / ".git").symlink_to(directory / "nowhere")
        case "bare_layout":
            (directory / "HEAD").write_text("ref: refs/heads/main\n")
            (directory / "objects").mkdir()
            (directory / "refs").mkdir()
        case "symlinked_head":
            (directory / "HEAD").symlink_to("refs/heads/main")
            (directory / "objects").mkdir()
            (directory / "refs").mkdir()
        case _:
            raise AssertionError(marker)


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param("", id="empty"),
        pytest.param("error: something entirely unrelated", id="unrelated"),
        pytest.param(
            "Schwerwiegend: Kein Git-Repository (oder eines der Elternverzeichnisse)",
            id="german",
        ),
        pytest.param(
            "fatal : ceci n'est pas un d\u00e9p\u00f4t git (ni aucun des parents)",
            id="french",
        ),
        pytest.param(
            "\u81f4\u547d\u9519\u8bef\uff1a\u4e0d\u662f git \u4ed3\u5e93", id="chinese"
        ),
        pytest.param(
            "fatal: unknown repository extensions found: bogus", id="other_failure"
        ),
    ],
)
@pytest.mark.usefixtures("markers_only_in_tmp_path")
def test_not_a_repo_does_not_depend_on_git_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    _fake_failing_git(tmp_path, monkeypatch, stderr)
    work = tmp_path / "work"
    work.mkdir()

    out, code = run([], dir=str(work))
    assert code == 1
    assert out == NOT_A_REPO
    assert is_git_repo(dir=str(work)) is False


@pytest.mark.parametrize("status", [1, 127, 129])
@pytest.mark.usefixtures("markers_only_in_tmp_path")
def test_git_failing_without_dying_is_reported_as_a_git_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _fake_failing_git(tmp_path, monkeypatch, SENTINEL, status=status)
    work = tmp_path / "work"
    work.mkdir()

    out, code = run([], dir=str(work))
    assert code == 1
    assert out == f"gitcalver: {SENTINEL}"

    with pytest.raises(GitError, match=SENTINEL):
        is_git_repo(dir=str(work))


@pytest.mark.parametrize("below", [False, True], ids=["same_dir", "subdirectory"])
@pytest.mark.parametrize(
    "marker",
    ["directory", "gitfile", "broken_symlink", "bare_layout", "symlinked_head"],
)
def test_repository_marker_relays_git_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str, below: bool
) -> None:
    _fake_failing_git(tmp_path, monkeypatch, SENTINEL)
    work = tmp_path / "work"
    work.mkdir()
    _add_repository_marker(work, marker)
    probe = work / "a" / "b" if below else work
    probe.mkdir(parents=True, exist_ok=True)

    out, code = run([], dir=str(probe))
    assert code == 1
    assert out == f"gitcalver: {SENTINEL}"

    with pytest.raises(GitError, match=SENTINEL):
        is_git_repo(dir=str(probe))


def test_repository_marker_without_stderr_names_the_failed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_failing_git(tmp_path, monkeypatch, "")
    work = tmp_path / "work"
    work.mkdir()
    _add_repository_marker(work, "directory")

    out, code = run([], dir=str(work))
    assert code == 1
    assert out == "gitcalver: git rev-parse --git-dir failed"


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param({"HEAD": "dir", "objects": "dir", "refs": "dir"}, id="head_dir"),
        pytest.param({"HEAD": "file", "objects": "dir"}, id="no_refs"),
        pytest.param({"HEAD": "file", "refs": "dir"}, id="no_objects"),
        pytest.param({"objects": "dir"}, id="lone_objects"),
        pytest.param(
            {"HEAD": "file", "objects": "file", "refs": "dir"}, id="objects_file"
        ),
        pytest.param(
            {"HEAD": "file", "objects": "dir", "refs": "file"}, id="refs_file"
        ),
    ],
)
@pytest.mark.usefixtures("markers_only_in_tmp_path")
def test_incomplete_bare_layout_is_not_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: dict[str, str]
) -> None:
    _fake_failing_git(tmp_path, monkeypatch, SENTINEL)
    work = tmp_path / "work"
    work.mkdir()
    for name, kind in layout.items():
        if kind == "dir":
            (work / name).mkdir()
        else:
            (work / name).write_text("")

    out, code = run([], dir=str(work))
    assert code == 1
    assert out == NOT_A_REPO
    assert is_git_repo(dir=str(work)) is False


def test_symlinked_directory_is_probed_through_its_physical_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_failing_git(tmp_path, monkeypatch, SENTINEL)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    link = tmp_path / "elsewhere" / "link"
    link.parent.mkdir()
    link.symlink_to(repo / "sub")

    out, code = run([], dir=str(link))
    assert code == 1
    assert out == f"gitcalver: {SENTINEL}"


@pytest.mark.parametrize("marked", [False, True], ids=["plain", "marked"])
@pytest.mark.usefixtures("markers_only_in_tmp_path")
def test_probe_defaults_to_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marked: bool
) -> None:
    _fake_failing_git(tmp_path, monkeypatch, SENTINEL)
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    if marked:
        _add_repository_marker(work, "directory")
    monkeypatch.chdir(work / "sub")

    out, code = run([])
    assert code == 1
    assert out == (f"gitcalver: {SENTINEL}" if marked else NOT_A_REPO)


def test_ceiling_directories_hiding_a_repository_still_fail(
    git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = Path(git_repo.dir, "sub")
    sub.mkdir()
    GitRepo(dir=str(sub)).git("rev-parse", "--git-dir")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", os.path.realpath(git_repo.dir))
    said = _git_stderr(str(sub), "rev-parse", "--git-dir")
    assert said

    out, code = run([], dir=str(sub))
    assert code == 1
    assert out != NOT_A_REPO
    assert out == f"gitcalver: {said}"


@pytest.mark.parametrize(
    "getcwd_succeeds", [False, True], ids=["platform_getcwd", "getcwd_succeeds"]
)
def test_unreadable_ancestor_of_current_directory_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, getcwd_succeeds: bool
) -> None:
    parent = tmp_path / "parent"
    work = parent / "work"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    cwd = str(Path.cwd())
    parent.chmod(0)
    real_access = os.access

    # Root, and filesystems that ignore mode bits, can search `parent` anyway.
    def access(path: str | os.PathLike[str], mode: int) -> bool:
        return real_access(path, mode) and not Path(path).is_relative_to(parent)

    try:
        with monkeypatch.context() as patch:
            if getcwd_succeeds:
                patch.setattr(os, "getcwd", lambda: cwd)
            patch.setattr(os, "access", access)
            out, code = run([])
    finally:
        parent.chmod(0o755)
    assert code == 1
    assert out != NOT_A_REPO
    assert out.startswith("gitcalver: ")


def test_deleted_current_directory_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()

    out, code = run([])
    assert code == 1
    assert out != NOT_A_REPO
    assert out.startswith("gitcalver: ")


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(
            "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tbogus = true\n",
            id="extension",
        ),
        pytest.param("[core\n", id="corrupt_config"),
    ],
)
def test_unusable_repo_reports_git_error(git_repo: GitRepo, config: str) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.write_file(".git/config", config)
    said = _git_stderr(git_repo.dir, "rev-parse", "--git-dir")
    assert said

    out, code = run_cmd(git_repo)
    assert code == 1
    assert out != NOT_A_REPO
    assert out == f"gitcalver: {said}"

    with pytest.raises(GitError) as raised:
        is_git_repo(dir=git_repo.dir)
    assert str(raised.value) == said


def test_non_utf8_commit_object_is_read(git_repo: GitRepo) -> None:
    when = "2026-04-10T09:00:00Z"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Andr\udce9",
        "GIT_AUTHOR_EMAIL": "andre@example.com",
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_NAME": "Andr\udce9",
        "GIT_COMMITTER_EMAIL": "andre@example.com",
        "GIT_COMMITTER_DATE": when,
    }
    git_repo.git(
        "-c",
        "i18n.commitEncoding=ISO-8859-1",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "r\udce9sum\udce9",
        env=env,
    )

    out, code = run_cmd(git_repo, "20200101.1")
    assert code == 1
    assert out == "gitcalver: version not found: 20200101.1"


def test_non_utf8_git_stderr_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_git = tmp_path / "bin" / "git"
    fake_git.parent.mkdir()
    fake_git.write_text('#!/bin/sh\nprintf "fatal: caf\\351\\n" >&2\nexit 128\n')
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_git.parent), prepend=os.pathsep)

    with pytest.raises(GitError, match="fatal: caf"):
        git("rev-parse", "HEAD", dir=str(tmp_path))
    with pytest.raises(GitError, match="fatal: caf"):
        rev_list_is_complete("HEAD", dir=str(tmp_path))


def test_empty_repo(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    repo = GitRepo(dir=str(tmp_path))
    _, code = run_cmd(repo)
    assert code == 1


def test_git_not_on_path(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    monkeypatch.setenv("PATH", "/nonexistent")
    out, code = run_cmd(git_repo)
    assert code == 1
    assert "git not found" in out


# --- Text that cannot be passed to git ---


def _rejection(entrance: str, what: str, repo: str, text: str) -> str:
    """Return the message `entrance` gives when only `what` is the bad `text`."""
    directory = text if what == "repository directory" else repo
    revision = text if what == "revision" else None
    branch = text if what == "branch name" else None
    remote = text if what == "remote name" else "origin"
    if entrance == "run":
        argv = ["--remote", remote]
        if branch is not None:
            argv += ["--branch", branch]
        if revision is not None:
            argv.append(revision)
        out, code = run(argv, dir=directory)
        assert code == 1
        assert out.startswith("gitcalver: ")
        return out.removeprefix("gitcalver: ")
    if entrance == "hatch":
        config = {"remote": remote}
        if branch is not None:
            config["branch"] = branch
        with pytest.raises(RuntimeError) as hatch_error:
            GitCalverSource(directory, config).get_version_data()
        return str(hatch_error.value).removeprefix("gitcalver: ")

    def call() -> str:
        if entrance == "get_version":
            return gitcalver.get_version(
                revision=revision, branch=branch, remote=remote, repo=directory
            )
        return gitcalver.find_commit(
            "20260410.1", branch=branch, remote=remote, repo=directory
        )

    with pytest.raises(ExitError) as exit_error:
        call()
    assert exit_error.value.code == 1
    return exit_error.value.message


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        pytest.param("a\0b", "must not contain a NUL byte", id="nul"),
        pytest.param(
            "a\ud800b",
            "contains a character that cannot be passed to git: U+D800",
            id="unencodable",
        ),
    ],
)
@pytest.mark.parametrize(
    ("entrance", "what"),
    [
        (entrance, what)
        for entrance in ("run", "get_version", "find_commit", "hatch")
        for what in ("repository directory", "revision", "branch name", "remote name")
        if what != "revision" or entrance in ("run", "get_version")
    ],
)
def test_text_git_cannot_take_is_rejected(
    git_repo: GitRepo, entrance: str, what: str, text: str, problem: str
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    assert _rejection(entrance, what, git_repo.dir, text) == f"{what} {problem}"


# --- UTC midnight boundary ---


def test_utc_midnight_boundary(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T23:59:00Z")
    git_repo.commit_at("2026-04-11T00:01:00Z")
    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260411.1"


def test_reverse_dates_are_utc_in_any_time_zone(
    git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = git_repo.commit_at("2026-04-10T20:00:00Z")
    east = {**os.environ, "TZ": "JST-9"}
    local_date = "--date=format-local:%Y%m%d"
    assert git_repo.git("log", "-1", "--format=%cd", local_date, env=east) == "20260411"
    monkeypatch.setenv("TZ", "JST-9")

    out, code = run_cmd(git_repo, "20260410.1")
    assert code == 0
    assert out == head


# --- Strictly increasing versions ---


def test_strictly_increasing_versions(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.commit_at("2026-04-11T09:00:00Z")
    git_repo.commit_at("2026-04-11T10:00:00Z")

    head = git_repo.head_hash()
    hashes = [head]
    for _ in range(3):
        hashes.append(git_repo.parent_hash(hashes[-1]))
    hashes.reverse()

    versions = []
    for h in hashes:
        out, code = run_cmd(git_repo, h)
        assert code == 0
        versions.append(out)

    for i in range(1, len(versions)):
        assert versions[i] > versions[i - 1]


# --- Decreasing committer dates ---


def test_decreasing_dates_exits_1(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-11T09:00:00Z")
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo)
    assert code == 1
    assert "committer date not monotonic" in out
    assert "older commit dated 20260411" in out
    assert "newer commit dated 20260410" in out


# --- Walk cohort: no commits ---


def test_walk_cohort_no_commits(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    with pytest.raises(ExitError, match="no commits found"):
        walk_cohort(dir=git_repo.dir, rev="HEAD..HEAD")


# --- Empty commits counted ---


def test_empty_commits_counted(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260410.2"


# --- Committer vs author date ---


def test_uses_committer_date(git_repo: GitRepo) -> None:
    git_repo.commit_at(
        "2026-04-09T09:00:00Z",
        committer_date="2026-04-10T09:00:00Z",
    )
    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260410.1"


# --- Reverse lookup ---


def test_reverse_basic(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.commit_at("2026-04-10T11:00:00Z")

    head = git_repo.head_hash()
    second = git_repo.parent_hash(head)
    third = git_repo.parent_hash(second)

    out, code = run_cmd(git_repo, "20260410.3")
    assert code == 0
    assert out == head

    out, code = run_cmd(git_repo, "20260410.2")
    assert code == 0
    assert out == second

    out, code = run_cmd(git_repo, "20260410.1")
    assert code == 0
    assert out == third


def test_reverse_prefixed(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    head = git_repo.head_hash()
    out, code = run_cmd(git_repo, "--prefix", "0.", "0.20260410.1")
    assert code == 0
    assert out == head


def test_reverse_go_prefix(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    head = git_repo.head_hash()
    out, code = run_cmd(git_repo, "--prefix", "v0.", "v0.20260410.1")
    assert code == 0
    assert out == head


def test_reverse_requires_prefix(git_repo: GitRepo) -> None:
    # With --prefix set, a bare version must not silently reverse-lookup.
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo, "--prefix", "v0.", "20260410.1")
    assert code == 1
    assert "missing required prefix" in out


def test_reverse_short(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    head = git_repo.head_hash()
    out, code = run_cmd(git_repo, "--short", "20260410.1")
    assert code == 0
    assert out == head[:7]


def test_reverse_short_ignores_core_abbrev(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.git("config", "core.abbrev", "12")
    out, code = run_cmd(git_repo, "--short", "20260410.1")
    assert code == 0
    assert len(out) == 7


def _install_fake_gpg(repo: GitRepo) -> None:
    fake_gpg = Path(repo.dir, ".git", "fake-gpg")
    fake_gpg.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        'case "$*" in\n'
        "*--verify*)\n"
        '    echo "gpg: Signature made Fri Apr 10 09:00:00 2026 UTC" >&2\n'
        "    exit 2\n"
        "    ;;\n"
        "esac\n"
        "printf '[GNUPG:] BEGIN_SIGNING\\n[GNUPG:] SIG_CREATED D 1 8 00 0 X\\n' >&2\n"
        "printf -- '-----BEGIN PGP SIGNATURE-----\\n\\nZmFrZQ==\\n"
        "-----END PGP SIGNATURE-----\\n'\n"
    )
    fake_gpg.chmod(0o755)
    repo.git("config", "gpg.program", str(fake_gpg))
    repo.git("config", "commit.gpgsign", "true")


def test_reverse_ignores_log_show_signature(git_repo: GitRepo) -> None:
    _install_fake_gpg(git_repo)
    first = git_repo.commit_at("2026-04-10T09:00:00Z")
    second = git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.git("config", "log.showSignature", "true")
    assert "gpg: Signature made" in git_repo.git("log", "--format=%H")

    out, code = run_cmd(git_repo, "20260410.1")
    assert code == 0
    assert out == first
    out, code = run_cmd(git_repo, "20260410.2")
    assert code == 0
    assert out == second


@pytest.mark.parametrize("key", ["i18n.logOutputEncoding", "i18n.commitEncoding"])
def test_reverse_ignores_log_output_encoding(git_repo: GitRepo, key: str) -> None:
    first = git_repo.commit_at("2026-04-10T09:00:00Z")
    second = git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.git("config", key, "UTF-16")
    raw = subprocess.run(
        ["git", "log", "--format=%H"], capture_output=True, cwd=git_repo.dir, check=True
    )
    assert b"\0" in raw.stdout

    out, code = run_cmd(git_repo, "20260410.1")
    assert code == 0
    assert out == first
    out, code = run_cmd(git_repo, "20260410.2")
    assert code == 0
    assert out == second


def test_reverse_ignores_inherited_log_output_encoding(
    git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "i18n.logOutputEncoding")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "UTF-16")

    out, code = run_cmd(git_repo, "20260410.1")
    assert code == 0
    assert out == first


def test_reverse_tip_hash_as_work_tree_file(git_repo: GitRepo) -> None:
    first = git_repo.commit_at("2026-04-10T09:00:00Z")
    tip = git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.write_file(tip)

    out, code = run_cmd(git_repo, "20260410.1")
    assert code == 0
    assert out == first


@pytest.mark.parametrize("separator", ["\r", "\f", "\u2028"], ids=["cr", "ff", "ls"])
def test_reverse_root_ident_resembling_parent_header(
    git_repo: GitRepo, separator: str
) -> None:
    when = "2026-04-09T09:00:00Z"
    ident = f"Mallory{separator}parent {'0' * 40}"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": ident,
        "GIT_AUTHOR_EMAIL": "mallory@example.com",
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_NAME": ident,
        "GIT_COMMITTER_EMAIL": "mallory@example.com",
        "GIT_COMMITTER_DATE": when,
    }
    git_repo.git("commit", "--allow-empty", "-m", "root", env=env)
    root = git_repo.head_hash()
    git_repo.commit_at("2026-04-10T09:00:00Z")

    out, code = run_cmd(git_repo, "20260409.1")
    assert code == 0
    assert out == root


def test_unsearchable_info_directory_is_ignored(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    info = Path(git_repo.dir, ".git", "info")
    info.chmod(0)
    try:
        out, code = run_cmd(git_repo)
    finally:
        info.chmod(0o755)
    assert code == 0
    assert out == "20260410.1"


def test_forward_tip_hash_as_work_tree_directory(git_repo: GitRepo) -> None:
    first = git_repo.commit_at("2026-04-10T09:00:00Z")
    tip = git_repo.commit_at("2026-04-10T10:00:00Z")
    Path(git_repo.dir, tip).mkdir()

    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260410.2"
    out, code = run_cmd(git_repo, first)
    assert code == 0
    assert out == "20260410.1"


def test_unrelated_history_hash_as_work_tree_directory_exits_3(
    git_repo: GitRepo,
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.git("checkout", "--orphan", "orphan")
    orphan = git_repo.commit_at("2026-04-10T10:00:00Z")
    Path(git_repo.dir, orphan).mkdir()

    out, code = run_cmd(git_repo, "--dirty", "-dirty")
    assert code == 3
    assert "cannot trace HEAD to the default branch" in out


def test_dirty_hash_ignores_core_abbrev(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.git("config", "core.abbrev", "12")
    git_repo.write_file("dirty.txt")
    out, code = run_cmd(git_repo, "--dirty", "-dirty")
    assert code == 0
    hash_part = out.rsplit(".", 1)[1]
    assert hash_part == git_repo.head_hash()[:7]


def test_reverse_not_found(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    _, code = run_cmd(git_repo, "20260410.5")
    assert code == 1


def test_reverse_empty_repo(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    repo = GitRepo(dir=str(tmp_path))
    out, code = run_cmd(repo, "20260410.1")
    assert code == 1
    assert "no commits in repository" in out


def test_reverse_date_not_in_history(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    _, code = run_cmd(git_repo, "20260501.1")
    assert code == 1


def test_reverse_round_trip(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    head = git_repo.head_hash()

    version, code = run_cmd(git_repo)
    assert code == 0
    assert version == "20260410.2"

    hash_out, code = run_cmd(git_repo, version)
    assert code == 0
    assert hash_out == head


def test_reverse_invalid_count(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    _, code = run_cmd(git_repo, "20260410.0")
    assert code == 1


def test_reverse_invalid_date_month(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo, "20261301.1")
    assert code == 1
    assert "invalid date in version" in out


def test_reverse_invalid_date_day(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo, "20260230.1")
    assert code == 1
    assert "invalid date in version" in out


@pytest.mark.parametrize("digits", [25, 4300, 4301, 5000])
def test_reverse_count_longer_than_any_commit_count_is_not_found(
    git_repo: GitRepo, digits: int
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    version = f"20260410.{'9' * digits}"
    out, code = run_cmd(git_repo, version)
    assert code == 1
    assert out == f"gitcalver: version not found: {version}"

    with pytest.raises(ExitError, match="version not found") as raised:
        gitcalver.find_commit(version, repo=git_repo.dir, branch="main")
    assert raised.value.code == 1


def test_reverse_compares_counts_numerically(git_repo: GitRepo) -> None:
    hashes = [git_repo.commit_at(f"2026-04-10T09:{n:02}:00Z") for n in range(11)]
    for n in (1, 2, 9, 10, 11):
        assert run_cmd(git_repo, f"20260410.{n}") == (hashes[n - 1], 0)
    for n in (12, 20, 100):
        assert run_cmd(git_repo, f"20260410.{n}") == (
            f"gitcalver: version not found: 20260410.{n}",
            1,
        )


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("20260410.1١", id="count"),
        pytest.param("20260410.١", id="count_first_digit"),
        pytest.param("٢٠٢٦٠٤١٠.1", id="date"),
    ],
)
def test_reverse_requires_ascii_digits(git_repo: GitRepo, version: str) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo, version)
    assert code == 1
    assert out == f"gitcalver: not a gitcalver version or git revision: {version}"


@pytest.mark.parametrize("prefix", ["", "v"], ids=["no_prefix", "prefix"])
def test_version_followed_by_a_line_break_is_not_a_version(
    git_repo: GitRepo, prefix: str
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    version = f"{prefix}20260410.1\n"

    out, code = run_cmd(git_repo, "--prefix", prefix, version)
    assert code == 1
    assert out == f"gitcalver: not a gitcalver version or git revision: {version}"

    with pytest.raises(ExitError, match="not a gitcalver version") as raised:
        gitcalver.find_commit(version, prefix=prefix, repo=git_repo.dir, branch="main")
    assert raised.value.code == 1


# --- Forward for specific revision ---


def test_specific_revision(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.commit_at("2026-04-10T11:00:00Z")

    parent = git_repo.parent_hash("HEAD")
    out, code = run_cmd(git_repo, parent)
    assert code == 0
    assert out == "20260410.2"


def test_specific_revision_with_prefix(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    head = git_repo.head_hash()
    out, code = run_cmd(git_repo, "--prefix", "0.", head)
    assert code == 0
    assert out == "0.20260410.1"


def test_specific_revision_not_on_branch(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    feature_hash = git_repo.head_hash()
    git_repo.checkout("main")
    out, code = run_cmd(git_repo, feature_hash)
    assert code == 2
    assert feature_hash in out


def test_forward_invalid_revision(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    _, code = run_cmd(git_repo, "not-a-valid-ref")
    assert code == 1


# --- Merge behavior (0.3: cohort counts all same-date parents) ---


def test_merge_counts_all_same_date_parents(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")  # A: root
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-10T10:00:00Z")  # C1: feature, parent A
    git_repo.commit_at("2026-04-10T11:00:00Z")  # C2: feature, parent C1
    git_repo.checkout("main")
    git_repo.commit_at("2026-04-10T12:00:00Z")  # B: main, parent A
    git_repo.merge("feature", "2026-04-10T13:00:00Z")  # M: parents B, C2

    out, code = run_cmd(git_repo)
    assert code == 0
    # Membership is still first-parent-chain-only, but N is the size of M's
    # date cohort: everything reachable from M through any parent that
    # shares its UTC date. {M, B, C2, C1, A} = 5; the feature-branch
    # commits are members of the cohort even though they're not chain
    # members themselves.
    assert out == "20260410.5"


def test_cross_day_merge_parent_not_counted(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")  # A: root
    git_repo.create_branch("side")
    git_repo.commit_at("2026-04-09T09:00:00Z")  # C: side, dated the day before A
    git_repo.checkout("main")
    git_repo.commit_at("2026-04-10T10:00:00Z")  # B: main, parent A
    git_repo.merge("side", "2026-04-10T11:00:00Z")  # M: parents B, C

    out, code = run_cmd(git_repo)
    assert code == 0
    # C is strictly older than M's date; it's excluded from the cohort and
    # never traversed, even though it's a direct merge parent.
    assert out == "20260410.3"


def test_root_reached_via_multiple_paths_counted_once(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")  # A: root
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-10T10:00:00Z")  # P1: feature, parent A
    git_repo.checkout("main")
    git_repo.commit_at("2026-04-10T11:00:00Z")  # P2: main, parent A
    git_repo.merge("feature", "2026-04-10T12:00:00Z")  # M: parents P2, P1

    out, code = run_cmd(git_repo)
    assert code == 0
    # A is reachable from M through both P1 and P2; the visited-once BFS
    # counts it exactly once. {M, P2, P1, A} = 4.
    assert out == "20260410.4"


# --- Incident regression: merge + fast-forward reparenting ---


def test_incident_topology_merge_ff_never_decreases(git_repo: GitRepo) -> None:
    # Reproduces the reparenting incident: main accumulates same-date
    # commits, a short-lived feature branch merges main in, then main
    # fast-forwards onto that merge. Main's own commits leave the
    # first-parent chain, but 0.3's cohort still counts them through the
    # merge's second parent, so the version never goes backwards.
    git_repo.commit_at("2026-04-10T09:00:00Z")  # A: root
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-10T10:00:00Z")  # F1: feature, parent A
    git_repo.checkout("main")
    git_repo.commit_at("2026-04-10T11:00:00Z")  # B2: main, parent A
    git_repo.commit_at("2026-04-10T12:00:00Z")  # B3: main, parent B2
    git_repo.commit_at("2026-04-10T13:00:00Z")  # B4: main, parent B3

    before_out, before_code = run_cmd(git_repo)
    assert before_code == 0
    assert before_out == "20260410.4"

    git_repo.checkout("feature")
    git_repo.merge("main", "2026-04-10T14:00:00Z")  # M: parents F1, B4
    git_repo.checkout("main")
    git_repo.git("merge", "--ff-only", "feature")

    after_out, after_code = run_cmd(git_repo)
    assert after_code == 0
    # Cohort of M: {M, F1, B4, A, B3, B2} = 6.
    assert after_out == "20260410.6"

    # Compare as (date, count) numbers: string comparison would falsely
    # order "…10" before "…9" exactly when 0.3's sparse jumps matter.
    def parsed(version: str) -> tuple[int, int]:
        date_part, _, count_part = version.partition(".")
        return int(date_part), int(count_part)

    assert parsed(after_out) > parsed(before_out)


def test_sparse_reverse_gaps(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")  # A: root
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-10T10:00:00Z")  # F1: feature, parent A
    git_repo.checkout("main")
    git_repo.commit_at("2026-04-10T11:00:00Z")  # B2: main, parent A
    git_repo.commit_at("2026-04-10T12:00:00Z")  # B3: main, parent B2
    git_repo.commit_at("2026-04-10T13:00:00Z")  # B4: main, parent B3
    git_repo.checkout("feature")
    git_repo.merge("main", "2026-04-10T14:00:00Z")  # M: parents F1, B4
    git_repo.checkout("main")
    git_repo.git("merge", "--ff-only", "feature")
    m_hash = git_repo.head_hash()
    f1_hash = git_repo.git("rev-parse", "main~1")
    a_hash = git_repo.git("rev-parse", "main~2")

    # First-parent chain membership is unchanged from 0.2: only M, F1, and A
    # are candidates for date 2026-04-10. Their cohort sizes are 6, 2, and 1
    # -- sparse. N=3,4,5 fall in the gap left by main's own commits
    # (B2-B4), which are reachable only off-chain, through the merge's
    # second parent.
    out, code = run_cmd(git_repo, "20260410.1")
    assert code == 0
    assert out == a_hash

    out, code = run_cmd(git_repo, "20260410.2")
    assert code == 0
    assert out == f1_hash

    out, code = run_cmd(git_repo, "20260410.6")
    assert code == 0
    assert out == m_hash

    for n in (3, 4, 5):
        _, code = run_cmd(git_repo, f"20260410.{n}")
        assert code == 1


# --- Pruned-walk monotonicity: near-cohort vs. buried skew ---


def test_near_cohort_future_date_errors(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")  # A: root
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-11T09:00:00Z")  # F: feature, dated a day later
    git_repo.checkout("main")
    git_repo.commit_at("2026-04-10T10:00:00Z")  # B: main, parent A
    git_repo.merge("feature", "2026-04-10T11:00:00Z")  # M: parents B, F

    out, code = run_cmd(git_repo)
    assert code == 1
    assert "committer date not monotonic" in out


def test_buried_future_date_tolerated(git_repo: GitRepo) -> None:
    # A "skewed" ancestor (dated later than its own child) exists deep in
    # history, but it's buried behind a commit that's strictly older than
    # the target's cohort date, so the pruned walk never traverses into it
    # and the skew goes unnoticed -- by design.
    git_repo.commit_at("2026-04-11T09:00:00Z")  # P: buried root, "future"-dated
    git_repo.commit_at("2026-04-05T09:00:00Z")  # O: child of P, dated much earlier
    git_repo.commit_at("2026-04-10T09:00:00Z")  # B: child of O
    git_repo.commit_at("2026-04-10T10:00:00Z")  # C: child of B (target)

    out, code = run_cmd(git_repo)
    assert code == 0
    # Cohort of C (2026-04-10): {C, B} = 2. O is strictly older and pruned
    # before P's anomalous date is ever examined.
    assert out == "20260410.2"


# --- Shallow boundaries reached through a second (merge) parent ---


def test_shallow_boundary_on_second_parent_path_exits_4(tmp_path: Path) -> None:
    origin_dir = str(tmp_path / "origin")
    subprocess.run(
        ["git", "init", "-b", "main", origin_dir],
        capture_output=True,
        check=True,
    )
    origin = GitRepo(dir=origin_dir)
    origin.commit_at("2026-04-10T09:00:00Z")  # A: root
    origin.create_branch("feature")
    origin.commit_at("2026-04-10T09:30:00Z")  # C1: feature, parent A
    origin.commit_at("2026-04-10T10:00:00Z")  # C2: feature, parent C1
    origin.checkout("main")
    origin.commit_at("2026-04-10T10:30:00Z")  # B: main, parent A
    origin.merge("feature", "2026-04-10T11:00:00Z")  # M: parents B, C2

    clone_dir = str(tmp_path / "clone")
    subprocess.run(
        ["git", "clone", "--depth", "3", f"file://{origin_dir}", clone_dir],
        capture_output=True,
        check=True,
    )
    clone = GitRepo(dir=clone_dir)
    out, code = run_cmd(clone)
    assert code == 4
    assert "local history ended" in out
    # The first-parent side (B -> A) is fully resolved down to a genuine
    # root, so only the second-parent side's shallow cut (C1) is at fault.


def test_reverse_shallow_second_parent_exits_4(tmp_path: Path) -> None:
    # Same topology as the forward test above; reverse lookup shares the
    # proof obligation through its per-member cohort scan. 20260410.2
    # resolves at B, whose cohort never reaches the shallow cut, while
    # 20260410.5 needs the merge's cohort, which does.
    origin_dir = str(tmp_path / "origin")
    subprocess.run(
        ["git", "init", "-b", "main", origin_dir],
        capture_output=True,
        check=True,
    )
    origin = GitRepo(dir=origin_dir)
    origin.commit_at("2026-04-10T09:00:00Z")  # A: root
    origin.create_branch("feature")
    origin.commit_at("2026-04-10T09:30:00Z")  # C1: feature, parent A
    origin.commit_at("2026-04-10T10:00:00Z")  # C2: feature, parent C1
    origin.checkout("main")
    origin.commit_at("2026-04-10T10:30:00Z")  # B: main, parent A
    origin.merge("feature", "2026-04-10T11:00:00Z")  # M: parents B, C2

    clone_dir = str(tmp_path / "clone")
    subprocess.run(
        ["git", "clone", "--depth", "3", f"file://{origin_dir}", clone_dir],
        capture_output=True,
        check=True,
    )
    clone = GitRepo(dir=clone_dir)

    out, code = run_cmd(clone, "20260410.2")
    assert code == 0
    assert out == clone.git("rev-parse", "HEAD~1")

    _, code = run_cmd(clone, "20260410.5")
    assert code == 4


def test_reverse_rejects_future_date_in_member_cohort(git_repo: GitRepo) -> None:
    # The requested block member's own cohort walk finds a future-dated
    # commit through the merge's second parent: a decreasing-history error
    # surfaced by the cohort machinery, not the block delimiter (the
    # first-parent chain's dates are perfectly monotonic here).
    git_repo.commit_at("2026-04-10T09:00:00Z")  # A: root
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-11T09:00:00Z")  # S1: feature, dated tomorrow
    git_repo.checkout("main")
    git_repo.merge("feature", "2026-04-10T10:00:00Z")  # M: parents A, S1

    out, code = run_cmd(git_repo, "20260410.2")
    assert code == 1
    assert "not monotonic" in out


def test_older_dated_second_parent_boundary_succeeds(tmp_path: Path) -> None:
    origin_dir = str(tmp_path / "origin")
    subprocess.run(
        ["git", "init", "-b", "main", origin_dir],
        capture_output=True,
        check=True,
    )
    origin = GitRepo(dir=origin_dir)
    origin.commit_at("2026-04-10T09:00:00Z")  # A: root
    origin.create_branch("feature")
    origin.commit_at("2026-04-09T09:00:00Z")  # C1: feature, dated the day before
    origin.commit_at("2026-04-09T10:00:00Z")  # C2: feature, dated the day before
    origin.checkout("main")
    origin.commit_at("2026-04-10T10:00:00Z")  # B: main, parent A
    origin.merge("feature", "2026-04-10T11:00:00Z")  # M: parents B, C2

    clone_dir = str(tmp_path / "clone")
    subprocess.run(
        ["git", "clone", "--depth", "3", f"file://{origin_dir}", clone_dir],
        capture_output=True,
        check=True,
    )
    clone = GitRepo(dir=clone_dir)
    out, code = run_cmd(clone)
    assert code == 0
    # C2 is strictly older than M's date and pruned before traversal; its
    # own shallow-cut ancestor (C1) never needs to be proved complete.
    assert out == "20260410.3"


# --- Off-branch anchor uses the cohort count of the anchor, not the target ---


def test_off_branch_anchor_uses_cohort_count(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")  # A: root
    git_repo.create_branch("feature2")
    git_repo.commit_at("2026-04-10T10:00:00Z")  # C: feature2, parent A
    git_repo.checkout("main")
    git_repo.commit_at("2026-04-10T11:00:00Z")  # B: main, parent A
    git_repo.merge("feature2", "2026-04-10T12:00:00Z")  # M: parents B, C
    git_repo.create_branch("feature3")
    git_repo.commit_at("2026-04-10T13:00:00Z")  # D: feature3, parent M

    out, code = run_cmd(git_repo, "--dirty", "-dirty", "--no-dirty-hash")
    assert code == 0
    # D is off main's first-parent chain; its anchor is M itself (D's
    # parent, since D has no first-parent-chain commits of main beyond M).
    # M's cohort is {M, B, C, A} = 4, not a first-parent-only count of 3.
    assert out == "20260410.4-dirty"


# --- Forward vs. reverse asymmetry across a pruned-away boundary ---


def test_deep_skew_forward_succeeds_reverse_detects_decreasing(
    git_repo: GitRepo,
) -> None:
    # First-parent chain dates, oldest to newest: D2, D1, D2, D2. Forward at
    # the tip prunes the D1 commit's own (D2-dated) parent away without
    # examining it, so it succeeds -- the deep skew is buried behind a
    # strictly-older commit relative to the tip's cohort date. Reverse
    # lookup for D1 fails in the block-delimiting first-parent walk (the
    # unchanged 0.2 machinery), which streams past D1 into the D2-dated
    # root and flags the date sequence as decreasing before any cohort is
    # computed. The cohort walk's own rejection paths are covered by
    # test_reverse_rejects_future_date_in_member_cohort and
    # test_reverse_shallow_second_parent_exits_4.
    git_repo.commit_at("2026-04-11T09:00:00Z")  # root: D2
    git_repo.commit_at("2026-04-10T09:00:00Z")  # D1
    git_repo.commit_at("2026-04-11T09:00:00Z")  # D2
    git_repo.commit_at("2026-04-11T10:00:00Z")  # tip: D2

    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260411.2"

    _, code = run_cmd(git_repo, "20260410.1")
    assert code == 1


# --- Branch detection ---


def test_detect_branch_local_main(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    _out, code = run_cmd(git_repo)
    assert code == 0


def test_detect_branch_override(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo, "--branch", "main")
    assert code == 0
    assert out == "20260410.1"


def test_detect_branch_override_not_found(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo, branch="nonexistent")
    assert code == 1
    assert "nonexistent" in out


def test_detect_branch_override_qualified_ref(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo, branch="refs/heads/main")
    assert code == 0
    assert out == "20260410.1"


def test_detect_branch_master_fallback(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "master", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    repo = GitRepo(dir=str(tmp_path))
    repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(repo, "--branch", "master")
    assert code == 0
    assert out == "20260410.1"


def test_detect_branch_none(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "trunk", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    repo = GitRepo(dir=str(tmp_path))
    repo.commit_at("2026-04-10T09:00:00Z")
    # No --branch override and no main/master → error
    _out, code = run_cmd(repo)
    assert code == 1


def test_detect_branch_remote(tmp_path: Path) -> None:
    # Create a "remote" repo
    remote_dir = str(tmp_path / "remote")
    subprocess.run(
        ["git", "init", "-b", "main", remote_dir],
        capture_output=True,
        check=True,
    )
    remote = GitRepo(dir=remote_dir)
    remote.commit_at("2026-04-10T09:00:00Z")

    # Clone it
    local_dir = str(tmp_path / "local")
    subprocess.run(
        ["git", "clone", remote_dir, local_dir],
        capture_output=True,
        check=True,
    )
    local = GitRepo(dir=local_dir)

    # Detect branch via origin/HEAD
    out, code = run_cmd(local, branch="")
    assert code == 0
    assert out == "20260410.1"


def test_detect_branch_remote_main(tmp_path: Path) -> None:
    # Create a "remote" repo, clone it, then remove origin/HEAD
    remote_dir = str(tmp_path / "remote")
    subprocess.run(
        ["git", "init", "-b", "main", remote_dir],
        capture_output=True,
        check=True,
    )
    remote = GitRepo(dir=remote_dir)
    remote.commit_at("2026-04-10T09:00:00Z")

    local_dir = str(tmp_path / "local")
    subprocess.run(
        ["git", "clone", remote_dir, local_dir],
        capture_output=True,
        check=True,
    )
    local = GitRepo(dir=local_dir)
    # Remove origin/HEAD so we fall through to origin/main
    subprocess.run(
        ["git", "remote", "set-head", "origin", "--delete"],
        cwd=local_dir,
        capture_output=True,
        check=True,
    )

    out, code = run_cmd(local, branch="")
    assert code == 0
    assert out == "20260410.1"


# --- Hatch plugin ---


def test_hatch_source(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")

    source = GitCalverSource(git_repo.dir, {"branch": "main"})
    data = source.get_version_data()
    assert data["version"] == "20260410.1"


def test_hatch_source_error(tmp_path: Path) -> None:
    source = GitCalverSource(str(tmp_path), {})
    with pytest.raises(RuntimeError, match="not a git repository"):
        source.get_version_data()


def test_hatch_source_empty_branch(git_repo: GitRepo) -> None:
    # An empty `branch` in pyproject.toml should fall through to
    # auto-detection, not be treated as a literal branch name.
    git_repo.commit_at("2026-04-10T09:00:00Z")
    source = GitCalverSource(git_repo.dir, {"branch": ""})
    data = source.get_version_data()
    assert data["version"] == "20260410.1"


@pytest.mark.parametrize(
    "config",
    [{}, {"branch": 0}, {"branch": False}, {"branch": []}],
    ids=["absent", "zero", "false", "empty_list"],
)
def test_hatch_source_absent_or_falsy_branch_autodetects(
    git_repo: GitRepo, config: dict[str, object]
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    source = GitCalverSource(git_repo.dir, config)
    assert source.get_version_data()["version"] == "20260410.1"


@pytest.mark.parametrize("branch", [5, ["main"], True], ids=["int", "list", "bool"])
def test_hatch_source_non_string_branch_is_reported(
    git_repo: GitRepo, branch: object
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    source = GitCalverSource(git_repo.dir, {"branch": branch})
    with pytest.raises(RuntimeError) as raised:
        source.get_version_data()
    assert str(raised.value) == f"gitcalver: branch not found: {branch}"


def test_hatch_hooks() -> None:
    cls = hatch_register_version_source()
    assert cls is GitCalverSource


# --- Reverse with non-matching version string ---


def test_reverse_not_a_version(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    _, code = run_cmd(git_repo, "not-a-version-string")
    # Should be treated as forward (revision lookup), not reverse
    assert code == 1


def test_reverse_bad_version_directly(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    with pytest.raises(ExitError, match="not a gitcalver version"):
        reverse(
            dir=git_repo.dir,
            version_str="notaversion",
            branch_override="main",
            short=False,
        )


# --- Branch detection: local fallback without override ---


def test_detect_branch_local_main_no_override(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    # Call without --branch override; should find local main
    name, _hash = detect_branch(dir=git_repo.dir)
    assert name == "main"


def test_detect_branch_local_master_no_override(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "master", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    repo = GitRepo(dir=str(tmp_path))
    repo.commit_at("2026-04-10T09:00:00Z")

    name, _hash = detect_branch(dir=str(tmp_path))
    assert name == "master"


def test_detect_branch_no_main_or_master_no_override(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "trunk", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    repo = GitRepo(dir=str(tmp_path))
    repo.commit_at("2026-04-10T09:00:00Z")

    with pytest.raises(ExitError, match="cannot determine default branch"):
        detect_branch(dir=str(tmp_path))


# --- Detached HEAD ---


def test_detached_head_on_branch(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.git("checkout", "--detach")
    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260410.1"


def test_detached_head_not_on_branch(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.git("checkout", "--detach")
    _, code = run_cmd(git_repo)
    assert code == 2


def test_detached_head_not_on_branch_dirty(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.git("checkout", "--detach")
    out, code = run_cmd(git_repo, "--dirty", "-dirty", "--no-dirty-hash")
    assert code == 0
    assert out == "20260410.1-dirty"


# --- CLI main with default argv ---


def test_cli_main_default_argv(
    git_repo: GitRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    monkeypatch.chdir(git_repo.dir)
    monkeypatch.setattr("sys.argv", ["gitcalver", "--branch", "main"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "20260410.1"


# --- Public API (get_version / find_commit) ---


def test_get_version(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    v = gitcalver.get_version(repo=git_repo.dir, branch="main")
    assert v == "20260410.1"


def test_get_version_rejects_dirty_newline(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    with pytest.raises(ExitError, match="dirty suffix must not contain a newline"):
        gitcalver.get_version(
            repo=git_repo.dir,
            branch="main",
            dirty="-dirty\nextra",
        )


def test_find_commit(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    head = git_repo.head_hash()
    h = gitcalver.find_commit("20260410.1", repo=git_repo.dir, branch="main")
    assert h == head


def test_find_commit_with_prefix(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    head = git_repo.head_hash()
    v = gitcalver.get_version(repo=git_repo.dir, branch="main", prefix="v0.")
    assert v == "v0.20260410.1"
    h = gitcalver.find_commit(v, prefix="v0.", repo=git_repo.dir, branch="main")
    assert h == head


def test_public_api_remote(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    head = git_repo.head_hash()
    git_repo.git("update-ref", "refs/remotes/upstream/trunk", head)
    git_repo.git(
        "symbolic-ref",
        "refs/remotes/upstream/HEAD",
        "refs/remotes/upstream/trunk",
    )
    version = gitcalver.get_version(repo=git_repo.dir, remote="upstream")
    assert version == "20260410.1"
    assert (
        gitcalver.find_commit(
            version, repo=git_repo.dir, branch="trunk", remote="upstream"
        )
        == head
    )


def test_find_commit_requires_prefix(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    with pytest.raises(ExitError, match="missing required prefix"):
        gitcalver.find_commit(
            "20260410.1", prefix="v0.", repo=git_repo.dir, branch="main"
        )


# --- python -m gitcalver ---


def test_module_invocation(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    result = subprocess.run(
        [sys.executable, "-m", "gitcalver", "--branch", "main"],
        capture_output=True,
        text=True,
        cwd=git_repo.dir,
        check=True,
    )
    assert result.stdout.strip() == "20260410.1"


# --- CLI main() ---


def test_cli_main_success(
    git_repo: GitRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    monkeypatch.chdir(git_repo.dir)
    with pytest.raises(SystemExit) as exc_info:
        main(["--branch", "main"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "20260410.1"


@pytest.mark.parametrize("option", ["prefix", "dirty"])
def test_cli_writes_non_utf8_argument_bytes_as_given(
    git_repo: GitRepo, option: str
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    if option == "prefix":
        args = [b"--prefix=\xff"]
        want = b"\xff20260410.1\n"
    else:
        git_repo.write_file("dirty.txt")
        args = [b"--dirty=\xff", "--no-dirty-hash"]
        want = b"20260410.1\xff\n"
    result = subprocess.run(
        [sys.executable, "-m", "gitcalver", "--branch", "main", *args],
        capture_output=True,
        cwd=git_repo.dir,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8:strict"},
        check=False,
    )
    assert result.stderr == b""
    assert result.stdout == want
    assert result.returncode == 0


def test_cli_main_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(["--branch", "main"])
    assert exc_info.value.code == 1


def test_cli_main_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    assert "Usage:" in capsys.readouterr().out


def test_cli_main_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("gitcalver ")
    assert out != "gitcalver "


def test_cli_main_invalid_option() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--invalid"])
    assert exc_info.value.code == 1


def test_cli_main_dirty(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.write_file("dirty.txt")
    monkeypatch.chdir(git_repo.dir)
    with pytest.raises(SystemExit) as exc_info:
        main(["--branch", "main"])
    assert exc_info.value.code == 2


def test_cli_main_reverse(
    git_repo: GitRepo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    monkeypatch.chdir(git_repo.dir)
    with pytest.raises(SystemExit) as exc_info:
        main(["--branch", "main", "20260410.1"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out == git_repo.head_hash()


# --- CLI parsing ---


def test_cli_help() -> None:
    assert _parse_args(["--help"]).help is True


def test_cli_prefix_missing() -> None:
    with pytest.raises(ExitError, match="--prefix"):
        _parse_args(["--prefix"])


def test_cli_dirty_missing() -> None:
    with pytest.raises(ExitError, match="--dirty"):
        _parse_args(["--dirty"])


def test_cli_dirty_empty_string() -> None:
    with pytest.raises(ExitError, match="--dirty requires a non-empty string"):
        _parse_args(["--dirty", ""])


def test_cli_dirty_newline() -> None:
    with pytest.raises(ExitError, match="--dirty must not contain a newline"):
        _parse_args(["--dirty", "-dirty\nextra"])


def test_cli_no_dirty_hash_without_dirty() -> None:
    with pytest.raises(ExitError, match="--no-dirty-hash requires --dirty"):
        _parse_args(["--no-dirty-hash"])


def test_cli_no_dirty_overrides_dirty(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.write_file("dirty.txt")
    _, code = run_cmd(git_repo, "--dirty", "-dirty", "--no-dirty")
    assert code == 2


def test_cli_branch_missing() -> None:
    with pytest.raises(ExitError, match="--branch"):
        _parse_args(["--branch"])


def test_cli_remote_missing() -> None:
    with pytest.raises(ExitError, match="--remote"):
        _parse_args(["--remote"])


def test_cli_remote_empty() -> None:
    with pytest.raises(ExitError, match="--remote requires a non-empty string"):
        _parse_args(["--remote", ""])


def test_cli_unknown_option() -> None:
    with pytest.raises(ExitError, match="unrecognized arguments"):
        _parse_args(["--bogus"])


def test_cli_single_dash() -> None:
    with pytest.raises(ExitError, match="unrecognized arguments"):
        _parse_args(["-x"])


def test_cli_all_flags() -> None:
    opts = _parse_args(
        [
            "--prefix",
            "v0.",
            "--dirty",
            "-dirty",
            "--no-dirty-hash",
            "--branch",
            "develop",
            "--remote",
            "upstream",
            "--short",
            "abc123",
        ]
    )
    assert opts.prefix == "v0."
    assert opts.dirty == "-dirty"
    assert opts.no_dirty_hash is True
    assert opts.branch == "develop"
    assert opts.remote == "upstream"
    assert opts.short is True
    assert opts.positional == "abc123"


# --- Argument terminator (--) ---


def test_cli_double_dash(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo, "--")
    assert code == 0
    assert out == "20260410.1"


def test_cli_double_dash_with_version(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    head = git_repo.head_hash()
    out, code = run_cmd(git_repo, "--", "20260410.1")
    assert code == 0
    assert out == head


def test_cli_double_dash_extra_arg() -> None:
    with pytest.raises(ExitError, match="unrecognized arguments"):
        _parse_args(["--branch", "main", "--", "a", "b"])


# --- Multiple positional args rejected ---


def test_cli_multiple_positional_args() -> None:
    with pytest.raises(ExitError, match="unrecognized arguments"):
        _parse_args(["arg1", "arg2"])


# --- --option=value syntax ---


def test_cli_prefix_equals(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo, "--prefix=v0.")
    assert code == 0
    assert out == "v0.20260410.1"


def test_cli_dirty_equals(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.write_file("dirty.txt")
    out, code = run_cmd(git_repo, "--dirty=-dirty", "--no-dirty-hash")
    assert code == 0
    assert out == "20260410.1-dirty"


def test_cli_branch_equals(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo, "--branch=main", branch="")
    assert code == 0
    assert out == "20260410.1"


def test_cli_dirty_equals_empty() -> None:
    with pytest.raises(ExitError, match="--dirty requires a non-empty string"):
        _parse_args(["--dirty="])


# --- Leading zeros in N rejected ---


def test_reverse_leading_zero_rejected(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    _, code = run_cmd(git_repo, "20260410.01")
    assert code == 1


# --- Trailing garbage rejected ---


def test_reverse_trailing_garbage(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.commit_at("2026-04-10T11:00:00Z")
    _, code = run_cmd(git_repo, "20260410.3rc1")
    assert code == 1


# --- --short in forward mode rejected ---


def test_cli_short_in_forward_mode(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    out, code = run_cmd(git_repo, "--short")
    assert code == 1
    assert "reverse lookup" in out


# --- Incomplete-history proofs ---


def test_shallow_clone_inside_date_block_is_incomplete(tmp_path: Path) -> None:
    origin_dir = str(tmp_path / "origin")
    subprocess.run(
        ["git", "init", "-b", "main", origin_dir],
        capture_output=True,
        check=True,
    )
    origin = GitRepo(dir=origin_dir)
    origin.commit_at("2026-04-10T09:00:00Z")
    origin.commit_at("2026-04-10T10:00:00Z")

    clone_dir = str(tmp_path / "clone")
    subprocess.run(
        ["git", "clone", "--depth", "1", f"file://{origin_dir}", clone_dir],
        capture_output=True,
        check=True,
    )
    clone = GitRepo(dir=clone_dir)
    out, code = run_cmd(clone)
    assert code == 4
    assert "local history ended" in out

    with pytest.raises(IncompleteHistoryError) as exc_info:
        gitcalver.get_version(repo=clone.dir, branch="main")
    assert exc_info.value.code == gitcalver.EXIT_INCOMPLETE_HISTORY


def test_shallow_clone_with_older_date_boundary_succeeds(tmp_path: Path) -> None:
    origin_dir = str(tmp_path / "origin")
    subprocess.run(
        ["git", "init", "-b", "main", origin_dir],
        capture_output=True,
        check=True,
    )
    origin = GitRepo(dir=origin_dir)
    origin.commit_at("2026-04-09T09:00:00Z")
    origin.commit_at("2026-04-10T10:00:00Z")

    clone_dir = str(tmp_path / "clone")
    subprocess.run(
        ["git", "clone", "--depth", "2", f"file://{origin_dir}", clone_dir],
        capture_output=True,
        check=True,
    )
    clone = GitRepo(dir=clone_dir)
    out, code = run_cmd(clone)
    assert code == 0
    assert out == "20260410.1"


# --- Shallow cuts that a stored parent proves are not roots ---


def _origin(tmp_path: Path) -> GitRepo:
    origin_dir = str(tmp_path / "origin")
    subprocess.run(
        ["git", "init", "-b", "main", origin_dir],
        capture_output=True,
        check=True,
    )
    return GitRepo(dir=origin_dir)


def _clone(tmp_path: Path, origin: GitRepo, *options: str) -> GitRepo:
    clone_dir = str(tmp_path / "clone")
    subprocess.run(
        ["git", "clone", *options, f"file://{origin.dir}", clone_dir],
        capture_output=True,
        check=True,
    )
    return GitRepo(dir=clone_dir)


def test_reverse_older_date_in_shallow_clone_exits_4(tmp_path: Path) -> None:
    origin = _origin(tmp_path)
    origin.commit_at("2026-04-09T09:00:00Z")
    origin.commit_at("2026-04-10T09:00:00Z")
    clone = _clone(tmp_path, origin, "--depth", "1")

    # The clone's only commit stores the 2026-04-09 commit as its parent, so
    # the log ending there does not show that 20260409.1 never existed.
    assert run_cmd(clone, "20260409.1") == (
        "gitcalver: local history ended before version could be proved",
        4,
    )


def test_unrelated_target_cut_by_shallow_boundary_exits_4(tmp_path: Path) -> None:
    origin = _origin(tmp_path)
    origin.commit_at("2026-04-10T08:00:00Z")  # main's only commit
    origin.git("checkout", "--orphan", "other")
    origin.commit_at("2026-04-10T09:00:00Z")  # other's root
    origin.commit_at("2026-04-10T10:00:00Z")  # the boundary of a depth-2 clone
    target = origin.commit_at("2026-04-10T11:00:00Z")
    origin.checkout("main")
    clone = _clone(tmp_path, origin, "--depth", "2", "--no-single-branch")

    # The boundary stores the root as its parent, and the root may connect to
    # main.
    assert run_cmd(clone, target) == (
        "gitcalver: local history ended before reachability could be proved",
        4,
    )


def test_unrelated_target_with_shallow_roots_exits_3(tmp_path: Path) -> None:
    origin = _origin(tmp_path)
    origin.commit_at("2026-04-10T09:00:00Z")  # main's only commit
    origin.git("checkout", "--orphan", "other")
    target = origin.commit_at("2026-04-10T10:00:00Z")  # other's only commit
    origin.checkout("main")
    clone = _clone(tmp_path, origin, "--depth", "1", "--no-single-branch")

    # Depth 1 lists both roots as shallow boundaries, but a boundary with no
    # stored parent hides nothing.
    assert len(Path(clone.dir, ".git", "shallow").read_text().split()) == 2
    assert run_cmd(clone, target) == (
        f"gitcalver: cannot trace {target} to the default branch (main)",
        3,
    )


def test_shallow_branch_cannot_prove_unrelated_target_exits_4(tmp_path: Path) -> None:
    origin = _origin(tmp_path)
    origin.commit_at("2026-04-10T09:00:00Z")
    origin.commit_at("2026-04-10T10:00:00Z")
    origin.commit_at("2026-04-10T11:00:00Z")  # main's tip
    origin.git("checkout", "--orphan", "other")
    target = origin.commit_at("2026-04-10T12:00:00Z")  # other's only commit
    origin.checkout("main")
    clone = _clone(tmp_path, origin, "--depth", "1", "--no-single-branch")

    # The tip is main's oldest local commit but stores its parent, so main may
    # reach the target through it.
    assert run_cmd(clone, target) == (
        "gitcalver: local history cannot prove the target's branch relationship",
        4,
    )


def test_unrelated_target_with_missing_shallow_boundary_exits_4(
    git_repo: GitRepo,
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.git("checkout", "--orphan", "other")
    target = git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.checkout("main")
    Path(git_repo.dir, ".git", "shallow").write_text("1" * len(target) + "\n")

    assert run_cmd(git_repo, target) == (
        "gitcalver: local history ended before reachability could be proved",
        4,
    )


def test_stored_first_parent_returns_the_first_parent(git_repo: GitRepo) -> None:
    root = git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.checkout("main")
    main = git_repo.commit_at("2026-04-10T11:00:00Z")
    git_repo.merge("feature", "2026-04-10T12:00:00Z")
    merge = git_repo.head_hash()

    assert stored_first_parent(root, dir=git_repo.dir) is None
    assert stored_first_parent(main, dir=git_repo.dir) == root
    assert stored_first_parent(merge, dir=git_repo.dir) == main


def test_reverse_root_message_resembling_parent_header(git_repo: GitRepo) -> None:
    root = git_repo.commit_at(
        "2026-04-09T09:00:00Z", message=f"root\n\nparent {'0' * 40}"
    )
    git_repo.commit_at("2026-04-10T09:00:00Z")

    assert run_cmd(git_repo, "20260409.1") == (root, 0)


# --- Shallow files and commit objects that cannot be read ---


def _linear_history(repo: GitRepo) -> tuple[str, str]:
    root = repo.commit_at("2026-04-10T09:00:00Z")
    middle = repo.commit_at("2026-04-10T10:00:00Z")
    repo.commit_at("2026-04-10T11:00:00Z")
    return root, middle


# Git reads only the object ID at the start of a line, so it accepts these.
_BAD_SHALLOW_LINES = [
    pytest.param(b"{oid}\xff", id="non_utf8"),
    pytest.param(b"{oid} junk", id="trailing_text"),
    pytest.param(b"{oid}\0junk", id="nul"),
    pytest.param(b"{oid}\x0cjunk", id="form_feed"),
]


def _shallow_message(shallow: Path) -> str:
    return (
        f"gitcalver: cannot read shallow boundary: {shallow}: "
        "line 1 is not an object ID: "
    )


@pytest.mark.parametrize("line", _BAD_SHALLOW_LINES)
@pytest.mark.parametrize("target", [[], ["20260410.2"]], ids=["forward", "reverse"])
def test_malformed_shallow_line_exits_4(
    git_repo: GitRepo, line: bytes, target: list[str]
) -> None:
    root, _ = _linear_history(git_repo)
    shallow = Path(git_repo.dir, ".git", "shallow")
    shallow.write_bytes(line.replace(b"{oid}", root.encode()) + b"\n")

    out, code = run_cmd(git_repo, *target)
    assert code == 4
    assert out.startswith(_shallow_message(shallow))


@pytest.mark.parametrize("line", _BAD_SHALLOW_LINES)
def test_malformed_shallow_line_is_reported_when_proving_unrelated_history(
    git_repo: GitRepo, line: bytes
) -> None:
    root = git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.git("checkout", "--orphan", "orphan")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    shallow = Path(git_repo.dir, ".git", "shallow")
    shallow.write_bytes(line.replace(b"{oid}", root.encode()) + b"\n")

    out, code = run_cmd(git_repo, "--dirty", "-dirty")
    assert code == 4
    assert out.startswith(_shallow_message(shallow))


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"{root}\n", id="lf"),
        pytest.param(b"{root}", id="no_final_newline"),
        pytest.param(b"{root}\r\n", id="crlf"),
        pytest.param(b"", id="empty"),
    ],
)
def test_shallow_file_forms_git_writes_or_reads_are_accepted(
    git_repo: GitRepo, content: bytes
) -> None:
    root, _ = _linear_history(git_repo)
    Path(git_repo.dir, ".git", "shallow").write_bytes(
        content.replace(b"{root}", root.encode())
    )

    assert run_cmd(git_repo) == ("20260410.3", 0)


@pytest.mark.parametrize("case", ["lower", "upper"])
def test_shallow_boundary_is_matched_whatever_the_case(
    git_repo: GitRepo, case: str
) -> None:
    _, middle = _linear_history(git_repo)
    name = middle.upper() if case == "upper" else middle
    Path(git_repo.dir, ".git", "shallow").write_text(f"{name}\n")

    out, code = run_cmd(git_repo)
    assert code == 4
    assert out == "gitcalver: local history ended inside the 20260410 date block"


def test_unreadable_shallow_file_exits_4(
    git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _linear_history(git_repo)
    Path(git_repo.dir, ".git", "shallow").write_text(f"{root}\n")
    read_bytes = Path.read_bytes

    # Mode bits cannot make the file unreadable to root or on a filesystem that
    # ignores them, so the read is refused here.
    def refuse(path: Path) -> bytes:
        if path.name == "shallow":
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", refuse)

    out, code = run_cmd(git_repo)
    assert code == 4
    assert out.startswith(
        "gitcalver: cannot read shallow boundary: [Errno 13] Permission denied: "
    )


_AUTHOR = b"author Test <test@test.com> 1775813400 +0000"
_COMMITTER = b"committer Test <test@test.com> 1775813400 +0000"


def _write_commit(repo: GitRepo, *headers: bytes) -> str:
    # --literally stores objects that git would refuse to write.
    result = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--literally", "--stdin"],
        capture_output=True,
        cwd=repo.dir,
        input=b"\n".join([*headers, b"", b"message"]),
        check=True,
    )
    return result.stdout.decode().strip()


def _merge_with_second_parent(repo: GitRepo, *headers: bytes) -> str:
    """Make HEAD a merge of two parents; return the second, built from `headers`.

    Git never parses a second parent while it walks first parents, so only
    gitcalver's own reader meets it.
    """
    base = repo.commit_at("2026-04-10T09:00:00Z")
    tree = b"tree " + repo.git("rev-parse", "HEAD^{tree}").encode()
    side = _write_commit(repo, tree, *headers)
    merge = _write_commit(
        repo,
        tree,
        f"parent {base}".encode(),
        f"parent {side}".encode(),
        _AUTHOR,
        _COMMITTER,
    )
    repo.git("update-ref", "refs/heads/main", merge)
    return side


@pytest.mark.parametrize(
    ("headers", "problem"),
    [
        pytest.param(
            [_AUTHOR],
            "has no committer line",
            id="no_committer",
        ),
        pytest.param(
            [_AUTHOR, b"committer x"],
            "has no committer date between the years 1 and 9999",
            id="committer_without_timestamp",
        ),
        pytest.param(
            [_AUTHOR, b"committer Test <test@test.com> abc +0000"],
            "has no committer date between the years 1 and 9999",
            id="timestamp_not_a_number",
        ),
        pytest.param(
            [_AUTHOR, b"committer Test <test@test.com> 1775813400"],
            "has no committer date between the years 1 and 9999",
            id="timezone_missing",
        ),
        pytest.param(
            [_AUTHOR, b"committer Test <test@test.com> 1775813400 +0000 extra"],
            "has no committer date between the years 1 and 9999",
            id="text_after_timezone",
        ),
        pytest.param(
            [_AUTHOR, b"committer Test <test@test.com> " + b"9" * 20 + b" +0000"],
            "has no committer date between the years 1 and 9999",
            id="timestamp_too_long",
        ),
        pytest.param(
            [_AUTHOR, b"committer Test <test@test.com> " + b"9" * 5000 + b" +0000"],
            "has no committer date between the years 1 and 9999",
            id="timestamp_beyond_int_conversion_limit",
        ),
        pytest.param(
            [_AUTHOR, b"committer Test <test@test.com> 253402300800 +0000"],
            "has no committer date between the years 1 and 9999",
            id="year_10000",
        ),
        pytest.param(
            [_AUTHOR, b"committer Test <test@test.com> -62135596801 +0000"],
            "has no committer date between the years 1 and 9999",
            id="year_0",
        ),
        pytest.param(
            [b"parent \xff\xfe", _AUTHOR, _COMMITTER],
            "has a parent line that is not an object ID",
            id="parent_non_utf8",
        ),
        pytest.param(
            [b"parent zzzz", _AUTHOR, _COMMITTER],
            "has a parent line that is not an object ID",
            id="parent_not_hex",
        ),
    ],
)
@pytest.mark.parametrize("target", [[], ["20260410.2"]], ids=["forward", "reverse"])
def test_unreadable_commit_object_exits_4(
    git_repo: GitRepo, headers: list[bytes], problem: str, target: list[str]
) -> None:
    side = _merge_with_second_parent(git_repo, *headers)

    assert run_cmd(git_repo, *target) == (f"gitcalver: commit {side} {problem}", 4)


@pytest.mark.parametrize(
    ("committer", "want"),
    [
        pytest.param(
            b"253402300799",
            (
                "gitcalver: committer date not monotonic: older commit dated "
                "99991231 has a later date than newer commit dated 20260410",
                1,
            ),
            id="last_second_of_9999",
        ),
        pytest.param(b"-62135596800", ("20260410.2", 0), id="first_second_of_year_1"),
        pytest.param(b"-30627460800", ("20260410.2", 0), id="three_digit_year"),
        pytest.param(b"-1", ("20260410.2", 0), id="before_1970"),
    ],
)
def test_committer_dates_at_the_edges_are_read(
    git_repo: GitRepo, committer: bytes, want: tuple[str, int]
) -> None:
    _merge_with_second_parent(
        git_repo,
        _AUTHOR,
        b"committer Test <test@test.com> " + committer + b" +0000",
    )

    assert run_cmd(git_repo) == want


def _commit_on_top(repo: GitRepo, committer: bytes) -> str:
    base = repo.commit_at("2026-04-10T09:00:00Z")
    tree = b"tree " + repo.git("rev-parse", "HEAD^{tree}").encode()
    tip = _write_commit(
        repo,
        tree,
        f"parent {base}".encode(),
        _AUTHOR,
        b"committer Test <test@test.com> " + committer + b" +0000",
    )
    repo.git("update-ref", "refs/heads/main", tip)
    return tip


@pytest.mark.parametrize(
    "committer",
    [
        pytest.param(b"253402300800", id="year_10000"),
        pytest.param(b"abc", id="not_a_number"),
        pytest.param(b"-1", id="before_1970"),
    ],
)
def test_reverse_refuses_a_first_parent_commit_without_a_date_it_can_use(
    git_repo: GitRepo, committer: bytes
) -> None:
    tip = _commit_on_top(git_repo, committer)

    assert run_cmd(git_repo, "20260410.1") == (
        f"gitcalver: git log printed no YYYYMMDD committer date for {tip}",
        4,
    )


def test_reverse_refuses_a_root_without_a_date_it_can_use(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    tree = b"tree " + git_repo.git("rev-parse", "HEAD^{tree}").encode()
    root = _write_commit(
        git_repo, tree, _AUTHOR, b"committer Test <test@test.com> -1 +0000"
    )
    tip = _write_commit(git_repo, tree, f"parent {root}".encode(), _AUTHOR, _COMMITTER)
    git_repo.git("update-ref", "refs/heads/main", tip)
    assert run_cmd(git_repo) == ("20260410.1", 0)

    assert run_cmd(git_repo, "20260410.1") == (
        f"gitcalver: git log printed no YYYYMMDD committer date for {root}",
        4,
    )


def test_parent_ids_are_compared_in_lowercase(git_repo: GitRepo) -> None:
    base = git_repo.commit_at("2026-04-10T09:00:00Z")
    tree = b"tree " + git_repo.git("rev-parse", "HEAD^{tree}").encode()
    merge = _write_commit(
        git_repo,
        tree,
        f"parent {base}".encode(),
        f"parent {base.upper()}".encode(),
        _AUTHOR,
        _COMMITTER,
    )
    git_repo.git("update-ref", "refs/heads/main", merge)
    assert git_repo.git("rev-list", "--count", "HEAD") == "2"

    assert run_cmd(git_repo) == ("20260410.2", 0)


# --- Partial clone accepted ---


@pytest.fixture
def partial_source(tmp_path: Path) -> GitRepo:
    origin_dir = str(tmp_path / "origin")
    subprocess.run(
        ["git", "init", "-b", "main", origin_dir],
        capture_output=True,
        check=True,
    )
    origin = GitRepo(dir=origin_dir)
    origin.git("config", "uploadpack.allowFilter", "true")
    origin.write_file("partial.txt", "one")
    origin.git("add", "partial.txt")
    origin.commit_at("2026-04-10T09:00:00Z")
    origin.write_file("partial.txt", "two")
    origin.git("add", "partial.txt")
    origin.commit_at("2026-04-11T09:00:00Z")
    return origin


def _missing_objects(repo: GitRepo) -> set[str]:
    listing = repo.git("rev-list", "--objects", "--missing=print", "HEAD")
    return {
        line.removeprefix("?") for line in listing.splitlines() if line.startswith("?")
    }


def test_partial_clone_accepted(tmp_path: Path, partial_source: GitRepo) -> None:
    clone_dir = str(tmp_path / "clone")
    subprocess.run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            f"file://{partial_source.dir}",
            clone_dir,
        ],
        capture_output=True,
        check=True,
    )
    clone = GitRepo(dir=clone_dir)
    assert clone.git("config", "remote.origin.promisor") == "true"
    assert clone.git("config", "remote.origin.partialclonefilter") == "blob:none"
    absent_blobs = {partial_source.git("rev-parse", "HEAD~1:partial.txt")}
    assert _missing_objects(clone) == absent_blobs

    out, code = run_cmd(clone, branch="")
    assert code == 0
    assert out == "20260411.1"
    out, code = run_cmd(clone, "20260411.1", branch="")
    assert code == 0
    assert out == partial_source.head_hash()
    assert _missing_objects(clone) == absent_blobs


def test_missing_promised_commit_is_not_lazy_fetched(
    tmp_path: Path, partial_source: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    tip = partial_source.head_hash()
    parent = partial_source.parent_hash()

    repo_dir = tmp_path / "promisor"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo_dir)],
        capture_output=True,
        check=True,
    )
    repo = GitRepo(dir=str(repo_dir))
    repo.git("config", "core.repositoryformatversion", "1")
    repo.git("config", "extensions.partialClone", "blocked")
    repo.git("config", "remote.blocked.promisor", "true")
    repo.git("config", "remote.blocked.partialCloneFilter", "blob:none")
    repo.git("config", "remote.blocked.url", "blocked::missing")
    fan_out = repo_dir / ".git" / "objects" / tip[:2]
    fan_out.mkdir()
    shutil.copyfile(
        Path(partial_source.dir, ".git", "objects", tip[:2], tip[2:]),
        fan_out / tip[2:],
    )
    repo.git("update-ref", "refs/heads/main", tip)

    marker = tmp_path / "lazy-fetch-attempted"
    helper_dir = tmp_path / "blocked-bin"
    helper_dir.mkdir()
    helper = helper_dir / "git-remote-blocked"
    helper.write_text(f"#!/bin/sh\n: >{shlex.quote(str(marker))}\nexit 1\n")
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", str(helper_dir), prepend=os.pathsep)

    subprocess.run(
        ["git", "cat-file", "-e", f"{parent}^{{commit}}"],
        capture_output=True,
        cwd=repo_dir,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "0"},
        check=False,
    )
    assert marker.exists()
    marker.unlink()

    out, code = run_cmd(repo, "HEAD")
    assert code == 4
    assert "local history ended" in out
    out, code = run_cmd(repo, "20260411.1")
    assert code == 4
    assert "local history ended" in out
    out, code = run_cmd(repo, parent)
    assert code == 4
    assert "revision is missing from local history" in out
    assert not marker.exists()


# --- Replacement refs ignored ---


def test_replace_refs_ignored(git_repo: GitRepo) -> None:
    parent = git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.commit_at("2026-04-10T12:00:00Z")
    git_repo.git("replace", "--graft", "HEAD")
    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260410.2"
    out, code = run_cmd(git_repo, parent)
    assert code == 0
    assert out == "20260410.1"
    out, code = run_cmd(git_repo, "20260410.1")
    assert code == 0
    assert out == parent

    git_repo.git("checkout", "--orphan", "side")
    missing = git_repo.commit_at("2026-04-10T13:00:00Z")
    git_repo.commit_at("2026-04-10T14:00:00Z")
    git_repo.git("replace", "--graft", "HEAD")
    Path(git_repo.dir, ".git", "objects", missing[:2], missing[2:]).unlink()
    out, code = run_cmd(git_repo)
    assert code == 4
    assert "reachability" in out


# --- Empty repo error message ---


def test_empty_repo_message(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    repo = GitRepo(dir=str(tmp_path))
    out, code = run_cmd(repo)
    assert code == 1
    assert "no commits" in out


def test_orphan_branch_without_commits(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.git("checkout", "--orphan", "fresh")
    out, code = run_cmd(git_repo)
    assert code == 1
    assert out == "gitcalver: no commits in repository"


def test_empty_loose_ref_is_not_reported_as_no_commits(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.write_file(".git/refs/heads/main", "")
    out, code = run_cmd(git_repo)
    assert code == 1
    assert out.startswith("gitcalver: cannot read HEAD: ")


def test_garbage_packed_refs_is_not_reported_as_no_commits(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.git("pack-refs", "--all")
    git_repo.write_file(".git/packed-refs", "garbage\n")
    said = _git_stderr(git_repo.dir, "rev-parse", "--verify", "--quiet", "HEAD")
    assert said
    out, code = run_cmd(git_repo)
    assert code == 1
    assert out == f"gitcalver: cannot read HEAD: {said}"


def _intercepting_git(
    bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[str, tuple[int, str]],
) -> None:
    real_git = shutil.which("git")
    assert real_git
    cases = "".join(
        f"{shlex.quote(args)}) printf '%s\\n' {shlex.quote(stderr)} >&2\n"
        f"exit {code};;\n"
        for args, (code, stderr) in responses.items()
    )
    fake_git = bin_dir / "git"
    fake_git.write_text(
        f'#!/bin/sh\ncase "$*" in\n{cases}esac\nexec {shlex.quote(real_git)} "$@"\n'
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)


@pytest.mark.parametrize(
    ("stderr", "tail"),
    [
        pytest.param(SENTINEL, SENTINEL, id="relays_git_stderr"),
        pytest.param("", "the reference HEAD names is broken", id="no_stderr"),
    ],
)
def test_unreadable_head_reports_cannot_read_head(
    git_repo: GitRepo,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    tail: str,
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    _intercepting_git(
        tmp_path_factory.mktemp("shim"),
        monkeypatch,
        {
            "rev-parse --verify --quiet HEAD": (1, stderr),
            "symbolic-ref --quiet HEAD": (128, ""),
        },
    )

    out, code = run_cmd(git_repo)
    assert code == 1
    assert out == f"gitcalver: cannot read HEAD: {tail}"


# --- A git cat-file child that stops cooperating ---


def _fake_cat_file(bin_dir: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    real_git = shutil.which("git")
    assert real_git
    fake_git = bin_dir / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        f"real={shlex.quote(real_git)}\n"
        'if [ "$*" = "cat-file --batch" ]; then\n'
        f"{script}\n"
        "fi\n"
        'exec "$real" "$@"\n'
    )
    fake_git.chmod(0o755)
    assert os.access(fake_git, os.X_OK), "tmp_path is on a noexec filesystem"
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)


def _child(reader: CommitReader) -> subprocess.Popen[bytes]:
    return reader._proc  # noqa: SLF001


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 30
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.01)


def _interrupt() -> bytes:
    raise KeyboardInterrupt


def _fail_after_closing(
    monkeypatch: pytest.MonkeyPatch, proc: subprocess.Popen[bytes], pipe: str
) -> None:
    stream = getattr(proc, pipe)
    close = stream.close

    def close_and_fail() -> None:
        close()
        msg = "cannot close"
        raise OSError(msg)

    monkeypatch.setattr(stream, "close", close_and_fail)


# A fake child that sleeps stays alive for 30 seconds unless it is killed, so a
# close() that waits for it to exit by itself takes that long.
SLEEP = "exec sleep 30"


def _closes_stdin_then(script: str, ready: Path) -> str:
    return f"exec <&-\n: >{shlex.quote(str(ready))}\n{script}"


@pytest.mark.parametrize(
    "args",
    [
        pytest.param([], id="forward"),
        pytest.param(["20260410.3"], id="reverse"),
    ],
)
def test_a_child_that_stops_reading_does_not_replace_the_error(
    git_repo: GitRepo,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    for hour in ("09", "10", "11"):
        git_repo.commit_at(f"2026-04-10T{hour}:00:00Z")
    scratch = tmp_path_factory.mktemp("scratch")
    answer = shlex.quote(str(scratch / "answer"))
    # Answer the first request with its stdin already closed, so the parent's
    # second request meets a pipe nobody reads, and stay alive.
    _fake_cat_file(
        tmp_path_factory.mktemp("shim"),
        monkeypatch,
        "read request\n"
        f'printf "%s\\n" "$request" | "$real" cat-file --batch >{answer}\n'
        "exec <&-\n"
        f"cat {answer}\n"
        f"{SLEEP}",
    )
    readers: list[CommitReader] = []
    init = CommitReader.__init__

    def record(self: CommitReader, dir: str | None = None) -> None:
        init(self, dir=dir)
        readers.append(self)

    monkeypatch.setattr(CommitReader, "__init__", record)

    out, code = run_cmd(git_repo, *args)

    assert code == 4
    assert out == "gitcalver: local history ended before the result could be proved"
    assert [_child(reader).returncode for reader in readers] == [-signal.SIGKILL]


def test_failed_request_is_reported_and_the_child_is_killed(
    git_repo: GitRepo,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = tmp_path_factory.mktemp("scratch") / "ready"
    _fake_cat_file(
        tmp_path_factory.mktemp("shim"),
        monkeypatch,
        _closes_stdin_then(SLEEP, ready),
    )
    reader = CommitReader(dir=git_repo.dir)
    _wait_for(ready)

    with pytest.raises(GitError) as raised:
        reader.read("HEAD")
    assert isinstance(raised.value.__cause__, BrokenPipeError)
    reader.close()

    proc = _child(reader)
    assert proc.returncode == -signal.SIGKILL
    assert proc.stdin is not None
    assert proc.stdin.closed
    assert proc.stdout is not None
    assert proc.stdout.closed


def test_unintelligible_answer_does_not_make_close_wait_for_the_child(
    git_repo: GitRepo,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_cat_file(
        tmp_path_factory.mktemp("shim"),
        monkeypatch,
        f"read request\necho garbage\n{SLEEP}",
    )
    reader = CommitReader(dir=git_repo.dir)

    with pytest.raises(GitError, match="unexpected cat-file response"):
        reader.read("HEAD")
    reader.close()

    assert _child(reader).returncode == -signal.SIGKILL


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("abc commit 12", id="header_without_newline"),
        pytest.param("abc commit 100\\ncommitter Bob 99 Smith", id="body_cut_short"),
        pytest.param("abc commit 5\\nhello?", id="body_without_final_newline"),
    ],
)
def test_answer_that_ends_early_is_not_parsed(
    git_repo: GitRepo,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    _fake_cat_file(
        tmp_path_factory.mktemp("shim"),
        monkeypatch,
        f"read request\nprintf '{answer}'\nexit 0",
    )

    assert run_cmd(git_repo) == (
        "gitcalver: local history ended before the result could be proved",
        4,
    )


def test_interrupted_read_does_not_make_close_wait_for_the_child(
    git_repo: GitRepo,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_cat_file(tmp_path_factory.mktemp("shim"), monkeypatch, SLEEP)
    reader = CommitReader(dir=git_repo.dir)
    proc = _child(reader)
    assert proc.stdout is not None
    monkeypatch.setattr(proc.stdout, "readline", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        reader.read("HEAD")
    reader.close()

    assert proc.returncode == -signal.SIGKILL


def test_failed_reader_refuses_further_reads(
    git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = git_repo.commit_at("2026-04-10T09:00:00Z")
    second = git_repo.commit_at("2026-04-10T10:00:00Z")
    reader = CommitReader(dir=git_repo.dir)
    proc = _child(reader)
    assert proc.stdout is not None
    readline = proc.stdout.readline
    calls = 0

    def interrupt_first_call() -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        return readline()

    monkeypatch.setattr(proc.stdout, "readline", interrupt_first_call)

    with pytest.raises(KeyboardInterrupt):
        reader.read(second)
    with pytest.raises(GitError, match="out of step"):
        reader.read(first)
    reader.close()

    assert proc.returncode == -signal.SIGKILL


def test_close_surfaces_a_broken_pipe_that_no_failed_request_explains(
    git_repo: GitRepo,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = tmp_path_factory.mktemp("scratch") / "ready"
    _fake_cat_file(
        tmp_path_factory.mktemp("shim"),
        monkeypatch,
        _closes_stdin_then("exit 0", ready),
    )
    reader = CommitReader(dir=git_repo.dir)
    proc = _child(reader)
    assert proc.stdin is not None
    _wait_for(ready)
    proc.stdin.write(b"never flushed\n")

    with pytest.raises(BrokenPipeError):
        reader.close()

    assert proc.stdin.closed
    assert proc.stdout is not None
    assert proc.stdout.closed
    assert proc.returncode == 0


def test_close_after_a_failed_exchange_still_surfaces_other_errors(
    git_repo: GitRepo,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_cat_file(
        tmp_path_factory.mktemp("shim"),
        monkeypatch,
        f"read request\necho garbage\n{SLEEP}",
    )
    reader = CommitReader(dir=git_repo.dir)
    with pytest.raises(GitError):
        reader.read("HEAD")
    proc = _child(reader)
    _fail_after_closing(monkeypatch, proc, "stdin")

    with pytest.raises(OSError, match="cannot close"):
        reader.close()

    assert proc.returncode == -signal.SIGKILL


def test_close_failure_keeps_the_error_already_in_flight_as_context(
    git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = CommitReader(dir=git_repo.dir)
    _fail_after_closing(monkeypatch, _child(reader), "stdout")

    def fail_while_open() -> None:
        with contextlib.closing(reader):
            msg = "local history ended before the result could be proved"
            raise IncompleteHistoryError(msg)

    with pytest.raises(OSError, match="cannot close") as raised:
        fail_while_open()

    assert isinstance(raised.value.__context__, IncompleteHistoryError)


def test_close_runs_every_step_when_closing_stdout_fails(
    git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    reader = CommitReader(dir=git_repo.dir)
    proc = _child(reader)
    _fail_after_closing(monkeypatch, proc, "stdout")

    with pytest.raises(OSError, match="cannot close"):
        reader.close()

    assert proc.stdin is not None
    assert proc.stdin.closed
    assert proc.returncode == 0


def test_close_lets_a_healthy_child_exit_on_its_own(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    reader = CommitReader(dir=git_repo.dir)
    assert reader.read("HEAD") is not None

    reader.close()

    assert _child(reader).returncode == 0
