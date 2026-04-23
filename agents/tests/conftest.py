"""Conftest for agents/tests/.

When pytest is invoked as ``pytest agents/tests/`` it picks up
``agents/pyproject.toml`` as its config file and treats ``agents/`` as the
rootdir, so the repo-root ``tests/conftest.py`` is *not* loaded.  This
conftest re-applies the same aiosqlite-worker-daemonisation safety net so
leaked connections cannot block process exit and produce a phantom hang
after ``passed`` is printed.

See ``tests/_test_infra.py`` for the full rationale.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ``tests/`` lives at the repo root, two levels up from this file.
_repo_tests = Path(__file__).resolve().parent.parent.parent / "tests"
if str(_repo_tests) not in sys.path:
    sys.path.insert(0, str(_repo_tests))

from _test_infra import daemonize_aiosqlite_workers  # noqa: E402

daemonize_aiosqlite_workers()
