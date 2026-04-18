# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

import gitcalver
from gitcalver._branch import detect_branch
from gitcalver._errors import ExitError
from gitcalver._hatch_hooks import hatch_register_version_source
from gitcalver._hatch_source import GitCalverSource
from gitcalver._version import reverse, walk_first_parent
from gitcalver.cli import _parse_args, main, run

from _helpers import GitRepo

if TYPE_CHECKING:
    from pathlib import Path


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


# --- Error cases ---


def test_not_a_repo(tmp_path: Path) -> None:
    repo = GitRepo(dir=str(tmp_path))
    _, code = run_cmd(repo)
    assert code == 1


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


# --- UTC midnight boundary ---


def test_utc_midnight_boundary(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T23:59:00Z")
    git_repo.commit_at("2026-04-11T00:01:00Z")
    out, code = run_cmd(git_repo)
    assert code == 0
    assert out == "20260411.1"


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


# --- Walk first parent: no commits ---


def test_walk_first_parent_no_commits(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    with pytest.raises(ExitError, match="no commits found"):
        walk_first_parent(dir=git_repo.dir, rev="HEAD..HEAD")


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
    assert "not a gitcalver version or git revision" in out


def test_reverse_short(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    head = git_repo.head_hash()
    out, code = run_cmd(git_repo, "--short", "20260410.1")
    assert code == 0
    assert head.startswith(out)
    assert len(out) == 7


def test_reverse_short_ignores_core_abbrev(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.git("config", "core.abbrev", "12")
    out, code = run_cmd(git_repo, "--short", "20260410.1")
    assert code == 0
    assert len(out) == 7


def test_dirty_hash_ignores_core_abbrev(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.git("config", "core.abbrev", "12")
    git_repo.write_file("dirty.txt")
    out, code = run_cmd(git_repo, "--dirty", "-dirty")
    assert code == 0
    hash_part = out.rsplit(".", 1)[1]
    assert len(hash_part) == 7


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
    _out, code = run_cmd(git_repo, feature_hash)
    assert code == 3


def test_forward_invalid_revision(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    _, code = run_cmd(git_repo, "not-a-valid-ref")
    assert code == 1


# --- First-parent / merge behavior ---


def test_merge_first_parent_only(git_repo: GitRepo) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    git_repo.create_branch("feature")
    git_repo.commit_at("2026-04-10T10:00:00Z")
    git_repo.commit_at("2026-04-10T11:00:00Z")
    git_repo.checkout("main")
    git_repo.commit_at("2026-04-10T12:00:00Z")
    git_repo.merge("feature", "2026-04-10T13:00:00Z")

    out, code = run_cmd(git_repo)
    assert code == 0
    # 3 first-parent commits on main: initial, 12:00, merge.
    # Feature branch commits are not counted.
    assert out == "20260410.3"


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
    assert len(out) == 40  # full SHA


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
            "--short",
            "abc123",
        ]
    )
    assert opts.prefix == "v0."
    assert opts.dirty == "-dirty"
    assert opts.no_dirty_hash is True
    assert opts.branch == "develop"
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


# --- Shallow clone rejected ---


def test_shallow_clone_rejected(tmp_path: Path) -> None:
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
    assert code == 1
    assert "shallow clone" in out


# --- Partial clone accepted ---


def test_partial_clone_accepted(tmp_path: Path) -> None:
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
        ["git", "clone", "--filter=blob:none", f"file://{origin_dir}", clone_dir],
        capture_output=True,
        check=True,
    )
    clone = GitRepo(dir=clone_dir)
    out, code = run_cmd(clone)
    assert code == 0
    assert out == "20260410.2"


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
