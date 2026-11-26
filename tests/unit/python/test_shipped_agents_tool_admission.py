"""No shipped agent loses a tool to the declared-permission check (ISSUE-0214).

The check refuses a call to a tool whose declared permission the agent's
config does not grant. Each of the nine built-in tools already refused such
a call from its own body, so for the shipped agents the new check may only
refuse what was refused before, with the same words.

This file loads every agent in ``config/agents.yaml`` the way the agent
server does and pins, for each, the tools it is offered and the ones its
config does not grant. It reads only what exists on both sides of the
change, so it passes unchanged before and after it: the table is the
"before", and its passing is the "after".

The unit suite empties the tool registry before each test, so the fixture
loads the built-in tools again, and the table lists whole offered sets:
an empty registry cannot pass it.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agents.base import BaseAgent
from agents.llm_client import ToolCall
from agents.server_persona import load_agent
from agents.tools import builtin
from agents.tools.permissions import PermissionGate
from agents.tools.recall import wire_recall_tools
from agents.tools.registry import ToolDefinition
from agents.tools.tool_list import offered_tools

_CONFIG = Path(__file__).resolve().parents[3] / "config" / "agents.yaml"

_NOTES: dict[str, str | None] = {
    "store_note": None, "recall_notes": None, "update_note": None, "delete_note": None,
}

# For each shipped agent: every tool it is offered, with the declared
# permission its config does not grant (None when it grants them all).
# Three personas list ``file_read`` with no ``filesystem`` block, and two
# have no ``channels.recall``; ISSUE-0147 records that gap.
_SHIPPED: dict[str, dict[str, str | None]] = {
    "planner": {},
    "code-writer": {"file_read": None, "file_write": None},
    "code-reviewer": {"file_read": None},
    "ember-owl": {
        "file_read": "filesystem:read", **_NOTES, "recall_channel_messages": None,
    },
    "iron-fox": {
        "file_read": "filesystem:read", **_NOTES,
        "recall_channel_messages": "channels:recall",
    },
    "nova-sparrow": {
        "file_read": "filesystem:read", **_NOTES,
        "recall_channel_messages": "channels:recall",
    },
}

# Arguments each refused tool accepts, so the call reaches the permission
# check and not the argument check.
_ARGUMENTS: dict[str, dict[str, Any]] = {
    "file_read": {"path": "/workspace/README.md"},
    "recall_channel_messages": {"query": "launch"},
}


@pytest.fixture(autouse=True)
def _built_in_tools() -> Iterator[None]:
    """Register the built-in tools, and put their module state back after."""
    saved = (builtin.permission_gate, builtin.path_validator, builtin.workspace_root)
    importlib.reload(builtin)
    yield
    builtin.permission_gate, builtin.path_validator, builtin.workspace_root = saved


def _shipped_ids() -> list[str]:
    agents = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))["agents"]
    return [agent["id"] for agent in agents]


def _load(agent_id: str, workspace: Path) -> BaseAgent:
    """Load one shipped agent as the server does, the recall tool included."""
    with patch(
        "agents.server_persona.create_provider", return_value=(MagicMock(), "test-model"),
    ):
        agent = load_agent(agent_id, str(_CONFIG), str(workspace))
    wire_recall_tools({agent_id: agent}, MagicMock(), "http://orchestrator:8080")
    return agent


def _runnable(agent: BaseAgent) -> dict[str, ToolDefinition]:
    """The definition the agent runs under each name it offers."""
    tools = {td.name: td for td in offered_tools(agent.agent_id, agent.config)}
    tools.update({td.name: td for td in getattr(agent, "_memory_tools", [])})
    return tools


def _not_granted(agent: BaseAgent, definition: ToolDefinition) -> str | None:
    gate = PermissionGate(agent.config.get("permissions"))
    return next((p for p in definition.permissions if not gate.check(p)), None)


def test_the_table_covers_every_shipped_agent() -> None:
    assert _shipped_ids() == list(_SHIPPED)


@pytest.mark.parametrize("agent_id", list(_SHIPPED))
def test_each_agent_is_offered_the_same_tools(agent_id: str, tmp_path: Path) -> None:
    agent = _load(agent_id, tmp_path)

    offered = [definition["name"] for definition in agent._build_tool_definitions()]

    assert sorted(offered) == sorted(_SHIPPED[agent_id])
    assert len(offered) == len(set(offered))


@pytest.mark.parametrize("agent_id", list(_SHIPPED))
def test_each_agent_is_granted_the_same_tools(agent_id: str, tmp_path: Path) -> None:
    agent = _load(agent_id, tmp_path)

    granted = {name: _not_granted(agent, td) for name, td in _runnable(agent).items()}

    assert granted == _SHIPPED[agent_id]


@pytest.mark.parametrize(
    ("agent_id", "name"),
    [(agent_id, name) for agent_id, tools in _SHIPPED.items()
     for name, missing in tools.items() if missing is not None],
)
async def test_a_refused_call_reads_as_the_tools_own_refusal(
    agent_id: str, name: str, tmp_path: Path,
) -> None:
    # The only calls the check refuses for a shipped agent are ones the
    # tool's own body refuses, and the model is told the same thing.
    agent = _load(agent_id, tmp_path)
    definition = _runnable(agent)[name]
    assert definition.func is not None
    arguments = _ARGUMENTS[name]

    from_the_body = await definition.func(**arguments)
    [from_the_round] = await agent._execute_tools(
        [ToolCall(id="c1", name=name, input=arguments)],
    )

    expected = f"Permission denied: {_SHIPPED[agent_id][name]}"
    assert (from_the_body.success, from_the_body.error) == (False, expected)
    assert from_the_body.error_type is None and from_the_body.data is None
    assert (from_the_round.content, from_the_round.is_error) == (expected, True)
