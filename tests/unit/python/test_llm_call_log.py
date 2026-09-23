"""The call log: one line per model call, tagged (EXP-001 harness PR 3).

Pre-registration §3, check 4: every model call is recorded with its arm,
meeting, adviser, purpose, time and token counts, cache reads and writes
included. The advisers are separate agent processes, so the record has to be
written where the call is made. When ``PERSATRIX_CALL_LOG`` names a file,
``LLMClient`` appends one JSON line per call to it. The line carries the
fixed tags from ``PERSATRIX_CALL_TAGS``, the agent's ID and the purpose the
call site passed. Without the setting nothing is written.

The purpose must come from the call site: the model alias alone cannot tell
a salience bid from the reflexion critic, since both use ``fast``. An AST
check below keeps every call site passing one.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agents import call_log
from agents.llm_client import LLMClient, LLMResponse, Usage
from agents.llm_types import CallPurpose
from agents.observability import metrics

_AGENTS = Path(__file__).resolve().parents[3] / "agents"


class _Provider:
    name = "anthropic"

    def __init__(self, response: LLMResponse | None = None, error: Exception | None = None):
        self._response = response or LLMResponse(
            text="ok", usage=Usage(10, 5, cache_write_tokens=7, cache_read_tokens=3),
        )
        self._error = error

    async def create_message(self, **_: object) -> LLMResponse:
        if self._error is not None:
            raise self._error
        return self._response


@pytest.fixture
def log_path(tmp_path, monkeypatch) -> Iterator[Path]:
    path = tmp_path / "calls.jsonl"
    monkeypatch.setenv(call_log.CALL_LOG_ENV, str(path))
    monkeypatch.setenv(
        call_log.CALL_TAGS_ENV,
        json.dumps({"arm": "D", "series": "series-1", "meeting": "plan-1"}),
    )
    monkeypatch.setattr(metrics, "_AGENT_ID", "ember-owl")
    call_log.reset_call_log()
    yield path
    call_log.reset_call_log()


async def _call(client: LLMClient, **extra: Any) -> LLMResponse:
    return await client.create_message(
        model="claude-sonnet-4-6", model_alias="quality", messages=[], system="s",
        tools=[], max_tokens=100, temperature=0.7, **extra,
    )


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestWritingTheLog:
    async def test_a_call_writes_one_tagged_line(self, log_path):
        before = dt.datetime.now(dt.UTC)
        await _call(LLMClient(_Provider()), purpose=CallPurpose.TURN)
        after = dt.datetime.now(dt.UTC)
        [line] = _lines(log_path)
        started = dt.datetime.fromisoformat(line.pop("started_at"))
        assert before <= started <= after
        assert line == {
            "tags": {"arm": "D", "series": "series-1", "meeting": "plan-1"},
            "agent_id": "ember-owl",
            "purpose": "turn",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "model_alias": "quality",
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_write_tokens": 7,
            "cache_read_tokens": 3,
            "error": None,
        }

    async def test_calls_append(self, log_path):
        client = LLMClient(_Provider())
        await _call(client, purpose=CallPurpose.BID)
        await _call(client, purpose=CallPurpose.SUMMARY)
        assert [line["purpose"] for line in _lines(log_path)] == ["bid", "summary"]

    async def test_a_failed_call_is_logged_with_its_error_and_still_raises(self, log_path):
        with pytest.raises(TimeoutError):
            await _call(LLMClient(_Provider(error=TimeoutError("slow"))), purpose=CallPurpose.TURN)
        [line] = _lines(log_path)
        assert line["error"] == "TimeoutError"
        assert (line["input_tokens"], line["output_tokens"]) == (0, 0)

    async def test_a_cancelled_call_is_logged(self, log_path):
        # summarize_close wraps its call in asyncio.wait_for, which cancels it.
        import asyncio

        with pytest.raises(asyncio.CancelledError):
            await _call(
                LLMClient(_Provider(error=asyncio.CancelledError())), purpose=CallPurpose.SUMMARY,
            )
        [line] = _lines(log_path)
        assert line["error"] == "CancelledError"

    async def test_a_call_without_a_purpose_is_logged_as_null(self, log_path):
        await _call(LLMClient(_Provider()))
        [line] = _lines(log_path)
        assert line["purpose"] is None

    async def test_no_tags_setting_logs_empty_tags(self, log_path, monkeypatch):
        monkeypatch.delenv(call_log.CALL_TAGS_ENV)
        call_log.reset_call_log()
        await _call(LLMClient(_Provider()), purpose=CallPurpose.TURN)
        assert _lines(log_path)[0]["tags"] == {}


class TestSettings:
    async def test_nothing_is_written_without_the_setting(self, tmp_path, monkeypatch):
        monkeypatch.delenv(call_log.CALL_LOG_ENV, raising=False)
        monkeypatch.chdir(tmp_path)
        call_log.reset_call_log()
        await _call(LLMClient(_Provider()), purpose=CallPurpose.TURN)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("raw", ["not json", "[1, 2]", '{"arm": 4}'])
    def test_tags_that_are_not_an_object_of_strings_are_refused(self, raw, monkeypatch):
        monkeypatch.setenv(call_log.CALL_TAGS_ENV, raw)
        call_log.reset_call_log()
        with pytest.raises(ValueError, match=call_log.CALL_TAGS_ENV):
            call_log.call_tags()

    def test_tags_are_read_once(self, monkeypatch):
        monkeypatch.setenv(call_log.CALL_TAGS_ENV, '{"arm": "B"}')
        call_log.reset_call_log()
        assert call_log.call_tags() == {"arm": "B"}
        monkeypatch.setenv(call_log.CALL_TAGS_ENV, '{"arm": "C"}')
        assert call_log.call_tags() == {"arm": "B"}


def _create_message_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_message"
            and ast.unparse(node.func.value).endswith("llm_client")
        ):
            yield node


# What each file's model calls are for. A new call site fails the test below
# until it is added here with its purpose.
_EXPECTED_PURPOSES = {
    "base.py": {"TURN"},
    "persona_runtime/action_loop.py": {"TURN"},
    "salience_bid.py": {"BID"},
    "persona_runtime/reflexion.py": {"CRITIC", "REVISE"},
    "persona_runtime/summarize_close.py": {"SUMMARY"},
    "memory/episodic_retention.py": {"SUMMARY"},
    "memory/working.py": {"COMPRESS"},
}


def test_every_runtime_call_site_names_its_purpose():
    """An untagged call would reach the log with a null purpose, and the
    harness refuses such a line, so every call site must pick one."""
    found: dict[str, set[str]] = {}
    for path in sorted(_AGENTS.rglob("*.py")):
        if "tests" in path.parts or "generated" in path.parts:
            continue
        for call in _create_message_calls(ast.parse(path.read_text())):
            where = path.relative_to(_AGENTS).as_posix()
            purpose = next((kw.value for kw in call.keywords if kw.arg == "purpose"), None)
            name = (
                purpose.attr
                if isinstance(purpose, ast.Attribute)
                and ast.unparse(purpose.value) == "CallPurpose"
                else f"<{ast.unparse(purpose) if purpose else 'missing'} at line {call.lineno}>"
            )
            found.setdefault(where, set()).add(name)
    assert found == _EXPECTED_PURPOSES


def test_bad_tags_stop_the_agent_at_startup(monkeypatch):
    """A malformed tag setting must fail the boot, not every later call."""
    from agents import server_cli
    from agents.model_aliases import use_alias_map

    priced = {"quality": {"provider": "mock", "model": "mock", "input_per_1m_tokens": 0.0,
                          "output_per_1m_tokens": 0.0}}
    monkeypatch.setenv(call_log.CALL_LOG_ENV, "/dev/null")
    monkeypatch.setenv(call_log.CALL_TAGS_ENV, "not json")
    call_log.reset_call_log()
    try:
        with use_alias_map(priced), pytest.raises(SystemExit, match=call_log.CALL_TAGS_ENV):
            server_cli._validate_startup_config()
    finally:
        call_log.reset_call_log()
