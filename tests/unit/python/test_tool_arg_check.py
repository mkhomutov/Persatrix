"""A tool call's arguments are checked before the tool runs (RFC 0009 PR 3).

The model writes a call's arguments as JSON, and they used to go straight
into the tool as keyword arguments, so a missing or misspelt argument
surfaced as a ``TypeError`` from inside the tool. The check
(:mod:`agents.tools.arg_check`) refuses those calls first, converts a value
sent in a lossless other form to the one form the tool declares, and
otherwise passes every value through as sent (the plan's G-2). These tests
pin the rules on small stand-in tools; ``test_tool_arg_check_paths.py``
pins the two places a tool runs, and ``test_tool_arg_check_builtins.py`` the
built-in tools.
"""

from __future__ import annotations

import inspect
import itertools
import json
import math

import pytest

from agents.tools.arg_check import ArgumentRefusal, validate_arguments
from agents.tools.registry import ToolDefinition, ToolResult, clear_registry, get_tool, tool


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _def(name: str) -> ToolDefinition:
    td = get_tool(name)
    assert td is not None
    return td


def _check(name: str, arguments: object) -> dict | ArgumentRefusal:
    return validate_arguments(_def(name).argument_spec, arguments)


def _register_shapes() -> None:
    @tool(name="plain")
    async def plain(a: str, b: int = 1) -> ToolResult:
        return ToolResult(success=True)

    @tool(name="kwonly")
    async def kwonly(a: str, *, b: str, c: int = 2) -> ToolResult:
        return ToolResult(success=True)

    @tool(name="open")
    async def open_(a: str, **rest: str) -> ToolResult:
        return ToolResult(success=True)

    @tool(name="star")
    async def star(a: str, *more: str) -> ToolResult:
        return ToolResult(success=True)

    @tool(name="bare")
    async def bare() -> ToolResult:
        return ToolResult(success=True)


class TestMissingAndUnknown:
    @pytest.mark.parametrize("name", ["plain", "kwonly", "open", "star", "bare"])
    def test_refused_exactly_when_the_call_could_not_bind(self, name: str) -> None:
        # The rule the check replaces is Python's own: a call is refused for a
        # missing or unknown argument exactly when binding it would raise.
        _register_shapes()
        td = _def(name)
        sig = inspect.signature(td.func.__wrapped__)  # type: ignore[union-attr]
        keys = ["a", "b", "c", "more", "rest", "zzz"]
        for n in range(len(keys) + 1):
            for subset in itertools.combinations(keys, n):
                arguments = dict.fromkeys(subset, "x")
                try:
                    sig.bind(**arguments)
                    binds = True
                except TypeError:
                    binds = False
                checked = validate_arguments(td.argument_spec, arguments)
                assert isinstance(checked, dict) is binds, (name, subset)

    def test_missing_and_unknown_are_named_together(self) -> None:
        @tool(name="shell")
        async def shell(command: str, timeout: int = 30) -> ToolResult:
            return ToolResult(success=True)

        refusal = _check("shell", {"cmd": "ls"})

        assert isinstance(refusal, ArgumentRefusal)
        body = json.loads(refusal.content())
        assert list(body) == ["tool_argument_validation_error", "missing", "unknown", "invalid"]
        assert body["missing"] == ["command"]
        assert body["unknown"] == ["cmd"]
        assert "missing required argument: command" in body["tool_argument_validation_error"]
        assert "unknown argument: cmd (this tool takes: command, timeout)" in (
            body["tool_argument_validation_error"]
        )
        assert body["tool_argument_validation_error"].endswith(
            "The tool did not run; fix the arguments and call it again."
        )

    @pytest.mark.parametrize(
        ("arguments", "json_type"),
        [(None, "null"), ([], "array"), ("x", "string"), (30, "integer")],
    )
    def test_arguments_that_are_not_an_object_are_refused(
        self, arguments: object, json_type: str,
    ) -> None:
        _register_shapes()
        refusal = _check("plain", arguments)

        assert isinstance(refusal, ArgumentRefusal)
        detail = json.loads(refusal.content())["tool_argument_validation_error"]
        assert detail.startswith(f"arguments must be a JSON object, not {json_type}")

    def test_a_definition_built_by_hand_gets_the_shape_rule_only(self) -> None:
        # Nothing but @tool knows a function's signature, so a definition
        # built by hand (an MCP tool, later) is checked for shape alone.
        assert validate_arguments(None, {"anything": 1}) == {"anything": 1}
        assert isinstance(validate_arguments(None, [1]), ArgumentRefusal)

    def test_unknown_names_are_bounded(self) -> None:
        _register_shapes()
        arguments: dict[str, object] = {f"{i:03d}" + "x" * 500: 1 for i in range(100)}
        arguments["a"] = "ok"

        refusal = _check("plain", arguments)

        assert isinstance(refusal, ArgumentRefusal)
        unknown = json.loads(refusal.content())["unknown"]
        assert len(unknown) == 16 and all(len(n) == 64 for n in unknown)
        assert refusal.audit_fields()["unknown_argument_count"] == 100
        assert "... and 84 more" in refusal.detail


class TestConversion:
    @pytest.mark.parametrize(
        ("given", "expected"), [("30", 30), ("-5", -5), ("0", 0), (30.0, 30), (-0.0, 0)],
    )
    def test_integer_text_and_whole_floats_become_an_int(
        self, given: object, expected: int,
    ) -> None:
        _register_shapes()
        checked = _check("plain", {"a": "x", "b": given})

        assert isinstance(checked, dict)
        assert checked["b"] == expected and type(checked["b"]) is int

    @pytest.mark.parametrize(
        "given",
        [" 30 ", "+30", "3_0", "030", "-0", "٣٠", "30.0", "9" * 5000,
         5.5, True, False, math.nan, math.inf, [30], {"n": 30}, None],
        ids=lambda v: repr(v)[:20],
    )
    def test_a_value_with_no_lossless_int_form_passes_unchanged(self, given: object) -> None:
        # Not refused either: each of these reaches a tool today, which
        # decides what to do with it (shell_exec clamps 5.5, a tool may
        # int() " 30 " itself), so the check leaves it as sent.
        @tool(name="needs_int")
        async def needs_int(b: int) -> ToolResult:
            return ToolResult(success=True)

        checked = _check("needs_int", {"b": given})

        assert isinstance(checked, dict)
        assert checked["b"] is given

    def test_a_string_parameter_given_a_number_passes_unchanged(self) -> None:
        _register_shapes()
        checked = _check("plain", {"a": 30})

        assert isinstance(checked, dict) and checked["a"] == 30

    def test_null_for_an_optional_argument_means_its_default(self) -> None:
        @tool(name="nulls")
        async def nulls(a: str, b: int = 1, c: int | None = 5, d=None) -> ToolResult:
            return ToolResult(success=True)

        checked = _check("nulls", {"a": None, "b": None, "c": None, "d": None})

        # b's annotation does not admit None, so null is its default; c and d
        # take None as a value of their own, and a required argument keeps it.
        assert checked == {"a": None, "c": None, "d": None}

    def test_the_call_input_is_never_changed(self) -> None:
        _register_shapes()
        arguments = {"a": "x", "b": "30"}

        checked = _check("plain", arguments)

        assert arguments == {"a": "x", "b": "30"}
        assert checked is not arguments


class TestNarrowing:
    def test_a_narrowing_is_offered_and_checked_after_conversion(self) -> None:
        @tool(name="limited", narrow={"limit": {"minimum": 1}})
        async def limited(limit: int = 10) -> ToolResult:
            return ToolResult(success=True)

        assert _def("limited").parameters["properties"]["limit"] == {
            "type": "integer", "minimum": 1,
        }
        assert _check("limited", {"limit": "5"}) == {"limit": 5}
        refusal = _check("limited", {"limit": 0})
        assert isinstance(refusal, ArgumentRefusal)
        body = json.loads(refusal.content())
        assert body["invalid"] == [{"argument": "limit", "rule": "minimum", "got": "integer"}]
        assert "argument limit must be >= 1" in body["tool_argument_validation_error"]

    def test_a_type_narrowing_enforces_the_declared_type(self) -> None:
        @tool(name="strict", narrow={"path": {"type": "string"}})
        async def strict(path: str) -> ToolResult:
            return ToolResult(success=True)

        assert _def("strict").parameters["properties"]["path"] == {"type": "string"}
        refusal = _check("strict", {"path": None})
        assert isinstance(refusal, ArgumentRefusal)
        assert "argument path must be of type string, not null" in refusal.detail

    @pytest.mark.parametrize(
        "narrow",
        [{"nope": {"type": "string"}}, {"path": {"default": "x"}},
         {"path": {"type": "integer"}}, {"path": {"maxLength": "x"}}],
        ids=["unknown-parameter", "keyword-not-allowed", "type-differs", "bad-schema"],
    )
    def test_a_bad_narrowing_fails_when_the_tool_is_defined(self, narrow: dict) -> None:
        with pytest.raises(ValueError):
            @tool(name="bad", narrow=narrow)
            async def bad(path: str) -> ToolResult:
                return ToolResult(success=True)


class TestRefusalCarriesNoValue:
    def test_values_reach_neither_the_model_nor_the_audit_line(self) -> None:
        @tool(name="strict", narrow={"path": {"type": "string"}})
        async def strict(path: str) -> ToolResult:
            return ToolResult(success=True)

        refusal = _check("strict", {"path": ["SECRET-123"], "SECRET-KEY": "SECRET-123"})

        assert isinstance(refusal, ArgumentRefusal)
        assert "SECRET-123" not in refusal.content()
        assert "SECRET-123" not in json.dumps(refusal.audit_fields())
        assert refusal.audit_fields()["invalid_arguments"] == [
            {"argument": "path", "rule": "type", "got": "array"},
        ]

    def test_content_is_ascii_even_for_a_lone_surrogate_name(self) -> None:
        _register_shapes()
        refusal = _check("plain", {"a": "x", "\ud800": 1})

        assert isinstance(refusal, ArgumentRefusal)
        refusal.content().encode("ascii")
