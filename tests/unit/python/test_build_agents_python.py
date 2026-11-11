"""`make build-agents` installs into the Python it is given (ISSUE-0228).

The recipe used to install through a separate `PIP := pip3` variable, so
`make build-agents PYTHON=.venv/bin/python` still ran whatever `pip3` was
first on the path, and CONTRIBUTING's Quick Start, which begins with that
command, installed the project into the system Python.

The recipe now runs `$(PYTHON) -m pip`, so the one variable picks the
interpreter and its installer, and the Quick Start creates and activates a
virtual environment first. These tests pin both halves, and that git ignores
the folder the Quick Start creates.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# When this suite runs under make (`make test-python PYTHON=…`), make hands its
# command-line variables to child makes through these. They are dropped so
# each test below sees only the variables it passes itself.
_MAKE_ENV = ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKELEVEL")


def _install_line(*make_args: str) -> str:
    """The one install command `make -n build-agents` prints. Nothing is installed."""
    env = {key: value for key, value in os.environ.items() if key not in _MAKE_ENV}
    proc = subprocess.run(
        ["make", "--no-print-directory", "-n", "build-agents", *make_args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if " install " in line]
    assert len(lines) == 1, f"expected one install line, found {lines}"
    return lines[0]


def test_build_agents_installs_with_the_python_it_is_given() -> None:
    line = _install_line("PYTHON=/somewhere/.venv/bin/python")
    assert line.startswith("/somewhere/.venv/bin/python -m pip install "), line


def test_build_agents_installs_with_python3_by_default() -> None:
    """With no `PYTHON=`, as in CI, the installer is still the interpreter's own."""
    line = _install_line()
    assert line.startswith("python3 -m pip install "), line


def test_quick_start_makes_a_virtual_environment_before_it_installs() -> None:
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    start = text.find("### Quick Start")
    assert start != -1, "CONTRIBUTING.md has no Quick Start section"
    end = text.find("\n### ", start + 1)
    section = text[start:end]

    create = section.find("python3 -m venv .venv")
    activate = section.find("source .venv/bin/activate")
    install = section.find("make build-agents")
    assert create != -1, "the Quick Start does not create a virtual environment"
    assert activate != -1, "the Quick Start does not activate the virtual environment"
    assert install != -1, "the Quick Start does not run make build-agents"
    assert create < activate < install, (
        "the Quick Start must create and activate .venv before it installs into it"
    )


def test_git_ignores_the_virtual_environment() -> None:
    """Python 3.13 writes an ignore file inside a new virtual environment;
    3.11 and 3.12 do not, so there the folder would show up as untracked."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".venv/" in ignored
