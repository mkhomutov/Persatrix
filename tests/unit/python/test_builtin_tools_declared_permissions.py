"""The declared-permission check on the nine built-in tools (ISSUE-0214).

Each built-in tool asks the permission gate in its own body, and goes on
to ask what a declared permission cannot say: which path, which command,
which domain. The new check, in
:func:`agents.tools.tool_list.admit_call`, comes before the body and
replaces none of it. Three things are pinned here:

- the check refuses a built-in tool on the *agent's own* config, even when
  the gate the tool's body reads would grant the call, and tells the model
  what the body would have told it;
- with the agent's config granting, the body's own check still decides;
- the path rules still decide after both.

The model is mocked at the ``LLMClient`` boundary; no network: the one HTTP
call that runs is answered by a mocked session.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import ToolCall
from agents.memory.episodic import EpisodicMemory
from agents.task_agent import TaskAgent
from agents.tools import builtin
from agents.tools.memory_tools import create_memory_tools
from agents.tools.permissions import PermissionGate
from agents.tools.recall import create_recall_tool
from agents.tools.registry import clear_registry, get_tool
from agents.tools.sandbox import PathValidator

from ._http_response_fakes import give_body

_NOTE_ID = "00000000-0000-4000-8000-000000000000"

# Each built-in tool, the permission it declares, and arguments it accepts.
_NINE: list[tuple[str, str, dict[str, Any]]] = [
    ("file_read", "filesystem:read", {"path": "{ws}/a.txt"}),
    ("file_write", "filesystem:write", {"path": "{ws}/b.txt", "content": "x"}),
    ("shell_exec", "shell:exec", {"command": "echo hi"}),
    ("http_request", "network:http", {"url": "https://api.example.com/x"}),
    ("store_note", "memory:write", {"topic": "t", "content": "c"}),
    ("recall_notes", "memory:read", {}),
    ("update_note", "memory:write", {"note_id": _NOTE_ID, "content": "c"}),
    ("delete_note", "memory:write", {"note_id": _NOTE_ID}),
    ("recall_channel_messages", "channels:recall", {"query": "launch"}),
]
_NAMES = [name for name, _, _ in _NINE]


def _everything(workspace: Path) -> dict[str, Any]:
    """A ``permissions`` block that grants all seven permissions."""
    return {
        "filesystem": {"read": [str(workspace / "**")], "write": [str(workspace / "**")]},
        "shell": {"allowed_commands": ["echo"]},
        "network": {"allow": ["api.example.com"], "deny": ["*"]},
        "memory": {"read": True, "write": True},
        "channels": {"recall": True},
    }


class _Recall:
    async def recall(self, **_: Any) -> list[dict[str, Any]]:
        return []


def _wire(workspace: Path, memory: EpisodicMemory, permissions: dict[str, Any]) -> None:
    """Give every built-in tool's own body a gate built from *permissions*."""
    builtin.permission_gate = PermissionGate(permissions)
    fs = permissions.get("filesystem", {})
    builtin.path_validator = PathValidator(
        allow_read=fs.get("read", []), allow_write=fs.get("write", []),
    )
    builtin.workspace_root = workspace
    create_memory_tools(memory, PermissionGate(permissions))
    create_recall_tool(_Recall(), PermissionGate(permissions), agent_id="code-writer")


@pytest.fixture(autouse=True)
def _tools(tmp_path: Path) -> Iterator[None]:
    clear_registry()
    importlib.reload(builtin)
    saved = (builtin.permission_gate, builtin.path_validator, builtin.workspace_root)
    (tmp_path / "a.txt").write_text("hello")
    yield
    builtin.permission_gate, builtin.path_validator, builtin.workspace_root = saved
    clear_registry()


def _ok_http_session() -> MagicMock:
    resp = AsyncMock(status=200, headers={})
    give_body(resp, "ok")
    resp.__aenter__ = AsyncMock(return_value=resp)
    session = AsyncMock()
    session.request = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    return MagicMock(return_value=session)


async def _run(
    permissions: dict[str, Any] | None, name: str, arguments: dict[str, Any], workspace: Path,
) -> tuple[str, bool]:
    """Run one call on a task agent that lists all nine tools."""
    config: dict[str, Any] = {"tools": _NAMES}
    if permissions is not None:
        config["permissions"] = permissions
    agent = TaskAgent(agent_id="code-writer", config=config)
    arguments = {
        key: value.format(ws=workspace) if isinstance(value, str) else value
        for key, value in arguments.items()
    }
    with patch("agents.tools.builtin.aiohttp.ClientSession", _ok_http_session()):
        [result] = await agent._execute_tools([ToolCall(id="c1", name=name, input=arguments)])
    return result.content, result.is_error


@pytest.mark.parametrize(("name", "permission", "arguments"), _NINE)
async def test_the_agents_own_config_decides_before_the_tool_runs(
    name: str, permission: str, arguments: dict[str, Any],
    tmp_path: Path, memory: EpisodicMemory,
) -> None:
    # The gate each tool's body reads grants everything, so only the new
    # check can refuse: before it, the tool ran for this agent.
    _wire(tmp_path, memory, _everything(tmp_path))

    refused = await _run({}, name, arguments, tmp_path)

    assert refused == (f"Permission denied: {permission}", True)
    assert not (tmp_path / "b.txt").exists()
    assert await memory.count_notes() == 0
    # Positive control: the same call is let through once the config grants it.
    content, _ = await _run(_everything(tmp_path), name, arguments, tmp_path)
    assert not content.startswith("Permission denied")


@pytest.mark.parametrize(("name", "permission", "arguments"), _NINE)
async def test_the_refusal_reads_as_the_tools_own(
    name: str, permission: str, arguments: dict[str, Any],
    tmp_path: Path, memory: EpisodicMemory,
) -> None:
    # Body and check both deny here, as they do for an agent the server
    # loaded: the model is told what the body alone told it before.
    _wire(tmp_path, memory, {})
    definition = get_tool(name)
    assert definition is not None and definition.func is not None
    filled = {
        key: value.format(ws=tmp_path) if isinstance(value, str) else value
        for key, value in arguments.items()
    }
    from_the_body = await definition.func(**filled)

    from_the_round = await _run({}, name, arguments, tmp_path)

    assert (from_the_body.success, from_the_body.error_type) == (False, None)
    assert from_the_body.error == f"Permission denied: {permission}"
    assert from_the_round == (from_the_body.error, True)


@pytest.mark.parametrize(("name", "permission", "arguments"), _NINE)
async def test_the_tools_own_check_is_still_there(
    name: str, permission: str, arguments: dict[str, Any],
    tmp_path: Path, memory: EpisodicMemory,
) -> None:
    # The agent's config grants everything, so the new check lets the call
    # through; the gate the body reads grants nothing, and the body refuses.
    _wire(tmp_path, memory, {})

    refused = await _run(_everything(tmp_path), name, arguments, tmp_path)

    assert refused == (f"Permission denied: {permission}", True)


async def test_the_path_rules_still_decide_after_the_permission(
    tmp_path: Path, memory: EpisodicMemory,
) -> None:
    granted = _everything(tmp_path)
    _wire(tmp_path, memory, granted)
    outside = {"path": "/etc/hostname"}

    content, is_error = await _run(granted, "file_read", outside, tmp_path)

    assert is_error is True
    assert content.startswith("Tool error (PermissionError)")
    # Control: a path the rules cover is read.
    read, read_failed = await _run(granted, "file_read", {"path": "{ws}/a.txt"}, tmp_path)
    assert read_failed is False
    assert "\nhello\n" in read
