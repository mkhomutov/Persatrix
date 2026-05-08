"""
Guards the explicit `[tool.setuptools].packages` list in
`agents/pyproject.toml` against the ISSUE-0046 failure mode:

A new sub-package is added under `agents/` (e.g. `agents/temporal/`)
but the contributor forgets to append `persatrix_agents.<name>` to
the explicit packages list. Editable installs and source-tree pytest
runs read directly from the working directory, so the gap is invisible
locally — but `pip install .` (and therefore `Dockerfile.agent`) omits
the directory from the wheel, and every agent container crash-loops
on startup with `ModuleNotFoundError`.

Auto-discovery is not an option here because of the `agents/ →
persatrix_agents` directory remap (see the maintainer note at the top
of `[tool.setuptools]` in pyproject.toml). The mitigation is this
test: enumerate every importable directory under `agents/` and pin it
to the declared list.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

# Directories that contain `__init__.py` but are not importable as
# `persatrix_agents.<name>` from the installed wheel. Keep this list
# tight — anything added here is an explicit waiver, not an oversight.
EXCLUDE_DIRS = {
    # Build/install metadata; not a runtime package.
    "Persatrix_agents.egg-info",
    # Bytecode cache; never a real package.
    "__pycache__",
}


def _agents_root() -> Path:
    return Path(__file__).resolve().parents[3] / "agents"


def _pyproject_path() -> Path:
    return _agents_root() / "pyproject.toml"


def _discover_subpackages() -> set[str]:
    """Return every `persatrix_agents.<name>` for which agents/<name>/__init__.py exists."""
    root = _agents_root()
    found: set[str] = set()
    for init in root.rglob("__init__.py"):
        rel = init.relative_to(root).parent
        # The repo root agents/ itself maps to `persatrix_agents`; the
        # __init__.py at agents/__init__.py contributes the top-level
        # package, not a sub-package.
        if rel == Path("."):
            found.add("persatrix_agents")
            continue
        # Skip excluded leaf names anywhere in the path.
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        dotted = "persatrix_agents." + ".".join(rel.parts)
        found.add(dotted)
    return found


def _declared_packages() -> set[str]:
    with _pyproject_path().open("rb") as fh:
        data = tomllib.load(fh)
    return set(data["tool"]["setuptools"]["packages"])


def test_every_subpackage_on_disk_is_declared() -> None:
    """ISSUE-0046 guard: agents/<x>/__init__.py ⇒ persatrix_agents.<x> in pyproject.

    A `pip install .` build (the Docker path) only ships the directories
    listed under `[tool.setuptools].packages` because we cannot use
    `find` auto-discovery with the agents/→persatrix_agents remap.
    """
    on_disk = _discover_subpackages()
    declared = _declared_packages()
    missing = sorted(on_disk - declared)
    assert not missing, (
        f"Sub-package(s) exist under agents/ but are not declared in "
        f"agents/pyproject.toml [tool.setuptools].packages: {missing}. "
        "Without this entry, `pip install .` (and Dockerfile.agent) will "
        "omit the directory from the wheel and every agent container will "
        "crash-loop on the first import. See ISSUE-0046."
    )


def test_no_declared_package_is_missing_from_disk() -> None:
    """Symmetric guard: every declared package must exist on disk.

    Catches the inverse drift — a package was deleted/renamed but the
    explicit list was not updated, which makes `pip install .` fail
    with `error: package directory '...' does not exist`.
    """
    on_disk = _discover_subpackages()
    declared = _declared_packages()
    orphaned = sorted(declared - on_disk)
    assert not orphaned, (
        f"agents/pyproject.toml declares package(s) that no longer exist "
        f"on disk: {orphaned}. Update [tool.setuptools].packages to match."
    )


def test_tomllib_available() -> None:
    """Sanity: pyproject.toml is parseable from the test runtime.

    `tomllib` is stdlib on Python 3.11+, which the project requires
    (`requires-python = ">=3.11"`). Failing here means the test
    environment is older than the project's declared floor.
    """
    assert sys.version_info >= (3, 11)
    with _pyproject_path().open("rb") as fh:
        data = tomllib.load(fh)
    assert data["project"]["name"] == "Persatrix-agents"
