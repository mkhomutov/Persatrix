"""The argument check on the built-in tools (RFC 0009 Phase 3).

The check enforces a parameter's declared type only where the tool asks for
it with a ``type`` narrowing, and a built-in asks only where every off-type
value already fails today (the plan's G-2: refuse only what fails today).
``TestNarrowedValuesFailToday`` pins that reason for each narrowing by
running the tool without the check; the other classes pin that the check
leaves the tools' own rules in place — the timeout clamp, ``PathValidator``,
the values that run today — and that no offered schema changes, so every
golden replays unchanged.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import LLMToolResult, ToolCall
from agents.memory.episodic import EpisodicMemory
from agents.tools import builtin
from agents.tools.memory_tools import create_memory_tools
from agents.tools.permissions import PermissionGate
from agents.tools.registry import ToolDefinition, ToolResult, clear_registry, get_tool
from agents.tools.sandbox import PathValidator
from agents.tools.tool_list import AdmittedCall, admit_call

_OFF_TYPE: list[Any] = [30, 30.0, True, ["x"], {"k": "v"}, None]


@pytest.fixture(autouse=True)
def _tools(tmp_path: Path, memory: EpisodicMemory) -> Any:
    clear_registry()
    importlib.reload(builtin)
    saved = (builtin.permission_gate, builtin.path_validator, builtin.workspace_root)
    builtin.permission_gate = PermissionGate({
        "filesystem": {"read": [str(tmp_path / "**")], "write": [str(tmp_path / "**")]},
        "shell": {"allowed_commands": ["echo"]},
        "network": {"allow": ["api.example.com"], "deny": ["*"]},
    })
    builtin.path_validator = PathValidator(
        allow_read=[str(tmp_path / "**")], allow_write=[str(tmp_path / "**")],
    )
    builtin.workspace_root = tmp_path
    create_memory_tools(memory, PermissionGate({"memory": {"read": True, "write": True}}))
    (tmp_path / "a.txt").write_text("hello")
    yield
    builtin.permission_gate, builtin.path_validator, builtin.workspace_root = saved
    clear_registry()


def _def(name: str) -> ToolDefinition:
    td = get_tool(name)
    assert td is not None and td.func is not None
    return td


async def _admit_and_run(name: str, arguments: dict[str, Any]) -> ToolResult | LLMToolResult:
    """Run *arguments* through the check, then the tool, as both paths do."""
    admitted = admit_call("t", {name: _def(name)}, ToolCall(id="c1", name=name, input=arguments))
    if isinstance(admitted, AdmittedCall):
        return await admitted.run(**admitted.arguments)
    return admitted


def _ok_http_session() -> MagicMock:
    resp = AsyncMock(status=200, headers={})
    resp.text = AsyncMock(return_value="ok")
    resp.__aenter__ = AsyncMock(return_value=resp)
    session = AsyncMock()
    session.request = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    return MagicMock(return_value=session)


async def _a_note_id() -> str:
    stored = await _def("store_note").func(topic="t", content="c")  # type: ignore[misc]
    assert stored.success
    return str(stored.data["note_id"])


class TestNarrowedValuesFailToday:
    @pytest.mark.parametrize(
        ("name", "parameter", "valid"),
        [
            ("file_read", "path", {"path": "{ws}/a.txt"}),
            ("file_write", "path", {"path": "{ws}/b.txt", "content": "x"}),
            ("file_write", "content", {"path": "{ws}/b.txt", "content": "x"}),
            ("shell_exec", "command", {"command": "echo hi"}),
            ("http_request", "url", {"url": "https://api.example.com/x"}),
            ("http_request", "method", {"url": "https://api.example.com/x", "method": "GET"}),
            ("store_note", "topic", {"topic": "t", "content": "c"}),
            ("store_note", "content", {"topic": "t", "content": "c"}),
            ("update_note", "note_id", {"note_id": None, "content": "c"}),
            ("update_note", "content", {"note_id": None, "content": "c"}),
            ("delete_note", "note_id", {"note_id": None}),
        ],
    )
    async def test_every_off_type_value_fails_without_the_check(
        self, tmp_path: Path, name: str, parameter: str, valid: dict[str, Any],
    ) -> None:
        td = _def(name)
        if "path" in valid:  # a relative path resolves against the process's directory
            valid = {**valid, "path": valid["path"].format(ws=tmp_path)}
        assert td.parameters["properties"][parameter] == {"type": "string"}
        assert td.argument_spec is not None and parameter in td.argument_spec.narrowed
        if "note_id" in valid:
            valid = {**valid, "note_id": await _a_note_id()}
        with patch("agents.tools.builtin.aiohttp.ClientSession", _ok_http_session()):
            control = await td.func(**valid)  # type: ignore[misc]
            assert control.success, control.error
            for value in _OFF_TYPE:
                if (name, value) == ("shell_exec", None):
                    continue  # reads the agent's stdin on Python 3.11; see below
                if "note_id" in valid:
                    valid = {**valid, "note_id": await _a_note_id()}
                result = await td.func(**{**valid, parameter: value})  # type: ignore[misc]
                assert result.success is False, (name, parameter, value)


class TestToolRulesStayInPlace:
    async def test_the_shell_timeout_is_still_clamped(self) -> None:
        clamps: list[tuple[Any, int]] = []
        real = builtin._shell_timeout

        def spy(requested: Any, limit: int | None) -> int:
            clamps.append((requested, real(requested, limit)))
            return clamps[-1][1]

        with patch.object(builtin, "_shell_timeout", spy):
            for timeout in ("1000", -5, 10**9):
                result = await _admit_and_run(
                    "shell_exec", {"command": "echo hi", "timeout": timeout},
                )
                assert isinstance(result, ToolResult) and result.success

        assert clamps == [(1000, 300), (-5, 1), (10**9, 300)]

    @pytest.mark.parametrize(
        ("name", "arguments"),
        [
            ("shell_exec", {"command": "echo hi", "timeout": 5.5}),
            ("shell_exec", {"command": "echo hi", "timeout": True}),
            ("store_note", {"topic": "t", "content": "c", "tags": None}),
            ("store_note", {"topic": "t", "content": "c", "tags": 0}),
            ("store_note", {"topic": "t", "content": "c", "tags": []}),
            ("recall_notes", {"query": None}),
            ("recall_notes", {"query": 0}),
            ("recall_notes", {"limit": True}),
            ("http_request", {"url": "https://api.example.com/x", "body": {"k": "v"}}),
        ],
    )
    async def test_values_that_run_today_still_run(
        self, name: str, arguments: dict[str, Any],
    ) -> None:
        with patch("agents.tools.builtin.aiohttp.ClientSession", _ok_http_session()):
            result = await _admit_and_run(name, arguments)

        assert isinstance(result, ToolResult) and result.success, result

    async def test_a_null_shell_command_is_refused_before_it_is_split(self) -> None:
        # On Python 3.11 shlex.split(None) reads the agent process's stdin.
        with patch("agents.tools.builtin.shlex.split") as split:
            result = await _admit_and_run("shell_exec", {"command": None})

        assert isinstance(result, LLMToolResult) and result.is_error
        split.assert_not_called()

    async def test_off_type_content_is_refused_before_a_directory_is_made(
        self, tmp_path: Path,
    ) -> None:
        result = await _admit_and_run(
            "file_write", {"path": str(tmp_path / "new" / "dir" / "f.txt"), "content": 30},
        )

        assert isinstance(result, LLMToolResult) and result.is_error
        assert not (tmp_path / "new").exists()

    async def test_a_path_traversal_string_reaches_path_validator_as_sent(self) -> None:
        path = "../../../../etc/passwd"
        validator = builtin.path_validator
        assert validator is not None
        with patch.object(validator, "validate", wraps=validator.validate) as validate:
            result = await _admit_and_run("file_read", {"path": path})

        validate.assert_called_once_with(path, mode="read")
        assert isinstance(result, ToolResult)
        assert (result.success, result.error_type) == (False, "PermissionError")


def test_no_offered_schema_changes() -> None:
    # The golden request hashes cover every offered tool definition.
    string, integer = {"type": "string"}, {"type": "integer"}
    expected = {
        "file_read": ({"path": string}, ["path"]),
        "file_write": ({"path": string, "content": string}, ["path", "content"]),
        "shell_exec": ({"command": string, "timeout": integer}, ["command"]),
        "http_request": ({"url": string, "method": string, "body": string}, ["url"]),
        "store_note": ({"topic": string, "content": string, "tags": string},
                       ["topic", "content"]),
        "recall_notes": ({"query": string, "limit": integer}, None),
        "update_note": ({"note_id": string, "content": string}, ["note_id", "content"]),
        "delete_note": ({"note_id": string}, ["note_id"]),
    }
    for name, (properties, required) in expected.items():
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        assert _def(name).parameters == schema, name
