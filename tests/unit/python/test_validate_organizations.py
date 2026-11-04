"""Tests for ``config/organizations.yaml`` validation (RFC 0012 PR 1).

``make validate`` checks the file's shape against
``schemas/organization.schema.json`` and then its graph with
``agents/org_validate.py``. The fixtures under
``tests/fixtures/organizations/`` are shared with the Go loader (RFC 0012
PR 2); their README says how a file name names the rule it breaks.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agents.validate import validate_config_dir

_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = _ROOT / "tests" / "fixtures" / "organizations"
_SCHEMAS = _ROOT / "schemas"
_SHIPPED = _ROOT / "config" / "organizations.yaml"


def _validate(tmp_path: Path, organizations: Path, *, agents: Path | None) -> tuple[bool, list]:
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(organizations, config / "organizations.yaml")
    if agents is not None:
        shutil.copy(agents, config / "agents.yaml")
    ok, errors, _ = validate_config_dir(
        str(config), str(_SCHEMAS), workflow_dir=str(tmp_path / "no-workflows")
    )
    return ok, errors


def _fixtures(kind: str) -> list[Path]:
    return sorted((_FIXTURES / kind).glob("*.yaml"))


@pytest.mark.parametrize("fixture", _fixtures("valid"), ids=lambda p: p.stem)
def test_valid_fixture_passes(tmp_path: Path, fixture: Path) -> None:
    ok, errors = _validate(tmp_path, fixture, agents=_FIXTURES / "agents.yaml")
    assert ok, [str(e) for e in errors]


@pytest.mark.parametrize("fixture", _fixtures("invalid"), ids=lambda p: p.stem)
def test_invalid_fixture_fails_for_its_rule_only(tmp_path: Path, fixture: Path) -> None:
    rule = fixture.stem.split("--")[0]
    ok, errors = _validate(tmp_path, fixture, agents=_FIXTURES / "agents.yaml")
    assert not ok
    assert errors, "a refused file must say why"
    rendered = [str(e) for e in errors]
    if rule.startswith("schema-"):
        # The shape is wrong, so the graph checks never ran. One error is at
        # the key at fault, or names it.
        key = rule.removeprefix("schema-")
        assert any(e.path.split(".")[-1] == key or f"'{key}'" in e.message for e in errors), (
            rendered
        )
    else:
        # Every error starts with this rule's name: the fixture breaks one
        # rule, and no other rule misfires on it.
        assert all(e.message.startswith(f"{rule}: ") for e in errors), rendered


def test_fixtures_cover_each_topology_and_rule() -> None:
    valid = {p.stem for p in _fixtures("valid")}
    assert {"hierarchy", "flat", "matrix"} <= valid
    rules = {p.stem.split("--")[0] for p in _fixtures("invalid")}
    # The PR plan's list: a cycle, a second manager in a hierarchy, a matrix
    # edge without scope, `manages` in a flat organization, an agent missing
    # from agents.yaml, a persona in two organizations; D-3's `clearance`,
    # OQ 1's required `authority` and D-9's reserved `general`; and a key
    # given twice, which Go's parser refuses and PyYAML would hide.
    assert {
        "cycle",
        "second-manager",
        "unscoped-matrix-edge",
        "flat-manages",
        "unknown-agent",
        "second-organization",
        "clearance",
        "schema-authority",
        "reserved-scope",
        "duplicate-key",
    } <= rules


def test_missing_agents_yaml_is_one_error_that_says_so(tmp_path: Path) -> None:
    ok, errors = _validate(tmp_path, _FIXTURES / "valid" / "flat.yaml", agents=None)
    assert not ok
    (error,) = errors
    assert error.message.startswith("unknown-agent: agents.yaml ")


def test_second_manager_names_both_managers(tmp_path: Path) -> None:
    ok, errors = _validate(
        tmp_path, _FIXTURES / "invalid" / "second-manager.yaml", agents=_FIXTURES / "agents.yaml"
    )
    assert not ok
    (error,) = errors
    assert "'iron-fox'" in error.message
    assert "'ember-owl'" in error.message
    assert "'nova-sparrow'" in error.message
    assert error.path == "organizations.0.members.1.manages.0"


def test_cycle_names_the_loop(tmp_path: Path) -> None:
    ok, errors = _validate(
        tmp_path, _FIXTURES / "invalid" / "cycle.yaml", agents=_FIXTURES / "agents.yaml"
    )
    assert not ok
    (error,) = errors
    assert "ember-owl → iron-fox → nova-sparrow → ember-owl" in error.message


def test_script_run_checks_the_graph(tmp_path: Path) -> None:
    """`make validate`, CI and `persatrix validate` run validate.py as a
    script, which loads the graph checks another way than the import above."""
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(_FIXTURES / "invalid" / "second-manager.yaml", config / "organizations.yaml")
    shutil.copy(_FIXTURES / "agents.yaml", config / "agents.yaml")
    result = subprocess.run(
        [sys.executable, str(_ROOT / "agents" / "validate.py"), str(config)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "second-manager: " in result.stdout


def test_shipped_file_declares_no_organization(tmp_path: Path) -> None:
    """The no-organization contract: the shipped file is `organizations: []`."""
    ok, errors = _validate(tmp_path, _SHIPPED, agents=_ROOT / "config" / "agents.yaml")
    assert ok, [str(e) for e in errors]
    assert "\norganizations: []\n" in _SHIPPED.read_text(encoding="utf-8")


def test_shipped_example_validates(tmp_path: Path) -> None:
    """The commented example in the shipped file, uncommented, validates
    against the shipped agents, so it cannot rot."""
    lines = _SHIPPED.read_text(encoding="utf-8").splitlines()
    start = lines.index("# organizations:")
    example = [line[2:] if line.startswith("# ") else line[1:] for line in lines[start:]]
    assert all(line.startswith("#") for line in lines[start:])
    uncommented = tmp_path / "example.yaml"
    uncommented.write_text("\n".join(example) + "\n", encoding="utf-8")
    ok, errors = _validate(tmp_path, uncommented, agents=_ROOT / "config" / "agents.yaml")
    assert ok, [str(e) for e in errors]
    assert "clearance" in _SHIPPED.read_text(encoding="utf-8")
