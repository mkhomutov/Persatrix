"""
Tests for load_agent(), _resolve_agent_type(), tool definition filtering,
permission wiring, and duplicate agent ID detection.

All tests use mock providers — no real API calls.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.server import load_agent
from agents.server_persona import _resolve_agent_type
from agents.task_agent import TaskAgent
from agents.tools import builtin

# ─── Helpers ─────────────────────────────────────────────────


class _StubAgent(BaseAgent):
    """Minimal agent for tool-filtering tests."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="stub result")


def _write_agent_config(tmp: Path, agents: list[dict]) -> str:
    """Write a temporary agents.yaml and return its path."""
    config_path = tmp / "agents.yaml"
    config_path.write_text(
        yaml.dump({"schema_version": "0.1", "agents": agents}),
        encoding="utf-8",
    )
    return str(config_path)


# ─── load_agent Tests ────────────────────────────────────────


class TestLoadAgent:
    """Tests for load_agent()."""

    @patch("agents.server_persona.create_provider")
    def test_load_planner(self, mock_create):
        mock_create.return_value = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {
                    "id": "planner",
                    "name": "Planner",
                    "role": "Plans things",
                    "model": "test-model",
                    "type": "task",
                    "capabilities": ["planning"],
                    "tools": [],
                    "permissions": {},
                },
            ])
            agent = load_agent("planner", config_path, tmp)
            assert isinstance(agent, TaskAgent)
            assert agent.agent_id == "planner"

    @patch("agents.server_persona.create_provider")
    def test_load_coder(self, mock_create):
        mock_create.return_value = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {
                    "id": "code-writer",
                    "name": "Code Writer",
                    "role": "Writes code",
                    "model": "test-model",
                    "type": "task",
                    "capabilities": ["code_generation", "code_review"],
                    "tools": ["file_read", "file_write"],
                    "permissions": {},
                },
            ])
            agent = load_agent("code-writer", config_path, tmp)
            assert isinstance(agent, TaskAgent)

    @patch("agents.server_persona.create_provider")
    def test_load_reviewer(self, mock_create):
        mock_create.return_value = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {
                    "id": "code-reviewer",
                    "name": "Code Reviewer",
                    "role": "Reviews code",
                    "model": "test-model",
                    "type": "task",
                    "capabilities": ["code_review", "security_audit"],
                    "tools": ["file_read"],
                    "permissions": {},
                },
            ])
            agent = load_agent("code-reviewer", config_path, tmp)
            assert isinstance(agent, TaskAgent)

    def test_missing_config_file(self):
        with pytest.raises(SystemExit, match="not found"):
            load_agent("planner", "/nonexistent/agents.yaml", "/workspace")

    def test_missing_agent_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {"id": "other", "capabilities": ["planning"], "permissions": {}},
            ])
            with pytest.raises(SystemExit, match="not found"):
                load_agent("planner", config_path, tmp)

    def test_bad_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "agents.yaml"
            bad_path.write_text(": : : invalid yaml {{", encoding="utf-8")
            with pytest.raises(SystemExit, match="Invalid YAML"):
                load_agent("planner", str(bad_path), tmp)

    @patch("agents.server_persona.create_provider")
    def test_unknown_type_raises_system_exit(self, mock_create):
        """Unknown agent type in config raises SystemExit (not ValueError)."""
        mock_create.return_value = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {
                    "id": "mystery",
                    "type": "banana",
                    "model": "test-model",
                    "permissions": {},
                },
            ])
            with pytest.raises(SystemExit, match="Unknown agent type"):
                load_agent("mystery", config_path, tmp)

    def test_agent_entry_missing_id_field(self):
        """F-03: agent config entry without 'id' gives a clear SystemExit."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {"name": "No ID Agent", "capabilities": ["planning"]},
            ])
            with pytest.raises(SystemExit, match="missing required 'id' field"):
                load_agent("planner", config_path, tmp)

    def test_invalid_agent_id_format(self):
        """MF-02: agent IDs must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {"id": "Valid", "capabilities": ["planning"], "permissions": {}},
            ])
            with pytest.raises(SystemExit, match="Invalid agent ID"):
                load_agent("UPPER_CASE", config_path, tmp)

    def test_single_char_agent_id_accepted(self):
        """F-6a-2: single character ID is now valid per updated regex."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {"id": "a", "capabilities": ["planning"], "permissions": {}},
            ])
            # 'a' is a valid ID now, so it should fail for missing model, not ID
            with pytest.raises(SystemExit, match="missing required 'model' field"):
                load_agent("a", config_path, tmp)

    def test_trailing_hyphen_agent_id_rejected(self):
        """Agent ID ending with hyphen fails the regex."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {"id": "agent-", "capabilities": ["planning"], "permissions": {}},
            ])
            with pytest.raises(SystemExit, match="Invalid agent ID"):
                load_agent("agent-", config_path, tmp)

    @patch("agents.server_persona.create_provider")
    def test_missing_model_field(self, mock_create):
        """SF-08: missing 'model' key gives a clear SystemExit at startup."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {
                    "id": "no-model",
                    "name": "No Model Agent",
                    "capabilities": ["planning"],
                    "permissions": {},
                },
            ])
            with pytest.raises(SystemExit, match="missing required 'model' field"):
                load_agent("no-model", config_path, tmp)

    def test_agents_key_not_a_list(self):
        """S-02: 'agents' value that is not a list gives a clear SystemExit."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "agents.yaml"
            config_path.write_text(
                "schema_version: '0.1'\nagents: not_a_list\n",
                encoding="utf-8",
            )
            with pytest.raises(SystemExit, match="must be a list"):
                load_agent("planner", str(config_path), tmp)


# ─── _resolve_agent_type Tests ───────────────────────────────


class TestResolveAgentType:
    """Tests for _resolve_agent_type() — type-based dispatch (RFC 0005 PR 1a)."""

    def test_type_task_explicit(self):
        assert _resolve_agent_type({"id": "x", "type": "task"}) == "task"

    def test_type_default_is_task(self):
        """Agents without a type field default to task (backward compat)."""
        assert _resolve_agent_type({"id": "x"}) == "task"

    def test_type_persona(self):
        """PersonaAgent type resolves to 'persona' string."""
        assert _resolve_agent_type({"id": "x", "type": "persona"}) == "persona"

    def test_unknown_type_raises_system_exit(self):
        """Unknown type values must produce a clean operator-facing SystemExit."""
        with pytest.raises(SystemExit, match="Unknown agent type"):
            _resolve_agent_type({"id": "x", "type": "banana"})


# ─── Tool Definition Filtering Tests ─────────────────────────


class TestToolDefinitionFiltering:
    """S-15: direct test for _build_tool_definitions() filtering by agent config."""

    def test_filters_to_configured_tools(self):
        """Agent with tools=['file_read'] only sees file_read, not other tools."""
        from agents.tools.registry import ToolResult, clear_registry, tool

        clear_registry()

        @tool(name="file_read", description="Read a file")
        async def file_read(path: str) -> ToolResult:
            return ToolResult(success=True, data="content")

        @tool(name="shell_exec", description="Run command")
        async def shell_exec(command: str) -> ToolResult:
            return ToolResult(success=True, data="output")

        agent = _StubAgent(
            agent_id="test-agent",
            config={"tools": ["file_read"]},
        )
        defs = agent._build_tool_definitions()

        assert len(defs) == 1
        assert defs[0]["name"] == "file_read"

        clear_registry()

    def test_empty_tools_list_returns_no_tools(self):
        """Agent with tools=[] (e.g. PlannerAgent) sees no tools."""
        from agents.tools.registry import ToolResult, clear_registry, tool

        clear_registry()

        @tool(name="file_read", description="Read a file")
        async def file_read(path: str) -> ToolResult:
            return ToolResult(success=True, data="content")

        agent = _StubAgent(
            agent_id="test-agent",
            config={"tools": []},
        )
        defs = agent._build_tool_definitions()
        assert defs == []

        clear_registry()

    def test_no_tools_key_returns_no_tools(self):
        """Agent without 'tools' key in config sees no tools."""
        agent = _StubAgent(agent_id="test-agent", config={})
        defs = agent._build_tool_definitions()
        assert defs == []


# ─── Permission Wiring Tests ──────────────────────────────────


class TestPermissionWiring:
    """S-16: verify load_agent wires permission_gate and path_validator."""

    @patch("agents.server_persona.create_provider")
    def test_permissions_wired_after_load(self, mock_create):
        """After load_agent(), builtin.permission_gate and builtin.path_validator are set."""
        mock_create.return_value = MagicMock()

        original_gate = builtin.permission_gate
        original_validator = builtin.path_validator
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                config_path = _write_agent_config(tmp_path, [
                    {
                        "id": "code-writer",
                        "name": "Code Writer",
                        "model": "test-model",
                        "capabilities": ["code_generation"],
                        "tools": ["file_read"],
                        "permissions": {
                            "filesystem": {
                                "read": ["/workspace/**"],
                                "write": ["/workspace/**"],
                                "deny": ["/etc/**"],
                            },
                            "network": {
                                "allow": ["api.example.com"],
                            },
                        },
                    },
                ])
                load_agent("code-writer", config_path, tmp)

                assert builtin.permission_gate is not None
                assert builtin.path_validator is not None
        finally:
            builtin.permission_gate = original_gate
            builtin.path_validator = original_validator


# ─── Duplicate Agent ID Tests ─────────────────────────────────
# instructions_file load tests live in
# test_server_load_agent_instructions_file.py (split for the 500-line policy).


class TestDuplicateAgentId:
    """S-17: duplicate agent IDs in config are detected."""

    def test_duplicate_agent_id_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {
                    "id": "code-writer",
                    "name": "Code Writer",
                    "model": "test-model",
                    "capabilities": ["code_generation"],
                    "permissions": {},
                },
                {
                    "id": "code-writer",
                    "name": "Code Writer Dupe",
                    "model": "test-model",
                    "capabilities": ["code_generation"],
                    "permissions": {},
                },
            ])
            with pytest.raises(SystemExit, match="Duplicate agent ID"):
                load_agent("code-writer", config_path, tmp)
