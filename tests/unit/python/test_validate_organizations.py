"""Tests for ``config/organizations.yaml`` validation (RFC 0012 PR 1).

``make validate`` checks the file's shape against
``schemas/organization.schema.json`` and then its graph with
``agents/org_validate.py``. The fixtures under
``tests/fixtures/organizations/`` are shared with the Go loader (RFC 0012
PR 2); their README says how a file name names the rule it breaks.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agents.org_validate import _find_loop
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
        assert all(e.path.split(".")[-1] == key or f"'{key}'" in e.message for e in errors), (
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
    # And every rule a message can name has a fixture, so PR 2 loses none.
    assert _rule_names() <= rules, sorted(_rule_names() - rules)


def _rule_names() -> set[str]:
    """Every rule a refusal can name: those `validate.py` and
    `org_validate.py` write at the start of a message, and the schema's
    `x-refusal` names."""
    names: set[str] = set()
    for module in ("validate.py", "org_validate.py"):
        source = (_ROOT / "agents" / module).read_text(encoding="utf-8")
        names.update(re.findall(r'"([a-z]+(?:-[a-z]+)*): ', source))
    schema = json.loads((_SCHEMAS / "organization.schema.json").read_text(encoding="utf-8"))
    names.update(refusal.split(":")[0] for refusal in _refusals(schema))
    return names


def _refusals(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "x-refusal":
                yield value
            else:
                yield from _refusals(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refusals(value)


def test_missing_agents_yaml_is_one_error_that_says_so(tmp_path: Path) -> None:
    ok, errors = _validate(tmp_path, _FIXTURES / "valid" / "flat.yaml", agents=None)
    assert not ok
    (error,) = errors
    assert error.message.startswith("unknown-agent: agents.yaml ")


@pytest.mark.parametrize(
    "text",
    ["", "# No agents yet.\n", 'schema_version: "0.2"\n', 'schema_version: "0.2"\nagents: []\n'],
    ids=["empty", "comment-only", "no-agents-key", "no-agents"],
)
def test_agents_yaml_that_lists_no_agents_is_one_error_that_says_so(
    tmp_path: Path, text: str
) -> None:
    agents = tmp_path / "agents.yaml"
    agents.write_text(text, encoding="utf-8")
    ok, errors = _validate(tmp_path, _FIXTURES / "valid" / "flat.yaml", agents=agents)
    assert not ok
    (error,) = [e for e in errors if e.file.endswith("organizations.yaml")]
    assert error.message.startswith(
        "unknown-agent: agents.yaml is missing, unreadable or lists no agents"
    ), error.message


def test_no_organization_needs_no_agents_yaml(tmp_path: Path) -> None:
    """The shipped `organizations: []` names no member, so it validates
    without agents.yaml, as v0.3.16's placeholder did."""
    ok, errors = _validate(tmp_path, _FIXTURES / "valid" / "empty.yaml", agents=None)
    assert ok, [str(e) for e in errors]


@pytest.mark.parametrize(
    "text",
    ["", "# Every line commented out.\n", "null\n", "~\n", "---\n"],
    ids=["empty", "comment-only", "null", "tilde", "bare-document"],
)
def test_file_with_nothing_in_it_is_refused(tmp_path: Path, text: str) -> None:
    """A file with nothing in it has no `organizations` list, which the
    schema requires: `organizations: []` is how a file declares no
    organization. Go's typed decode reads each of these as a file with no
    keys, so PR 2's loader must refuse them too."""
    organizations = tmp_path / "organizations.yaml"
    organizations.write_text(text, encoding="utf-8")
    ok, errors = _validate(tmp_path, organizations, agents=None)
    assert not ok
    (error,) = errors
    assert error.message == "'organizations' is a required property", error.message


def test_other_config_files_may_still_be_empty(tmp_path: Path) -> None:
    """Only organizations.yaml must say what it declares. An empty or
    comment-only file of another kind still passes, as before."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "agents.yaml").write_text("", encoding="utf-8")
    (config / "ui.yaml").write_text("# Nothing set yet.\n", encoding="utf-8")
    ok, errors, checked = validate_config_dir(
        str(config), str(_SCHEMAS), workflow_dir=str(tmp_path / "no-workflows")
    )
    assert ok, [str(e) for e in errors]
    assert checked == 2


def test_cycle_search_stays_fast_on_a_long_chain() -> None:
    """The schema sets no limit on how many members an organization has.
    A chain of 100 000 managers that loops back to its start is found and
    named in a fraction of a second. A search that rescans the path at
    every step slows with the square of the chain, over 30 s here, so the
    5 s bound catches it."""
    n = 100_000
    reports = {f"m{i}": [f"m{(i + 1) % n}"] for i in range(n)}
    start = time.perf_counter()
    loop = _find_loop(reports)
    elapsed = time.perf_counter() - start
    assert loop == [*reports, "m0"]
    assert elapsed < 5, f"{elapsed:.1f} s"


def test_agent_with_an_empty_type_is_read_as_a_task_agent(tmp_path: Path) -> None:
    """agents.yaml refuses a `type:` with no value. The graph check reads it
    as the default, `task`, as Go reads an empty value, so the member is not
    a persona; it is not missing from agents.yaml."""
    text = (_FIXTURES / "agents.yaml").read_text(encoding="utf-8")
    agents = tmp_path / "agents.yaml"
    agents.write_text(
        text.replace("  - id: iron-fox\n    type: persona\n", "  - id: iron-fox\n    type:\n"),
        encoding="utf-8",
    )
    ok, errors = _validate(tmp_path, _FIXTURES / "valid" / "flat.yaml", agents=agents)
    assert not ok
    graph = [e.message for e in errors if e.file.endswith("organizations.yaml")]
    assert graph == [
        "not-a-persona: 'iron-fox' is a task agent; only a persona joins an organization"
    ], graph


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


@pytest.mark.parametrize("flags", [[], ["-I"]], ids=["plain", "isolated"])
def test_script_run_checks_the_graph(tmp_path: Path, flags: list[str]) -> None:
    """`make validate`, CI and `persatrix validate` run validate.py as a
    script, which loads the graph checks another way than the import above.
    Under `-I` (as under `-P` or PYTHONSAFEPATH) the script's own folder is
    not on the import path."""
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(_FIXTURES / "invalid" / "second-manager.yaml", config / "organizations.yaml")
    shutil.copy(_FIXTURES / "agents.yaml", config / "agents.yaml")
    result = subprocess.run(
        [sys.executable, *flags, str(_ROOT / "agents" / "validate.py"), str(config)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "second-manager: " in result.stdout


def test_script_prints_a_cycle_through_a_cp1252_pipe(tmp_path: Path) -> None:
    """On Windows a piped stdout uses the ANSI code page, which has no `→`;
    `persatrix validate` reads the script's output as UTF-8."""
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(_FIXTURES / "invalid" / "cycle.yaml", config / "organizations.yaml")
    shutil.copy(_FIXTURES / "agents.yaml", config / "agents.yaml")
    result = subprocess.run(
        [sys.executable, str(_ROOT / "agents" / "validate.py"), str(config)],
        cwd=_ROOT,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
    )
    stdout = result.stdout.decode("utf-8", errors="replace")
    assert result.returncode == 1, stdout + result.stderr.decode("utf-8", errors="replace")
    assert "cycle: ember-owl → iron-fox → nova-sparrow → ember-owl" in stdout, stdout


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
