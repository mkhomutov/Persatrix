"""RFC 0045 §B — the MIT↛BUSL dependency-direction gate (Python half).

RFC 0045 splits the codebase into three license tiers and fixes one
load-bearing rule: **imports may only point down-tier** (`MIT ← BUSL ←
Private`). The clearest failure this prevents: a small, generically-useful
"MIT-candidate" primitive (the wallet client, the prompt loader / safety
snippets, the provider abstraction + mock) quietly grows an import into an
orchestrator-internal (BUSL) module. On the next mirror/release that one-line
diff ships BUSL-licensed source under an MIT grant — a licensing violation, not
a style nit (RFC 0045 §M-4). The boundary therefore has to be a hard CI gate,
seeded *before* any code physically moves.

The Python half of that gate is an [import-linter](https://import-linter.readthedocs.io/)
``forbidden`` contract declared in ``agents/pyproject.toml``. It names each
MIT-candidate module as a source that may not import any orchestrator-coupled
peer. The candidates are each already leaf on ``main`` (they import only
stdlib, third-party SDKs, the generated wallet stubs, or a fellow MIT leaf), so
the contract is green at acceptance and only a genuine future up-import turns it
red.

This test file is the gate's own regression suite. It asserts the contract
(1) covers every MIT candidate, (2) does not silently whitelist an
orchestrator module through ``ignore_imports``, (3) passes on the current tree,
and (4) actually has *teeth* — that the ``forbidden = <root>.*`` shape catches a
real up-import. Point (4) guards a sharp edge discovered while authoring the
contract: forbidding the *ancestor* package (``persatrix_agents``) is silently
vacuous — import-linter does not treat a source importing a sibling under that
same ancestor as a violation. Only forbidding the *children*
(``persatrix_agents.*``) has teeth, so the suite proves that shape works rather
than trusting it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO_ROOT / "agents"
PYPROJECT = AGENTS_DIR / "pyproject.toml"

# The MIT-candidate leaf primitives RFC 0045 §B seeds the contract on. Kept in
# sync with the ``source_modules`` list in agents/pyproject.toml's
# ``[tool.importlinter]`` block — this set is the test's independent copy so a
# silently-dropped candidate fails loudly here.
EXPECTED_MIT_CANDIDATES = {
    "persatrix_agents.wallet_client",
    "persatrix_agents.prompt_loader",
    "persatrix_agents.prompt_safety",
    "persatrix_agents.llm_types",
    "persatrix_agents.llm_offline",
}

# The only first-party imports the candidates are allowed to make: a fellow MIT
# leaf, or the generated wallet proto stubs (the published MIT wire contract,
# RFC 0045 §F / RFC 0046 §D). Anything else appearing on the right-hand side of
# an ``ignore_imports`` entry would be an orchestrator-coupled module smuggled
# past the gate.
ALLOWED_IGNORE_TARGETS = EXPECTED_MIT_CANDIDATES | {
    "persatrix_agents.generated.wallet_pb2",
    "persatrix_agents.generated.wallet_pb2_grpc",
}


def _lint_imports_cmd() -> str | None:
    """Return the ``lint-imports`` console-script path, or ``None``.

    import-linter ships no ``__main__`` (so ``python -m importlinter`` fails);
    the entry point is the ``lint-imports`` script. It is usually on ``PATH``,
    but in a non-activated venv it sits next to the interpreter that has
    import-linter installed — check both. Returning ``None`` lets the
    subprocess tests skip cleanly when the dev/CI extra is not installed.
    """
    found = shutil.which("lint-imports")
    if found:
        return found
    candidate = Path(sys.executable).parent / "lint-imports"
    return str(candidate) if candidate.exists() else None


def _load_forbidden_contract() -> dict:
    """Parse the single ``forbidden`` import-linter contract from pyproject."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    importlinter = data.get("tool", {}).get("importlinter", {})
    contracts = [c for c in importlinter.get("contracts", []) if c.get("type") == "forbidden"]
    assert contracts, (
        "agents/pyproject.toml has no [tool.importlinter] forbidden contract — "
        "the RFC 0045 §B dependency-direction gate is missing"
    )
    assert len(contracts) == 1, "expected exactly one forbidden contract"
    return contracts[0]


def test_contract_root_package_is_persatrix_agents() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert data["tool"]["importlinter"]["root_package"] == "persatrix_agents"


def test_contract_covers_every_mit_candidate() -> None:
    contract = _load_forbidden_contract()
    assert set(contract["source_modules"]) == EXPECTED_MIT_CANDIDATES
    # Forbid the *children* of the root, not the root itself — see the module
    # docstring: forbidding the ancestor is silently vacuous.
    assert "persatrix_agents.*" in contract["forbidden_modules"]
    assert "persatrix_agents" not in contract["forbidden_modules"], (
        "forbidding the ancestor package 'persatrix_agents' is vacuous — "
        "use the child wildcard 'persatrix_agents.*'"
    )


def test_ignore_imports_never_whitelists_an_orchestrator_module() -> None:
    contract = _load_forbidden_contract()
    for entry in contract.get("ignore_imports", []):
        _src, _, target = entry.partition(" -> ")
        assert target.strip() in ALLOWED_IGNORE_TARGETS, (
            f"ignore_imports entry {entry!r} whitelists a non-leaf target — an "
            "orchestrator-coupled (BUSL) module must never be ignored by the gate"
        )


@pytest.mark.skipif(_lint_imports_cmd() is None, reason="import-linter not installed")
def test_dependency_direction_contract_passes_on_current_tree() -> None:
    """The seeded contract is green on ``main`` (every candidate is leaf)."""
    cmd = _lint_imports_cmd()
    assert cmd is not None  # guaranteed by skipif; narrows the type for mypy
    result = subprocess.run(
        [cmd, "--no-cache"],
        cwd=AGENTS_DIR,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "the MIT↛BUSL import-direction contract is broken — an MIT-candidate "
        "primitive now imports an orchestrator-coupled (BUSL) module:\n"
        + result.stdout
        + result.stderr
    )


@pytest.mark.skipif(_lint_imports_cmd() is None, reason="import-linter not installed")
def test_forbidden_child_wildcard_has_teeth(tmp_path: Path) -> None:
    """A ``forbidden = <root>.*`` contract catches a real up-import.

    Mirrors the production contract's shape against a throwaway package whose
    "leaf" reaches up into an orchestrator submodule. Proves the gate is not
    vacuous: if this shape ever stops catching the violation, the production
    contract is silently toothless even while reporting "KEPT".
    """
    pkg = tmp_path / "demo_pkg"
    (pkg / "orch").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "orch" / "__init__.py").write_text("")
    (pkg / "orch" / "server.py").write_text("VALUE = 1\n")
    (pkg / "leaf.py").write_text("from demo_pkg.orch import server\n\n_ = server\n")

    config = tmp_path / ".importlinter"
    config.write_text(
        "[importlinter]\n"
        "root_package = demo_pkg\n\n"
        "[importlinter:contract:teeth]\n"
        "name = leaf must not import orchestrator submodules\n"
        "type = forbidden\n"
        "source_modules =\n"
        "    demo_pkg.leaf\n"
        "forbidden_modules =\n"
        "    demo_pkg.*\n"
    )

    cmd = _lint_imports_cmd()
    assert cmd is not None  # guaranteed by skipif; narrows the type for mypy
    result = subprocess.run(
        [cmd, "--config", str(config), "--no-cache"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    assert result.returncode != 0, (
        "forbidden = <root>.* failed to catch a leaf->orchestrator up-import; the "
        "real gate would be vacuous despite reporting success:\n" + result.stdout
    )
    assert "demo_pkg.leaf -> demo_pkg.orch.server" in result.stdout
