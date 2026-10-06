# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _helpers import GitRepo

OID_LENGTH = {"sha1": 40, "sha256": 64}


def test_git_environment_is_pinned() -> None:
    names = {name for name in os.environ if name.startswith("GIT_")}
    assert names == {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_DEFAULT_HASH"}


def test_user_level_git_files_are_unreachable(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    base = tmp_path_factory.getbasetemp()
    for name in ("HOME", "XDG_CONFIG_HOME"):
        assert Path(os.environ[name]).is_relative_to(base), name


def test_git_repo_fixture_uses_object_format(
    git_repo: GitRepo, object_format: str
) -> None:
    git_repo.commit_at("2026-04-10T09:00:00Z")
    assert git_repo.git("rev-parse", "--show-object-format") == object_format
    assert len(git_repo.head_hash()) == OID_LENGTH[object_format]


def test_every_test_runs_in_both_object_formats(
    request: pytest.FixtureRequest,
) -> None:
    # A child process with PYTEST_ADDOPTS cleared collects every variant,
    # whatever -k or --deselect narrows this run.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            f"{__file__}::{request.node.originalname}",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    ids = set(re.findall(r"\[(\w+)\]$", result.stdout, re.MULTILINE))
    assert ids == {"sha1", "sha256"}
