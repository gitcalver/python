# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import subprocess
from pathlib import Path

import pytest

from _helpers import GitRepo


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
