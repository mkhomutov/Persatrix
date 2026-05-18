"""RFC 0029 Phase 1 PR 2 — personal/society storage-boundary guard rail.

The facade exists so personal-tier storage has exactly one entry point
(``agents.memory.MemoryStore``).  PR 2 makes that boundary *enforceable*
with two halves of one guard rail (RFC 0029 PR plan PR 2):

* a ruff ``TID251`` rule blocks a direct ``import aiosqlite`` in any file
  outside ``agents/memory/`` — the import is how a caller would bypass
  the facade;
* a ``DeprecationWarning`` fires on direct construction of the per-tier
  classes (``EpisodicMemory`` / ``RelationshipMemory``) outside
  ``agents/memory/`` — construction is the other bypass.

Construction *inside* ``agents/memory/`` (the ``MemoryStore`` facade, the
shared-pool wrapper, the tier modules themselves) stays silent: the
facade is the supported builder, and the ``MemoryFacade`` alias is the
warning-free one-minor-version compatibility path.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from agents.memory import _boundary
from agents.memory.episodic import EpisodicMemory
from agents.memory.relationship import RelationshipMemory
from agents.memory.store import MemoryStore

# tests/unit/python/test_memory_boundary.py → repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENTS_DIR = _REPO_ROOT / "agents"

_RUFF_AVAILABLE = importlib.util.find_spec("ruff") is not None
_requires_ruff = pytest.mark.skipif(
    not _RUFF_AVAILABLE, reason="ruff (dev dependency) not installed",
)


# ─── DeprecationWarning on direct tier construction ──────────────────


def test_direct_episodic_construction_outside_memory_warns() -> None:
    """Constructing ``EpisodicMemory`` from a non-memory module is deprecated."""
    with pytest.warns(DeprecationWarning, match="EpisodicMemory"):
        EpisodicMemory(agent_id="alice")


def test_direct_relationship_construction_outside_memory_warns() -> None:
    """Constructing ``RelationshipMemory`` from a non-memory module is deprecated."""
    with pytest.warns(DeprecationWarning, match="RelationshipMemory"):
        RelationshipMemory(agent_id="alice")


def test_memorystore_construction_does_not_warn() -> None:
    """The facade builds ``EpisodicMemory`` from inside ``agents/memory/``.

    ``MemoryStore.__init__`` (``agents/memory/store.py``) constructs the
    episodic tier — that in-boundary construction must stay silent, or
    every facade caller would inherit a spurious deprecation warning.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        MemoryStore(agent_id="alice")
    offending = [
        w for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "EpisodicMemory" in str(w.message)
    ]
    assert not offending, f"facade construction emitted: {offending}"


# ─── Boundary classification (the warning's frame check) ─────────────


def test_paths_inside_memory_dir_are_internal() -> None:
    """Both per-tier home modules classify as inside the boundary."""
    memory_dir = Path(_boundary.__file__).parent
    assert _boundary.is_construction_external(str(memory_dir / "episodic.py")) is False
    assert _boundary.is_construction_external(str(memory_dir / "relationship.py")) is False
    assert _boundary.is_construction_external(str(memory_dir / "store.py")) is False


def test_paths_outside_memory_dir_are_external() -> None:
    """Non-memory callers — and this test module — classify as external."""
    agents_dir = Path(_boundary.__file__).parent.parent
    assert _boundary.is_construction_external(str(agents_dir / "persona.py")) is True
    assert _boundary.is_construction_external(str(agents_dir / "memory_helper.py")) is True
    assert _boundary.is_construction_external(__file__) is True


def test_unknown_caller_file_classifies_as_external() -> None:
    """An empty / unknown caller filename classifies as external.

    ``warn_external_construction`` reaches this branch when ``sys._getframe``
    yields a frame whose ``co_filename`` is empty or unset.  A guard rail
    must fail *open*: a spurious ``DeprecationWarning`` is harmless noise,
    but silently misclassifying a real facade bypass as in-boundary is not —
    so an unknown filename counts as external.
    """
    assert _boundary.is_construction_external("") is True


# ─── Lint rule: TID251 blocks direct aiosqlite outside agents/memory/ ─


def _ruff_tid251(stdin_filename: str) -> list[dict]:
    """Run ruff's ``TID251`` rule over a one-line ``import aiosqlite`` module.

    ``stdin_filename`` is the path ruff treats the piped content as living
    at; it drives per-file-ignore matching, which is the boundary under
    test.  ``cwd`` is ``agents/`` so ruff discovers ``agents/pyproject.toml``
    exactly as ``make lint`` (``cd agents && ruff check .``) does.
    """
    proc = subprocess.run(
        [
            sys.executable, "-m", "ruff", "check",
            "--select", "TID251", "--no-cache",
            "--output-format", "json",
            "--stdin-filename", stdin_filename, "-",
        ],
        input="import aiosqlite\n",
        capture_output=True,
        text=True,
        cwd=_AGENTS_DIR,
    )
    # ruff exits 0 (clean) or 1 (violations); anything else is a config /
    # invocation error and should fail loudly rather than silently pass.
    if proc.returncode not in (0, 1):
        raise AssertionError(
            f"ruff exited {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return json.loads(proc.stdout or "[]")


@_requires_ruff
def test_lint_rule_flags_direct_aiosqlite_outside_memory() -> None:
    """A direct ``import aiosqlite`` in a non-memory module is a TID251 error."""
    findings = _ruff_tid251("persona_runtime/sample.py")
    assert any(f["code"] == "TID251" for f in findings), findings


@_requires_ruff
def test_lint_rule_silent_for_direct_aiosqlite_inside_memory() -> None:
    """The same import inside ``agents/memory/`` is exempt — that dir IS the boundary."""
    findings = _ruff_tid251("memory/sample.py")
    assert not any(f["code"] == "TID251" for f in findings), findings
