"""
The cap on what a tool returns, and the ``resource_limits`` block that sets
it (RFC 0009 Phase 3; the Phases 3–4 plan's PR 4, design point G-3).

An agent's ``resource_limits.tool_output_bytes`` caps what any one tool call
hands the model; ``shell_cpu_seconds`` caps a ``shell_exec`` command's CPU
time (its own tests are in ``test_shell_exec_limits.py``). With no block,
every tool returns what it returned before, and the three built-in tools
that already cut their output at 100 KB still do, with the same notice.

The cap is applied in the ``@tool`` wrapper, so it holds for every tool.
A structured output keeps its fields: its long texts are cut, so a long
stdout does not cost the model ``shell_exec``'s stderr and exit code.
A cut writes a ``tool.resource_limited`` audit line.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import jsonschema  # type: ignore[import-untyped]
import pytest
import yaml

from agents.server_persona import load_agent
from agents.tools import builtin, registry
from agents.tools.registry import ToolResult, clear_registry, tool
from agents.tools.sandbox import (
    OutputSizeLimiter,
    read_resource_limits,
    render_output,
    truncate_text,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENT_SCHEMA: dict[str, Any] = json.loads(
    (_REPO_ROOT / "schemas" / "agent.schema.json").read_text(encoding="utf-8"),
)
_LIMITS_VALIDATOR = jsonschema.Draft7Validator(
    {"$ref": "#/definitions/resource_limits", "definitions": _AGENT_SCHEMA["definitions"]},
)


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "agents.tools.audit"]


@pytest.fixture(autouse=True)
def _restore_tool_state():
    """Each test starts with an empty registry and no limits, and leaves them so."""
    clear_registry()
    saved = (
        registry.output_limiter, builtin.resource_limiter,
        builtin.permission_gate, builtin.path_validator, builtin.workspace_root,
    )
    registry.output_limiter = None
    yield
    (
        registry.output_limiter, builtin.resource_limiter,
        builtin.permission_gate, builtin.path_validator, builtin.workspace_root,
    ) = saved
    clear_registry()


# ─── Cutting one text ─────────────────────────────────────────


class TestTruncateText:
    def test_text_within_the_cap_is_unchanged(self):
        assert truncate_text("short", 10) == "short"

    def test_a_cut_at_100_kb_reads_as_the_built_in_tools_cut_today(self):
        """G-3: the three built-in tools keep their notice, word for word."""
        cut = truncate_text("X" * 102_500, 102_400)
        assert cut == "X" * 102_400 + "\n... [truncated at 100 KB]"

    def test_a_cap_that_is_not_whole_kilobytes_is_named_in_bytes(self):
        assert truncate_text("abcdef", 4) == "abcd\n... [truncated at 4 bytes]"

    def test_a_cut_never_splits_a_character(self):
        # "é" is two bytes in UTF-8: five bytes keep two whole characters.
        assert truncate_text("é" * 10, 5) == "éé\n... [truncated at 5 bytes]"

    def test_the_built_in_tools_cut_is_this_cut(self):
        text = "X" * 102_500
        assert builtin._truncate(text) == truncate_text(text, builtin.MAX_OUTPUT_BYTES)


# ─── Cutting one tool call's output ───────────────────────────


class TestOutputSizeLimiter:
    def test_output_within_the_cap_is_returned_as_it_is(self):
        data = {"stdout": "ok", "stderr": "", "exit_code": 0}
        assert OutputSizeLimiter(1024).limit(data) == (data, None)

    def test_text_over_the_cap_is_cut_with_the_notice_inside_the_cap(self):
        """Review F-5: the notice counts toward the cap, so the model gets at
        most the cap."""
        data, size = OutputSizeLimiter(1024).limit("A" * 5000)
        notice = "\n... [truncated at 1 KB]"
        assert data == "A" * (1024 - len(notice)) + notice
        assert _size(data) == 1024
        assert size == 5000

    def test_a_structured_output_fits_and_keeps_its_other_fields(self):
        """A long stdout does not cost the model the stderr and exit code."""
        original = {"stdout": "A" * 10_000, "stderr": "boom", "exit_code": 2}
        data, size = OutputSizeLimiter(2048).limit(original)
        assert size == _size(json.dumps(original))
        assert _size(render_output(data)) <= 2048
        assert data["stderr"] == "boom"
        assert data["exit_code"] == 2
        assert data["stdout"].startswith("A" * 1500)
        assert data["stdout"].endswith("\n... [truncated at 2 KB]")
        assert original["stdout"] == "A" * 10_000  # the tool's own value is left alone

    def test_two_long_texts_share_the_cap(self):
        data, _ = OutputSizeLimiter(4096).limit({"stdout": "A" * 9000, "stderr": "B" * 9000})
        assert _size(render_output(data)) <= 4096
        assert len(data["stdout"]) > 1500
        assert len(data["stderr"]) > 1500

    def test_texts_inside_lists_are_cut_and_the_rows_kept(self):
        rows = [{"text": "x" * 3000, "id": i} for i in range(3)]
        data, _ = OutputSizeLimiter(4096).limit(rows)
        assert _size(render_output(data)) <= 4096
        assert [row["id"] for row in data] == [0, 1, 2]

    def test_output_with_too_many_fields_to_keep_its_shape_is_cut_as_text(self):
        """No cut of its texts makes it fit, so the model gets the head of what it
        would have seen."""
        rows = list(range(5000))
        data, size = OutputSizeLimiter(1024).limit(rows)
        assert isinstance(data, str)
        assert data.startswith("[0, 1, 2")
        assert data.endswith("\n... [truncated at 1 KB]")
        assert _size(data) <= 1024
        assert size == _size(json.dumps(rows))

    def test_short_texts_beside_a_long_one_do_not_cost_the_shape(self):
        """Review F-4: cutting every text to nothing turns each short one into
        a longer notice, so a cut that leaves short texts whole can fit where
        that one does not."""
        original = {"ids": [f"{i % 100:02d}" for i in range(1000)], "content": "x" * 100_000}
        data, _ = OutputSizeLimiter(16_384).limit(original)
        assert isinstance(data, dict)
        assert data["ids"] == original["ids"]
        assert data["content"].endswith("\n... [truncated at 16 KB]")
        assert _size(render_output(data)) <= 16_384

    def test_output_the_model_cannot_be_shown_is_left_to_the_tool_round(self):
        """An output json.dumps refuses fails in the tool round today; the cap
        does not change that."""
        data = {"when": object()}
        assert OutputSizeLimiter(1024).limit(data) == (data, None)

    @pytest.mark.parametrize("max_bytes", [1023, 0, -1])
    def test_a_cap_under_one_kilobyte_is_refused(self, max_bytes):
        """A cap too small to hold the notice could not keep its own promise."""
        with pytest.raises(ValueError):
            OutputSizeLimiter(max_bytes)


# ─── The cap in the @tool wrapper ─────────────────────────────


class TestToolOutputCap:
    async def test_with_no_cap_set_a_tool_returns_what_it_returned_before(self):
        @tool(name="big")
        async def big() -> ToolResult:
            return ToolResult(success=True, data="A" * 500_000)

        result = await big()
        assert result.data == "A" * 500_000

    async def test_a_tool_given_an_output_cap_cuts_with_the_notice(
        self, caplog: pytest.LogCaptureFixture,
    ):
        @tool(name="big")
        async def big() -> ToolResult:
            return ToolResult(success=True, data="A" * 5000)

        registry.output_limiter = OutputSizeLimiter(1024)
        with caplog.at_level(logging.INFO, logger="agents.tools.audit"):
            result = await big()
        assert result.success is True
        assert result.data.endswith("\n... [truncated at 1 KB]")
        assert _size(result.data) == 1024
        [record] = _audit_records(caplog)
        assert record.getMessage() == "tool.resource_limited"
        assert record.tool_name == "big"  # type: ignore[attr-defined]
        assert record.limit == "tool_output_bytes"  # type: ignore[attr-defined]
        assert record.max_bytes == 1024  # type: ignore[attr-defined]
        assert record.output_bytes == 5000  # type: ignore[attr-defined]

    async def test_a_failed_calls_output_is_capped_and_its_error_kept(self):
        @tool(name="failing")
        async def failing() -> ToolResult:
            return ToolResult(
                success=False, data={"stdout": "A" * 5000, "exit_code": 1},
                error="Command exited with code 1",
            )

        registry.output_limiter = OutputSizeLimiter(1024)
        result = await failing()
        assert result.success is False
        assert result.error == "Command exited with code 1"
        assert result.data["exit_code"] == 1
        assert _size(render_output(result.data)) <= 1024

    async def test_a_plain_return_value_is_capped(self):
        @tool(name="plain")
        async def plain() -> str:
            return "A" * 5000

        registry.output_limiter = OutputSizeLimiter(1024)
        result = await plain()
        assert result.data.endswith("\n... [truncated at 1 KB]")
        assert _size(result.data) == 1024

    async def test_output_within_the_cap_writes_no_audit_line(
        self, caplog: pytest.LogCaptureFixture,
    ):
        @tool(name="small")
        async def small() -> ToolResult:
            return ToolResult(success=True, data="ok")

        registry.output_limiter = OutputSizeLimiter(1024)
        with caplog.at_level(logging.INFO, logger="agents.tools.audit"):
            result = await small()
        assert result.data == "ok"
        assert _audit_records(caplog) == []


# ─── The resource_limits block ────────────────────────────────


class TestReadResourceLimits:
    def test_no_block_sets_no_limit(self):
        limits = read_resource_limits(None)
        assert limits.shell.cpu_seconds is None
        assert limits.output is None

    def test_a_block_sets_both_limits(self):
        limits = read_resource_limits({"shell_cpu_seconds": 10, "tool_output_bytes": 65_536})
        assert limits.shell.cpu_seconds == 10
        assert limits.output is not None
        assert limits.output.max_bytes == 65_536

    @pytest.mark.parametrize(
        "block",
        [
            {},
            {"shell_cpu_seconds": 300, "tool_output_bytes": 102_400},
            {"shell_cpu_seconds": 1, "tool_output_bytes": 1024},
            # Review F-6: what YAML gives for a block with only its header
            # uncommented, and for a key left empty, both set no limit.
            None,
            {"shell_cpu_seconds": None, "tool_output_bytes": None},
        ],
    )
    def test_the_schema_and_the_runtime_accept_the_same_block(self, block):
        _LIMITS_VALIDATOR.validate(block)
        read_resource_limits(block)

    def test_a_whole_number_written_with_a_decimal_point_is_that_number(self):
        """Review F-6: JSON Schema's integer takes 10.0, so the runtime does too."""
        _LIMITS_VALIDATOR.validate({"shell_cpu_seconds": 10.0})
        limits = read_resource_limits({"shell_cpu_seconds": 10.0, "tool_output_bytes": 2048.0})
        assert limits.shell.cpu_seconds == 10
        assert isinstance(limits.shell.cpu_seconds, int)
        assert limits.output is not None
        assert limits.output.max_bytes == 2048

    def test_keys_of_mixed_types_are_refused_cleanly(self):
        """Review F-9: a YAML block can hold a number as a key beside a name."""
        with pytest.raises(ValueError, match="resource_limits does not take"):
            read_resource_limits({1: 2, "a": 3})

    @pytest.mark.parametrize(
        "block",
        [
            {"shell_cpu_seconds": -1},  # a negative limit
            {"tool_output_bytes": -1},
            {"shell_cpu_seconds": 0},
            {"shell_cpu_seconds": 301},  # over the 300-second wall-time ceiling
            {"shell_cpu_seconds": "10"},
            {"shell_cpu_seconds": True},
            {"shell_cpu_seconds": 1.5},
            {"tool_output_bytes": 0},
            {"tool_output_bytes": 1023},  # too small to hold the notice
            {"tool_output_bytes": 102_401},  # over the built-in tools' own cap
            {"cpu_seconds": 10},  # a key the block does not take
            ["shell_cpu_seconds"],
        ],
    )
    def test_the_schema_and_the_runtime_refuse_the_same_block(self, block):
        with pytest.raises(jsonschema.ValidationError):
            _LIMITS_VALIDATOR.validate(block)
        with pytest.raises(ValueError):
            read_resource_limits(block)

    def test_the_shipped_configs_commented_example_is_a_good_block(self):
        """``config/agents.yaml`` shows the block commented out, so the shipped
        agents keep today's behaviour; uncommented, it must still be valid."""
        lines = (_REPO_ROOT / "config" / "agents.yaml").read_text(encoding="utf-8").splitlines()
        start = lines.index("    # resource_limits:")
        example = [lines[start]]
        for line in lines[start + 1:]:
            if not line.startswith("    #   "):
                break
            example.append(line)
        block = yaml.safe_load("\n".join(line.replace("# ", "", 1) for line in example))
        _LIMITS_VALIDATOR.validate(block["resource_limits"])
        limits = read_resource_limits(block["resource_limits"])
        assert limits.shell.cpu_seconds == 10
        assert limits.output is not None
        assert limits.output.max_bytes == 65_536


def _write_agent(tmp: Path, extra: dict[str, Any]) -> str:
    config_path = tmp / "agents.yaml"
    agent = {
        "id": "code-writer", "name": "Code Writer", "model": "test-model",
        "capabilities": ["code_generation"], "tools": ["shell_exec"],
        "permissions": {"shell": {"allowed_commands": ["echo"]}},
        **extra,
    }
    config_path.write_text(
        yaml.dump({"schema_version": "0.1", "agents": [agent]}), encoding="utf-8",
    )
    return str(config_path)


@patch("agents.server_persona.create_provider", return_value=(MagicMock(), "test-model"))
class TestLoadAgentWiresTheLimits:
    def test_load_agent_sets_the_agents_limits(self, _create):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_agent(
                Path(tmp),
                {"resource_limits": {"shell_cpu_seconds": 10, "tool_output_bytes": 65_536}},
            )
            load_agent("code-writer", config_path, tmp)
        assert builtin.resource_limiter is not None
        assert builtin.resource_limiter.cpu_seconds == 10
        assert registry.output_limiter is not None
        assert registry.output_limiter.max_bytes == 65_536

    def test_an_agent_with_no_block_leaves_no_earlier_limit_behind(self, _create):
        registry.output_limiter = OutputSizeLimiter(1024)
        with tempfile.TemporaryDirectory() as tmp:
            load_agent("code-writer", _write_agent(Path(tmp), {}), tmp)
        assert registry.output_limiter is None
        assert builtin.resource_limiter is not None
        assert builtin.resource_limiter.cpu_seconds is None

    def test_a_block_the_schema_refuses_stops_the_agent_at_startup(self, _create):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = _write_agent(Path(tmp), {"resource_limits": {"shell_cpu_seconds": -1}})
            with pytest.raises(SystemExit, match="resource_limits.shell_cpu_seconds"):
                load_agent("code-writer", config_path, tmp)
