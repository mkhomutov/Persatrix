"""Nothing in ``agents/decision/`` imports a runtime loop (RFC 0028 PR plan, PR 1).

The persona's turn and the task agent's step will both call the decision
contract (PRs 5 and 6). If a decision module imported either loop, the loops
could not import it back without a cycle, and the contract would stop being
one that both runtimes share. So the package may reach, directly or through
what it imports, only itself and a short list of type modules.

The walk reads import statements rather than ``sys.modules``: importing any
module under ``agents`` first runs ``agents/__init__.py``, which loads both
runtimes, so a runtime check would see them whatever the decision modules
import. So the package root's own ``__init__`` is walked only when a module
takes a name from it (``from .. import X``, ``import agents``); every other
package's ``__init__`` is walked whenever a module under it is imported.

import-linter's contract (``make imports-check``) is not used here: its
import graph does not count the package ``__init__`` an import runs first.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# The first-party modules the decision package may reach: type definitions
# with no loop in them, and the config reader `persona_types` loads inside a
# function for a sub-agent's default model.
ALLOWED = {
    "agents.persona_types",
    "agents.llm_types",
    "agents.clock",
    "agents.optimization",
}


def _module_path(root: Path, module: str) -> Path | None:
    base = root.joinpath(*module.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def _direct_imports(root: Path, module: str) -> set[str]:
    path = _module_path(root, module)
    assert path is not None, module
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                stem = ".".join(parts[: len(parts) - node.level + 1])
                if node.module:
                    stem = f"{stem}.{node.module}"
            else:
                stem = node.module or ""
            # `from x import y` may import module x.y as well as a name in x.
            names = [stem, *(f"{stem}.{alias.name}" for alias in node.names)]
        else:
            continue
        for name in names:
            if name == "persatrix_agents" or name.startswith("persatrix_agents."):
                name = "agents" + name.removeprefix("persatrix_agents")
            if name != "agents" and not name.startswith("agents."):
                continue
            # Importing a.b.c runs package a.b's __init__ first, so it is
            # reached too. The root's own runs for every import and counts
            # only when a name is taken from it (the bare `agents`).
            parts = name.split(".")
            for end in range(min(2, len(parts)), len(parts) + 1):
                prefix = ".".join(parts[:end])
                if _module_path(root, prefix) is not None:
                    found.add(prefix)
    return found


def _reached(root: Path, start: set[str]) -> set[str]:
    seen: set[str] = set()
    todo = list(start)
    while todo:
        module = todo.pop()
        if module not in seen:
            seen.add(module)
            todo.extend(_direct_imports(root, module) - seen)
    return seen


def _decision_modules(root: Path) -> set[str]:
    """Every module under ``agents/decision/``, subpackages included."""
    modules = set()
    for path in (root / "agents" / "decision").rglob("*.py"):
        parts = path.relative_to(root).with_suffix("").parts
        modules.add(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return modules


def test_the_decision_package_reaches_no_runtime_loop() -> None:
    start = _decision_modules(REPO_ROOT)
    assert "agents.decision.decision_types" in start
    outside = {m for m in _reached(REPO_ROOT, start) if not m.startswith("agents.decision")}
    assert outside <= ALLOWED, f"agents/decision/ reaches {sorted(outside - ALLOWED)}"
    # Positive control: the walk does see the one import the package needs,
    # so an empty walk cannot pass by finding nothing.
    assert "agents.persona_types" in outside


@pytest.mark.parametrize(
    "bad_import",
    [
        "from ...helper import thing\n",
        # A name the package root re-exports, which runs its __init__.
        "from ... import thing\n",
        "import agents\n",
        "from persatrix_agents import thing\n",
        # A module whose package __init__ reaches the loop.
        "from ...tools.permissions import Gate\n",
    ],
)
def test_the_walk_catches_a_loop_reached_from_a_subpackage(
    tmp_path: Path, bad_import: str
) -> None:
    files = {
        "agents/__init__.py": "from .helper import thing\n",
        "agents/decision/__init__.py": "",
        "agents/decision/sub/__init__.py": "",
        "agents/decision/sub/bad.py": bad_import,
        "agents/helper.py": "from .persona_runtime.action_loop import run\n",
        "agents/tools/__init__.py": "from ..persona_runtime.action_loop import run\n",
        "agents/tools/permissions.py": "class Gate: ...\n",
        "agents/persona_runtime/__init__.py": "",
        "agents/persona_runtime/action_loop.py": "def run(): ...\n",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    reached = _reached(tmp_path, _decision_modules(tmp_path))
    assert "agents.persona_runtime.action_loop" in reached
