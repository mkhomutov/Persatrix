"""The argument check's rules for nulls, numbers and narrowings (RFC 0009 Phase 3).

``test_tool_arg_check.py`` pins the check's main rules; this file pins the
ones the PR's second review tightened: a null on a narrowed parameter, a
number parameter's one form, the narrowings refused when a tool is defined,
the schema offered for ``*args`` and ``**kwargs``, and the wording of every
narrowing keyword.
"""

from __future__ import annotations

import dataclasses
import json
import math
import sys
from typing import Any

import pytest

from agents.tools import arg_check
from agents.tools.arg_check import NARROWING_KEYWORDS, ArgumentRefusal, validate_arguments
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


class TestNullOnANarrowedParameter:
    def test_null_is_refused_where_the_annotation_does_not_take_none(self) -> None:
        # http_request's method: a null failed inside the tool (None.upper()),
        # so its narrowing refuses it rather than run a GET.
        @tool(name="req", narrow={"method": {"type": "string"}})
        async def req(url: str, method: str = "GET") -> ToolResult:
            return ToolResult(success=True)

        refusal = _check("req", {"url": "u", "method": None})

        assert isinstance(refusal, ArgumentRefusal)
        assert "argument method must be of type string, not null" in refusal.detail

    def test_null_runs_where_the_annotation_takes_none(self) -> None:
        # The tool takes None, so the call ran before the check and still runs.
        @tool(name="maybe", narrow={"x": {"maxLength": 5}})
        async def maybe(x: str | None = None) -> ToolResult:
            return ToolResult(success=True)

        assert _check("maybe", {"x": None}) == {"x": None}
        assert isinstance(_check("maybe", {"x": "toolong"}), ArgumentRefusal)

    def test_an_un_narrowed_optional_null_still_means_its_default(self) -> None:
        @tool(name="req", narrow={"url": {"type": "string"}})
        async def req(url: str, method: str = "GET") -> ToolResult:
            return ToolResult(success=True)

        assert _check("req", {"url": "u", "method": None}) == {"url": "u"}


class TestNumberOneForm:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [(1, 1.0), (-3, -3.0), (0, 0.0), ("1", 1.0), ("0.5", 0.5), ("-2.25", -2.25),
         (0.5, 0.5), (2**53, float(2**53))],
    )
    def test_a_number_parameter_gets_a_float(self, given: object, expected: float) -> None:
        @tool(name="ratio")
        async def ratio(r: float) -> ToolResult:
            return ToolResult(success=True)

        checked = _check("ratio", {"r": given})

        assert isinstance(checked, dict)
        assert checked["r"] == expected and type(checked["r"]) is float

    @pytest.mark.parametrize(
        "given",
        [True, False, " 0.5", "+0.5", ".5", "0.50", "1e5", "0x10", "nan", "inf",
         2**53 + 1, 10**400, math.nan, [1], None],
        ids=lambda v: repr(v)[:20],
    )
    def test_a_value_with_no_lossless_float_form_passes_unchanged(self, given: object) -> None:
        @tool(name="ratio")
        async def ratio(r: float) -> ToolResult:
            return ToolResult(success=True)

        checked = _check("ratio", {"r": given})

        assert isinstance(checked, dict)
        assert checked["r"] is given

    def test_number_text_longer_than_python_reads_passes_unchanged(self) -> None:
        @tool(name="ratio")
        async def ratio(r: float) -> ToolResult:
            return ToolResult(success=True)

        limit = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(4300)
        try:
            checked = _check("ratio", {"r": "9" * 5000})
        finally:
            sys.set_int_max_str_digits(limit)

        assert checked == {"r": "9" * 5000}

    def test_two_providers_sending_one_value_give_one_form(self) -> None:
        # Anthropic sends 1 where Gemini sends 1.0; RFC 0028's hold hashes
        # the arguments as JSON, so the two must serialise alike (G-1).
        @tool(name="ratio")
        async def ratio(r: float) -> ToolResult:
            return ToolResult(success=True)

        forms = {json.dumps(_check("ratio", {"r": sent})) for sent in (1, 1.0, "1", "1.0")}

        assert forms == {'{"r": 1.0}'}


class TestBadNarrowingsFailWhenTheToolIsDefined:
    @pytest.mark.parametrize(
        "narrow",
        [{"count": {"minimum": math.nan}}, {"count": {"maximum": math.inf}},
         {"ratio": {"minimum": -math.inf}}, {"path": {"enum": 5}}, {"path": {"enum": []}},
         {"count": {"minLength": 1}}, {"path": {"minimum": 1}}, {"flag": {"maxLength": 1}},
         {"flag": {"maximum": 1}}, {"count": {"minimum": 5, "maximum": 1}},
         {"path": {"minLength": 3, "maxLength": 2}}, {"path": "string"}, {"path": 5}],
        ids=["nan-bound", "infinite-bound", "negative-infinite-bound", "enum-not-a-list",
             "empty-enum", "length-on-an-integer", "bound-on-text", "length-on-a-boolean",
             "bound-on-a-boolean", "minimum-over-maximum", "min-length-over-max-length",
             "rule-is-text", "rule-is-a-number"],
    )
    def test_a_narrowing_that_cannot_work_raises_value_error(self, narrow: dict) -> None:
        with pytest.raises(ValueError):
            @tool(name="bad", narrow=narrow)
            async def bad(
                path: str, count: int = 1, ratio: float = 0.5, flag: bool = False,
            ) -> ToolResult:
                return ToolResult(success=True)

        assert get_tool("bad") is None

    def test_equal_bounds_are_allowed(self) -> None:
        @tool(name="fixed", narrow={"count": {"minimum": 2, "maximum": 2}})
        async def fixed(count: int) -> ToolResult:
            return ToolResult(success=True)

        assert _check("fixed", {"count": 2}) == {"count": 2}


class TestOfferedSchemaForStarParameters:
    def test_star_args_are_not_offered(self) -> None:
        # A call binds by name alone, so *more can never be filled; offering
        # it as required told the model to send what the check refuses.
        @tool(name="star")
        async def star(a: str, *more: str) -> ToolResult:
            return ToolResult(success=True)

        assert _def("star").parameters == {
            "type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
        }

    def test_star_kwargs_are_not_offered(self) -> None:
        @tool(name="open")
        async def open_(a: str, **rest: str) -> ToolResult:
            return ToolResult(success=True)

        assert _def("open").parameters == {
            "type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
        }
        assert _check("open", {"a": "x", "extra": 1}) == {"a": "x", "extra": 1}


class TestNarrowingWords:
    def test_every_keyword_has_its_wording(self) -> None:
        assert set(arg_check._RULES) == NARROWING_KEYWORDS

    @pytest.mark.parametrize(
        ("narrow", "given", "text"),
        [({"minimum": 1}, 0, "must be >= 1"),
         ({"maximum": 3}, 4, "must be <= 3"),
         ({"minLength": 2}, "a", "must be at least 2 characters"),
         ({"maxLength": 1}, "ab", "must be at most 1 character"),
         ({"enum": ["a"]}, "b", 'must be one of ["a"]'),
         ({"type": "integer"}, "x", "must be of type integer, not string")],
    )
    def test_each_keyword_reads_as_a_sentence(
        self, narrow: dict[str, Any], given: object, text: str,
    ) -> None:
        if {"minimum", "maximum", "type"} & set(narrow):
            @tool(name="worded", narrow={"v": narrow})
            async def as_int(v: int) -> ToolResult:
                return ToolResult(success=True)
        else:
            @tool(name="worded", narrow={"v": narrow})
            async def as_text(v: str) -> ToolResult:
                return ToolResult(success=True)

        refusal = _check("worded", {"v": given})

        assert isinstance(refusal, ArgumentRefusal)
        assert f"argument v {text}." in refusal.detail


def test_an_un_narrowed_tool_skips_the_narrowing_pass() -> None:
    # Only a narrowed parameter is checked against a schema, so a tool with
    # none never pays for a pass over an empty one.
    class _Refuses:
        def iter_errors(self, instance: object) -> Any:
            raise AssertionError("an un-narrowed tool ran the narrowing pass")

    @tool(name="plain")
    async def plain(a: str, b: int = 1) -> ToolResult:
        return ToolResult(success=True)

    spec = _def("plain").argument_spec
    assert spec is not None and spec.narrowed == frozenset()
    spec = dataclasses.replace(spec, validator=_Refuses())

    assert validate_arguments(spec, {"a": "x", "b": "2"}) == {"a": "x", "b": 2}
