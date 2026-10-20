"""Fixture git repositories for the tests of scripts that call git themselves.

A fixture repository commits as a fixed identity and takes nothing from the
caller's git setup — no hooks, signing or templates, no GIT_DIR pointing
elsewhere. The scripts under test run under the same environment, since they
call git themselves.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def run(repo: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run *argv* in *repo* under ``GIT_ENV``, capturing its output."""
    return subprocess.run(
        argv, cwd=repo, capture_output=True, encoding="utf-8", check=False, env=GIT_ENV
    )


def git(repo: Path, *args: str) -> str:
    """``git <args>`` in *repo*: its stdout, or the test fails with its stderr."""
    proc = run(repo, "git", *args)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout
