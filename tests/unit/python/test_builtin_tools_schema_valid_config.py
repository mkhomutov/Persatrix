"""
The built-in tools under an agent config that ``make validate`` accepts,
loaded the way the agent server loads one (ISSUE-0150).

An operator turns a tool on in the agent's ``permissions`` block, and the
agent schema (``schemas/agent.schema.json``) says which keys that block may
hold. The permission gate has to read those same keys. For two tools it
did not: ``shell_exec`` asked for a ``shell.exec`` key and ``http_request``
for a ``network.http`` key, the schema allows neither, and so no validated
config could turn either tool on. The tool tests never saw it, because
they hand the gate a permissions dict directly and never meet the schema.

So every test here goes the long way round: it writes one agent config,
checks it with the validator ``make validate`` runs, loads it through
``load_agent`` (which builds the gate from the config's ``permissions``
block), and only then calls the tool.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema  # type: ignore[import-untyped]
import pytest
import yaml

from agents.server_persona import load_agent
from agents.tools import builtin
from agents.tools.permissions import PermissionGate
from agents.tools.tool_list import offered_tools
from agents.validate import validate_config_dir

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCHEMAS_DIR = _REPO_ROOT / "schemas"
_PERMISSIONS_SCHEMA: dict[str, Any] = json.loads(
    (_SCHEMAS_DIR / "agent.schema.json").read_text(encoding="utf-8"),
)["definitions"]["permissions"]

# ISSUE-0129: allowlist the interpreter running the tests by its absolute
# path; a bare `python` is not on PATH on macOS.
_PY = sys.executable
_PY_CMD = shlex.quote(_PY)


@pytest.fixture(autouse=True)
def _restore_builtin_state():
    """``load_agent`` sets the built-in tools' module-level gate, path
    validator and workspace; put the originals back after each test."""
    saved = (builtin.permission_gate, builtin.path_validator, builtin.workspace_root)
    yield
    builtin.permission_gate, builtin.path_validator, builtin.workspace_root = saved


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return workspace


def _load(tmp_path: Path, permissions: dict[str, Any], tools: list[str]) -> None:
    """Write a one-agent config, check that ``make validate``'s validator
    accepts it, then load it the way the agent server does."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    agent = {
        "id": "tool-user",
        "type": "task",
        "name": "Tool User",
        "role": "Uses the built-in tools",
        "model": "test-model",
        "instructions": "Use the tools you are given.",
        "tools": tools,
        "permissions": permissions,
    }
    config_path = config_dir / "agents.yaml"
    config_path.write_text(
        yaml.safe_dump({"schema_version": "0.2", "agents": [agent]}),
        encoding="utf-8",
    )
    ok, errors, checked = validate_config_dir(
        str(config_dir),
        schemas_dir=str(_SCHEMAS_DIR),
        workflow_dir=str(tmp_path / "no-workflows"),
    )
    assert (ok, checked) == (True, 1), [str(e) for e in errors]
    with patch(
        "agents.server_persona.create_provider",
        return_value=(MagicMock(), "test-model"),
    ):
        load_agent("tool-user", str(config_path), str(_workspace(tmp_path)))


def _mock_session() -> AsyncMock:
    resp = AsyncMock()
    resp.status = 200
    resp.text = AsyncMock(return_value="ok")
    resp.headers = {}
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    session.request = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


class TestShellExec:
    async def test_allowlisted_command_runs(self, tmp_path):
        _load(tmp_path, {"shell": {"allowed_commands": [_PY]}}, ["shell_exec"])
        result = await builtin.shell_exec(f"{_PY_CMD} -c \"print('ok')\"")
        assert result.success is True, result.error
        assert result.data["stdout"].strip() == "ok"

    async def test_the_list_still_limits_which_commands_run(self, tmp_path):
        _load(tmp_path, {"shell": {"allowed_commands": [_PY]}}, ["shell_exec"])
        result = await builtin.shell_exec("echo hi")
        assert result.success is False
        assert result.error == "Command not in allowlist: echo"

    @pytest.mark.parametrize(
        "permissions",
        [{}, {"shell": {}}, {"shell": {"allowed_commands": []}}],
        ids=["no-shell-block", "empty-shell-block", "empty-allowlist"],
    )
    async def test_no_allowlist_means_no_shell(self, tmp_path, permissions):
        _load(tmp_path, permissions, ["shell_exec"])
        result = await builtin.shell_exec(f"{_PY_CMD} -c pass")
        assert result.error == "Permission denied: shell:exec"

    async def test_max_execution_seconds_caps_the_timeout(self, tmp_path):
        """The model picks a timeout; the agent's ``max_execution_seconds``
        is the most it gets."""
        _load(
            tmp_path,
            {"shell": {"allowed_commands": [_PY], "max_execution_seconds": 1}},
            ["shell_exec"],
        )
        result = await builtin.shell_exec(
            f"{_PY_CMD} -c \"import time; time.sleep(30)\"", timeout=5,
        )
        assert result.error_type == "TimeoutError"
        assert result.error == "Command timed out after 1s"


class TestHttpRequest:
    async def test_allowlisted_domain_is_requested(self, tmp_path):
        _load(
            tmp_path,
            {"network": {"allow": ["api.example.com"], "deny": ["*"]}},
            ["http_request"],
        )
        session = _mock_session()
        with patch("agents.tools.builtin.aiohttp.ClientSession", return_value=session):
            result = await builtin.http_request("https://api.example.com/v1/items")
        assert result.success is True, result.error
        assert result.data["status"] == 200
        session.request.assert_called_once()

    async def test_the_list_still_limits_which_domains(self, tmp_path):
        _load(
            tmp_path,
            {"network": {"allow": ["api.example.com"], "deny": ["*"]}},
            ["http_request"],
        )
        result = await builtin.http_request("https://evil.example/steal")
        assert result.error == "Domain not in allowlist: evil.example"

    @pytest.mark.parametrize(
        "permissions",
        [{}, {"network": {"deny": ["*"]}}, {"network": {"allow": [], "deny": ["*"]}}],
        ids=["no-network-block", "deny-only", "empty-allow-list"],
    )
    async def test_no_allow_list_means_no_http(self, tmp_path, permissions):
        _load(tmp_path, permissions, ["http_request"])
        result = await builtin.http_request("https://api.example.com/")
        assert result.error == "Permission denied: network:http"


class TestFilesystem:
    """No mismatch here: the gate reads ``filesystem.read`` and
    ``filesystem.write``, which are the schema's own keys. Pinned so the
    three list-scoped tools keep working alike."""

    async def test_write_then_read_inside_the_lists(self, tmp_path):
        inside = str(_workspace(tmp_path) / "**")
        _load(
            tmp_path,
            {"filesystem": {"read": [inside], "write": [inside]}},
            ["file_read", "file_write"],
        )
        target = _workspace(tmp_path) / "note.txt"
        wrote = await builtin.file_write(str(target), "hello")
        assert wrote.success is True, wrote.error
        read = await builtin.file_read(str(target))
        assert read.data == "hello"

    async def test_empty_write_list_means_no_writes(self, tmp_path):
        inside = str(_workspace(tmp_path) / "**")
        _load(
            tmp_path,
            {"filesystem": {"read": [inside], "write": []}},
            ["file_read", "file_write"],
        )
        result = await builtin.file_write(str(_workspace(tmp_path) / "note.txt"), "x")
        assert result.error == "Permission denied: filesystem:write"


class TestShippedConfig:
    """``config/agents.yaml`` passes ``make validate`` in CI. Its
    code-writer lists ``shell_exec`` and allowlists four commands."""

    def test_code_writer_can_use_its_shell_allowlist(self, tmp_path):
        with patch(
            "agents.server_persona.create_provider",
            return_value=(MagicMock(), "test-model"),
        ):
            load_agent("code-writer", str(_REPO_ROOT / "config" / "agents.yaml"), str(tmp_path))
        gate = builtin.permission_gate
        assert gate is not None
        assert gate.check("shell:exec") is True
        assert gate.is_command_allowed(["pytest", "-q"]) is True
        assert gate.shell_time_limit() == 30

    def test_task_agents_granted_http_are_not_offered_it(self, tmp_path):
        """The ``network`` blocks on planner, code-writer and code-reviewer
        are examples. They now grant ``network:http``, but none of the three
        lists ``http_request``, so none is offered it, and a call to it gets
        ``Unknown tool`` (ISSUE-0151)."""
        config_path = _REPO_ROOT / "config" / "agents.yaml"
        agents = yaml.safe_load(config_path.read_text(encoding="utf-8"))["agents"]
        offered_when_granted: dict[str, list[str]] = {}
        for agent in agents:
            if agent.get("type") != "task":
                continue
            with patch(
                "agents.server_persona.create_provider",
                return_value=(MagicMock(), "test-model"),
            ):
                load_agent(agent["id"], str(config_path), str(tmp_path))
            gate = builtin.permission_gate
            assert gate is not None
            if gate.check("network:http"):
                offered = offered_tools(agent["id"], agent)
                offered_when_granted[agent["id"]] = [td.name for td in offered]
        assert sorted(offered_when_granted) == ["code-reviewer", "code-writer", "planner"]
        for names in offered_when_granted.values():
            assert "http_request" not in names, offered_when_granted


def _truthy_sample(spec: dict[str, Any]) -> Any:
    """A value of the property's type that the gate would read as "on"."""
    kind = spec.get("type")
    if not isinstance(kind, str):
        return None
    return {"boolean": True, "array": ["x"], "integer": 1}.get(kind)


def _checked_permissions() -> set[str]:
    """Every ``category:action`` a tool passes to ``gate.check``, read from
    the source so a new tool is covered the day it lands."""
    pattern = re.compile(r'\.check\(\s*"([a-z_]+:[a-z_]+)"')
    return {
        match.group(1)
        for path in (_REPO_ROOT / "agents").rglob("*.py")
        if "tests" not in path.parts
        for match in pattern.finditer(path.read_text(encoding="utf-8"))
    }


def test_every_checked_permission_can_be_granted_by_a_schema_valid_config():
    """ISSUE-0150's class of defect: a tool asks for a permission that no
    schema-valid config can grant. For each permission a tool checks, try
    every key the schema allows in that category, set to a "yes" value of
    its type; at least one of them must grant it."""
    checked = _checked_permissions()
    # Not vacuous: the four built-in tools' permissions must be found.
    assert {"filesystem:read", "filesystem:write", "shell:exec", "network:http"} <= checked
    validator = jsonschema.Draft7Validator(_PERMISSIONS_SCHEMA)
    ungrantable = []
    for permission in sorted(checked):
        category = permission.split(":", 1)[0]
        keys = _PERMISSIONS_SCHEMA["properties"].get(category, {}).get("properties", {})
        blocks = [
            {category: {key: sample}}
            for key, spec in keys.items()
            if (sample := _truthy_sample(spec)) is not None
        ]
        for block in blocks:
            assert not list(validator.iter_errors(block)), block
        if not any(PermissionGate(block).check(permission) for block in blocks):
            ungrantable.append(permission)
    assert ungrantable == []


def test_schema_bounds_on_max_execution_seconds_match_the_code():
    """The schema rejects a limit the tool would not honour: at least one
    second, and no more than the ceiling ``shell_exec`` never exceeds."""
    spec = _PERMISSIONS_SCHEMA["properties"]["shell"]["properties"]["max_execution_seconds"]
    assert spec["minimum"] == 1
    assert spec["maximum"] == builtin.MAX_TIMEOUT_SECONDS
