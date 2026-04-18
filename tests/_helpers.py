# Copyright © 2026 Michael Shields
# SPDX-License-Identifier: MIT

import os
import subprocess
from pathlib import Path


class GitRepo:
    def __init__(self, dir: str) -> None:
        self.dir = dir
        self._env: dict[str, str] = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            "TZ": "UTC",
        }

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=self.dir,
            env=env or self._env,
            check=False,
        )
        if result.returncode != 0:
            msg = f"git {' '.join(args)} failed: {result.stderr}"
            raise RuntimeError(msg)
        return result.stdout.strip()

    def commit_at(self, date_str: str, *, committer_date: str | None = None) -> str:
        env = {
            **self._env,
            "GIT_AUTHOR_DATE": date_str,
            "GIT_COMMITTER_DATE": committer_date or date_str,
        }
        self.git("commit", "--allow-empty", "-m", "commit", env=env)
        return self.head_hash()

    def write_file(self, name: str, content: str = "dirty") -> None:
        Path(self.dir, name).write_text(content)

    def create_branch(self, name: str) -> None:
        self.git("checkout", "-b", name)

    def checkout(self, name: str) -> None:
        self.git("checkout", name)

    def head_hash(self) -> str:
        return self.git("rev-parse", "HEAD")

    def parent_hash(self, rev: str = "HEAD") -> str:
        return self.git("rev-parse", f"{rev}~1")

    def merge(self, branch: str, date: str) -> None:
        env = {
            **self._env,
            "GIT_MERGE_AUTOEDIT": "no",
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        }
        self.git("merge", "--no-ff", branch, "-m", "merge", env=env)
