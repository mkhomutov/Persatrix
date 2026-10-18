"""One read-only git call, shared by the scripts that need it (ISSUE-0135).

Six independent "run git, capture stdout, tolerate failure" implementations
had grown across ``scripts/`` and ``tests/perf/``, each with its own timeout,
encoding, and error handling. New call sites use this one; the older sites
migrate as they are touched.

Deliberately narrow: stdout or ``None``. A caller that needs the exit code or
stderr is doing something this helper is not for. ``is_shallow`` is built on
it: a script that reads the whole log asks it first.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Generous — these are local metadata reads; anything near it means git is
#: wedged (index.lock, a credential prompt), and the caller wants an answer
#: either way.
_TIMEOUT_S = 10


def git_output(repo_root: Path, *args: str) -> str | None:
    """Return ``git <args>`` stdout run in *repo_root*, or ``None`` if git cannot answer.

    ``None`` covers every failure the same way — git missing, not a
    repository, a non-zero exit, a hang — so callers fall back to their
    conservative default instead of branching on the cause.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            # Explicit codec: ``text=True`` decodes with the locale encoding and
            # raises UnicodeDecodeError, a ValueError that would escape the
            # except clause below.
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=_TIMEOUT_S,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def is_shallow(repo_root: Path) -> bool:
    """Whether *repo_root* is a shallow clone, whose log stops at its oldest fetched commit.

    Anything built from the whole log comes out cut short there (cloud
    sessions clone with ``--depth 50``). ``False`` when git cannot answer: the
    caller's own git call then fails the way it always did.
    """
    out = git_output(repo_root, "rev-parse", "--is-shallow-repository")
    return out is not None and out.strip() == "true"
