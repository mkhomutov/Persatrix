"""
Tests for Docker agent sandbox, timeout, and service discovery fixes.

Covers:
- PathValidator allowing workspace root alongside glob children
- Tool registry JSON Schema format (properties/required structure)
- TaskAgent workspace root injection into LLM system prompt
- AgentServer advertise-address for Docker service discovery
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import TaskInput, TaskStatus
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.server import AgentServer
from agents.task_agent import TaskAgent
from agents.tools import builtin
from agents.tools.registry import ToolResult, clear_registry, get_tool, tool
from agents.tools.sandbox import PathValidator


# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture()
def _workspace_root(tmp_path, monkeypatch):
    """Set builtin.workspace_root for the duration of a test."""
    monkeypatch.setattr(builtin, "workspace_root", tmp_path)
    yield tmp_path
    monkeypatch.setattr(builtin, "workspace_root", None)


def _make_client(
    responses: list[LLMResponse] | None = None,
) -> LLMClient:
    mock_provider = AsyncMock()
    if responses:
        mock_provider.create_message = AsyncMock(side_effect=responses)
    else:
        mock_provider.create_message = AsyncMock(
            return_value=LLMResponse(text="default response"),
        )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: [
            *msgs,
            {"role": "assistant", "content": "tool round"},
            {"role": "user", "content": "tool results"},
        ],
    )
    return LLMClient(mock_provider)


def _task(payload: str = "do something") -> TaskInput:
    return TaskInput(task_id="t1", workflow_id="w1", payload=payload)


# ─── Sandbox: workspace root matching ───────────────────────


class TestWorkspaceRootAccess:
    """Validates that /workspace itself is accessible alongside /workspace/**."""

    def test_glob_star_star_does_not_match_root(self, tmp_path):
        """fnmatchcase('/workspace', '/workspace/**') is False — this is the bug."""
        validator = PathValidator(
            allow_read=[str(tmp_path / "**")],
        )
        with pytest.raises(PermissionError, match="not in read allow list"):
            validator.validate(str(tmp_path), mode="read")

    def test_explicit_root_plus_glob_allows_both(self, tmp_path):
        """Adding the root itself alongside /** fixes access to both."""
        target_file = tmp_path / "src" / "hello.py"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("print('hi')")

        validator = PathValidator(
            allow_read=[str(tmp_path), str(tmp_path / "**")],
        )
        # Root directory itself is now allowed
        result = validator.validate(str(tmp_path), mode="read")
        assert result == tmp_path.resolve()

        # Children are still allowed via **
        result = validator.validate(str(target_file), mode="read")
        assert result == target_file.resolve()

    def test_write_root_plus_glob(self, tmp_path):
        """Write mode also benefits from explicit root entry."""
        validator = PathValidator(
            allow_write=[str(tmp_path), str(tmp_path / "src" / "**")],
        )
        # Root directory writable
        result = validator.validate(str(tmp_path), mode="write")
        assert result == tmp_path.resolve()

        # Subdirectory writable via glob
        result = validator.validate(str(tmp_path / "src" / "file.py"), mode="write")
        assert result == (tmp_path / "src" / "file.py").resolve()

    def test_deny_list_still_wins_over_root(self, tmp_path):
        """Deny-list precedence is preserved even with explicit root allow."""
        validator = PathValidator(
            allow_read=[str(tmp_path), str(tmp_path / "**")],
            deny=[str(tmp_path / ".env")],
        )
        with pytest.raises(PermissionError, match="blocked by security policy"):
            validator.validate(str(tmp_path / ".env"), mode="read")


# ─── Tool registry: JSON Schema format ──────────────────────


class TestToolSchemaFormat:
    """Validates that @tool produces valid JSON Schema for LLM tool_use."""

    def test_schema_is_json_schema_object(self):
        @tool(name="demo")
        async def demo(path: str, count: int) -> ToolResult:
            return ToolResult(success=True)

        defn = get_tool("demo")
        assert defn is not None
        assert defn.parameters["type"] == "object"
        assert "properties" in defn.parameters

    def test_properties_contain_types(self):
        @tool(name="typed")
        async def typed(a: str, b: int, c: float, d: bool) -> ToolResult:
            return ToolResult(success=True)

        defn = get_tool("typed")
        props = defn.parameters["properties"]
        assert props["a"]["type"] == "string"
        assert props["b"]["type"] == "integer"
        assert props["c"]["type"] == "number"
        assert props["d"]["type"] == "boolean"

    def test_required_list_for_mandatory_params(self):
        @tool(name="mixed")
        async def mixed(required: str, optional: str = "default") -> ToolResult:
            return ToolResult(success=True)

        defn = get_tool("mixed")
        assert "required" in defn.parameters
        assert "required" in defn.parameters["required"]
        assert "optional" not in defn.parameters["required"]

    def test_no_required_key_when_all_optional(self):
        @tool(name="allopt")
        async def allopt(x: str = "a", y: int = 0) -> ToolResult:
            return ToolResult(success=True)

        defn = get_tool("allopt")
        assert "required" not in defn.parameters

    def test_all_required_params_listed(self):
        @tool(name="allreq")
        async def allreq(a: str, b: int) -> ToolResult:
            return ToolResult(success=True)

        defn = get_tool("allreq")
        assert set(defn.parameters["required"]) == {"a", "b"}



# ─── TaskAgent: workspace root in system prompt ─────────────


class TestWorkspaceRootInjection:
    """Validates workspace root is injected into LLM system prompt."""

    async def test_workspace_root_injected_when_tools_configured(
        self, _workspace_root,
    ):
        config = {
            "model": "test-model",
            "instructions": "Write code.",
            "tools": ["file_write"],
        }
        client = _make_client()
        agent = TaskAgent(agent_id="code-writer", config=config, llm_client=client)
        await agent.handle(_task())

        call_kwargs = client._provider.create_message.call_args[1]
        assert f"Workspace root: {_workspace_root}" in call_kwargs["system"]
        assert "absolute paths" in call_kwargs["system"]

    async def test_workspace_root_not_injected_without_tools(
        self, _workspace_root,
    ):
        config = {
            "model": "test-model",
            "instructions": "Plan the work.",
        }
        client = _make_client()
        agent = TaskAgent(agent_id="planner", config=config, llm_client=client)
        await agent.handle(_task())

        call_kwargs = client._provider.create_message.call_args[1]
        assert "Workspace root" not in call_kwargs["system"]

    async def test_workspace_root_not_injected_when_none(self):
        """When workspace_root is None (no --workspace flag), skip injection."""
        original = builtin.workspace_root
        builtin.workspace_root = None
        try:
            config = {
                "model": "test-model",
                "tools": ["file_read"],
            }
            client = _make_client()
            agent = TaskAgent(
                agent_id="code-writer", config=config, llm_client=client,
            )
            await agent.handle(_task())

            call_kwargs = client._provider.create_message.call_args[1]
            assert "Workspace root" not in call_kwargs["system"]
        finally:
            builtin.workspace_root = original

    async def test_workspace_root_with_empty_tools_list(self, _workspace_root):
        config = {
            "model": "test-model",
            "tools": [],
        }
        client = _make_client()
        agent = TaskAgent(agent_id="code-writer", config=config, llm_client=client)
        await agent.handle(_task())

        call_kwargs = client._provider.create_message.call_args[1]
        # Empty tools list is falsy, so workspace root should not be injected
        assert "Workspace root" not in call_kwargs["system"]


# ─── AgentServer: advertise-address ─────────────────────────


class TestAdvertiseAddress:
    """Validates Docker service discovery via advertise-address."""

    def test_default_uses_host_port(self):
        server = AgentServer(host="0.0.0.0", port=50051)
        assert server.advertise_address == "0.0.0.0:50051"

    def test_explicit_advertise_address(self):
        server = AgentServer(
            host="0.0.0.0",
            port=50051,
            advertise_address="agent-planner:50051",
        )
        assert server.advertise_address == "agent-planner:50051"

    def test_none_falls_back_to_host_port(self):
        server = AgentServer(host="127.0.0.1", port=9999, advertise_address=None)
        assert server.advertise_address == "127.0.0.1:9999"

    async def test_registration_uses_advertise_address(self):
        """Self-registration payload contains advertise_address, not bind address."""
        server = AgentServer(
            host="0.0.0.0",
            port=50052,
            orchestrator_url="http://orchestrator:8080",
            advertise_address="agent-coder:50052",
        )
        stub = _StubAgent(agent_id="code-writer", config={"capabilities": []})
        server.register_agent(stub)

        mock_resp = AsyncMock()
        mock_resp.status = 201
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        server._session = mock_session

        await server._self_register()

        payload = mock_session.post.call_args[1]["json"]
        assert payload["address"] == "agent-coder:50052"


class TestPortZeroFixup:
    """Validates the port=0 advertise_address fixup in AgentServer.start().

    When port=0 (dynamic allocation), the default advertise_address contains
    ":0" which is unreachable.  The fixup must replace it with the actual port
    allocated by the OS — but only when no explicit advertise_address was
    provided.  (PR #71 deep-review §2.4.5.)
    """

    def test_default_address_gets_fixup(self):
        """No explicit advertise_address + port=0 → fixup applies."""
        server = AgentServer(host="127.0.0.1", port=0)
        assert server.advertise_address == "127.0.0.1:0"
        assert not server._advertise_address_explicit

    def test_explicit_address_skips_fixup(self):
        """Explicit advertise_address ending in :0 is NOT overwritten."""
        server = AgentServer(
            host="0.0.0.0",
            port=0,
            advertise_address="agent-planner:0",
        )
        assert server._advertise_address_explicit
        # Simulate what start() does: the fixup should NOT apply because
        # the address was explicitly provided.
        actual_port = 54321
        server.port = actual_port
        if not server._advertise_address_explicit and server.advertise_address.endswith(":0"):
            server.advertise_address = f"{server.host}:{actual_port}"
        # The explicit address must be preserved.
        assert server.advertise_address == "agent-planner:0"

    def test_default_address_fixup_replaces_port(self):
        """Simulates start() fixup: default :0 → :actual_port."""
        server = AgentServer(host="0.0.0.0", port=0)
        actual_port = 54321
        server.port = actual_port
        # Replicate the fixup logic from start()
        if not server._advertise_address_explicit and server.advertise_address.endswith(":0"):
            server.advertise_address = f"{server.host}:{actual_port}"
        assert server.advertise_address == "0.0.0.0:54321"

    def test_nonzero_port_no_fixup(self):
        """Fixed port (not 0) → no fixup needed, address is already correct."""
        server = AgentServer(host="127.0.0.1", port=50051)
        assert server.advertise_address == "127.0.0.1:50051"
        assert not server._advertise_address_explicit
        # endswith(":0") is False, so fixup would not trigger.
        assert not server.advertise_address.endswith(":0")


class _StubAgent(TaskAgent):
    """Minimal agent for server tests."""

    def __init__(self, agent_id: str, config: dict):
        super().__init__(agent_id=agent_id, config=config)
