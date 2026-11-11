"""No module in the agent package imports a sibling as ``agents.…`` (ISSUE-0210).

The ``agents/`` folder installs as the package ``persatrix_agents``
(``agents/pyproject.toml``), and the image starts
``python -m persatrix_agents.server``. A module inside the package that writes
``from agents.x import y`` therefore works only where a folder named ``agents``
is on the import path:

* without one, the installed package fails to start
  (``ModuleNotFoundError: No module named 'agents'``);
* with one, as in the image, ``agents.x`` loads as a second copy of the module
  beside ``persatrix_agents.x``, with its own module-level state. A session,
  epoch or principal set in one copy is unset in the other.

Every test here imports ``agents.…`` from the repository root, so no test that
runs the code can see either fault. This one reads the import statements
instead, in every file of the package that is not a test and not a generated
stub, including the imports written inside functions.

A ruff rule cannot do this job. Ruff resolves a relative import against the
folder it sits in, so to ruff ``from .x import y`` inside ``agents/`` is an
import of ``agents.x``, and banning ``agents`` flags every relative import in
the package. CI also starts the installed package from an empty folder
(``.github/workflows/ci.yml``), which catches the imports that run at start-up.

The tests, ``scripts/`` and ``evaluators/`` are outside the package and import
``agents.…`` on purpose; this check does not read them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO_ROOT / "agents"

# Folders directly under agents/ that are not the package's own source: its
# tests import `agents.…` on purpose, the protobuf stubs are generated, and
# the last two are what a local build or virtualenv leaves behind.
_SKIPPED_TOP_LEVEL = {"tests", "generated", "build", ".venv"}


def _absolute_agents_imports(source: str) -> list[int]:
    """Line numbers of the imports in ``source`` that name ``agents`` absolutely."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        else:
            continue
        if any(name == "agents" or name.startswith("agents.") for name in names):
            lines.append(node.lineno)
    return sorted(lines)


def _package_sources() -> list[Path]:
    return sorted(
        path
        for path in AGENTS_DIR.rglob("*.py")
        if path.relative_to(AGENTS_DIR).parts[0] not in _SKIPPED_TOP_LEVEL
    )


def test_no_package_module_imports_a_sibling_as_agents() -> None:
    sources = _package_sources()
    # Positive control: the walk reads the package, sub-packages included, so
    # it cannot pass by finding no files.
    names = {path.relative_to(AGENTS_DIR).as_posix() for path in sources}
    assert {"server_cli.py", "memory/store.py", "persona_runtime/session_id.py"} <= names
    assert not any(name.startswith(("tests/", "generated/")) for name in names)

    found = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{line}"
        for path in sources
        for line in _absolute_agents_imports(path.read_text(encoding="utf-8"))
    ]
    assert not found, (
        "these lines import a sibling module as `agents.…`; inside agents/ write a "
        "relative import (`from .x import y`, `from ..x import y`), because the "
        "package installs as `persatrix_agents` (ISSUE-0210): " + ", ".join(found)
    )


@pytest.mark.parametrize(
    "source",
    [
        "from agents.prompt_loader import load_snippet\n",
        "from agents import security\n",
        "import agents\n",
        "import agents.session_id\n",
        "import os, agents.clock as clock\n",
        # Inside a function: it runs only when the function does, so starting
        # the package does not show it.
        "def cleanup():\n    from agents.observability import metrics\n",
        "if TYPE_CHECKING:\n    from agents.base import BaseAgent\n",
    ],
)
def test_the_check_sees_an_absolute_import(source: str) -> None:
    assert _absolute_agents_imports(source), source


@pytest.mark.parametrize(
    "source",
    [
        "from .prompt_loader import load_snippet\n",
        "from ..session_id import LEGACY_SESSION_ID\n",
        "from . import security\n",
        # A relative import of a sub-package that happens to be called agents.
        "from .agents import registry\n",
        "from persatrix_agents.server import AgentServer\n",
        "import agents_helper\n",
        "from agentsmith import tool\n",
        'logger = logging.getLogger("agents.tools.audit")\n',
    ],
)
def test_the_check_leaves_other_imports_alone(source: str) -> None:
    assert _absolute_agents_imports(source) == [], source


def test_the_check_reports_each_line() -> None:
    source = "import os\nfrom agents.a import b\n\n\ndef f():\n    import agents.c\n"
    assert _absolute_agents_imports(source) == [2, 6]
