# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import os
import subprocess
from pathlib import Path

import pytest

from _helpers import GitRepo


# Every git command in the suite inherits the environment. Variables that git
# exports to hooks (GIT_DIR, GIT_INDEX_FILE, ...) would point the fixtures at
# the outer repository, and global configuration (hooks, signing) would change
# what they do, so start from a clean slate. Git reads the global ignore and
# attributes files from $XDG_CONFIG_HOME/git or $HOME/.config/git whatever
# GIT_CONFIG_GLOBAL says, so both point into an empty per-test directory.
# Setting GIT_DEFAULT_HASH then puts every `git init`, whatever the outer value,
# in this format.
@pytest.fixture(autouse=True, params=["sha1", "sha256"])
def object_format(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> str:
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    param: str = request.param
    monkeypatch.setenv("GIT_DEFAULT_HASH", param)
    return param


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    repo_dir = str(tmp_path)
    subprocess.run(
        ["git", "init", "-b", "main", repo_dir],
        capture_output=True,
        text=True,
        check=True,
    )
    return GitRepo(dir=repo_dir)
