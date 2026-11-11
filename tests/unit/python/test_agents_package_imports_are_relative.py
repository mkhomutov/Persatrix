"""No module in the agent package imports a sibling by an absolute name (ISSUE-0210).

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

The installed name is no better inside the package. The evaluation harness
starts the runtime as ``python -m agents.server``, and there a line that
writes ``from persatrix_agents.x import y`` loads the second copy. Only a
relative import names the copy that is already running, so both absolute
names are refused.

Every test here imports ``agents.…`` from the repository root, so no test that
runs the code can see either fault. This one reads the source instead, in
every file of the package that is not a test and not a generated stub. It
reads ``import`` statements, including the ones written inside functions, and
the calls that import by a string: ``importlib.import_module``,
``__import__`` and ``pkgutil.resolve_name``, when their first argument is a
literal. A name built while the program runs is out of its reach.

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


# The two absolute names the package answers to: its folder's, and the one it
# installs as.
_PACKAGE_NAMES = ("agents", "persatrix_agents")

# The calls that import a module named by a string.
_STRING_IMPORTERS = {"import_module", "__import__", "resolve_name"}


def _names_the_package(name: str) -> bool:
    # `pkgutil.resolve_name` also takes "package.module:attribute". A name
    # with a leading dot is relative, the form wanted: its root is empty.
    root = name.partition(":")[0].partition(".")[0]
    return root in _PACKAGE_NAMES


def _string_import(node: ast.Call) -> str | None:
    """The module a string-import call names with a literal, or ``None``."""
    func = node.func
    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if called not in _STRING_IMPORTERS or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _absolute_agents_imports(source: str) -> list[int]:
    """Line numbers in ``source`` that import the package by an absolute name."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        elif isinstance(node, ast.Call) and (named := _string_import(node)) is not None:
            names = [named]
        else:
            continue
        if any(_names_the_package(name) for name in names):
            lines.append(node.lineno)
    return sorted(lines)


def _package_sources() -> list[Path]:
    return sorted(
        path
        for path in AGENTS_DIR.rglob("*.py")
        if path.relative_to(AGENTS_DIR).parts[0] not in _SKIPPED_TOP_LEVEL
    )


def test_no_package_module_imports_a_sibling_by_an_absolute_name() -> None:
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
        "these lines import a sibling module as `agents.…` or `persatrix_agents.…`; "
        "inside agents/ write a relative import (`from .x import y`, `from ..x import "
        "y`). The package runs under both names, and an absolute one loads a second "
        "copy or none (ISSUE-0210): " + ", ".join(found)
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
        # The installed name is as wrong as the folder's: the evaluation
        # harness starts the runtime as `python -m agents.server`, and there
        # this line loads a second copy under `persatrix_agents`.
        "from persatrix_agents.server import AgentServer\n",
        "import persatrix_agents.clock\n",
        # An import written as a string runs the same way.
        'module = importlib.import_module("agents.org_validate")\n',
        'module = import_module("persatrix_agents.tools")\n',
        'module = __import__("agents.session_id")\n',
        'now = pkgutil.resolve_name("agents.clock:agent_now")\n',
        'tools = resolve_name("agents")\n',
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
        "import agents_helper\n",
        "from agentsmith import tool\n",
        "import persatrix_agents_extras\n",
        'logger = logging.getLogger("agents.tools.audit")\n',
        # A string import that names a sibling relatively, as agents/validate.py does.
        'module = importlib.import_module(".org_validate", __package__)\n',
        # A name only known when the line runs is not this check's to judge.
        "module = importlib.import_module(name)\n",
        'module = importlib.import_module("agentsmith.tools")\n',
        'path = resolve_name("os.path:join")\n',
    ],
)
def test_the_check_leaves_other_imports_alone(source: str) -> None:
    assert _absolute_agents_imports(source) == [], source


def test_the_check_reports_each_line() -> None:
    source = (
        "import os\n"
        "from agents.a import b\n"
        "\n"
        "\n"
        "def f():\n"
        "    import agents.c\n"
        '    return importlib.import_module("persatrix_agents.d")\n'
    )
    assert _absolute_agents_imports(source) == [2, 6, 7]
