"""EXP-001 harness — arm A, one model call per meeting (PR 4).

Arm A asks one model to play all four advisers and reply once, as the panel;
nothing carries from one meeting to the next. Pre-registration §3 asks two
things of it before any scored meeting. Check 7: its clock line shows the
meeting's date, as every adviser's does. Check 8: its prompt holds each
adviser's identity exactly as the persona agents' prompts do. So the harness
renders the identities with the persona runtime's own prompt code, and these
tests build each adviser as a persona agent to compare the two.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agents import call_log
from agents.clock import FrozenClock
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.llm_types import LLMToolResult
from agents.persona import create_persona_agent
from agents.persona_runtime.action_loop import _PERSONA_DEFAULT_MAX_TOKENS
from agents.persona_runtime.prompt_assembly import render_persona_sections
from evaluators.exp001.arm_a import (
    MAX_TOKENS,
    identity_sections,
    now_anchor_line,
    run_meeting,
    system_prompt,
)
from evaluators.exp001.costs import ARMS_MODEL, CallPurpose
from evaluators.exp001.materials import Meeting, MeetingKind, load_series
from evaluators.exp001.panel import adviser_agent_config, load_panel
from evaluators.exp001.runtime import read_call_log, story_start

from ._persona_test_helpers import _make_client

_EXP = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"
PANEL = load_panel(_EXP / "panel.yaml")
SERIES = load_series(_EXP / "series-1.yaml")


def _first(kind: MeetingKind) -> Meeting:
    return next(m for m in SERIES.meetings if m.kind is kind)


class TestNowAnchorLine:
    def test_it_reads_ten_o_clock_on_the_story_date(self) -> None:
        assert now_anchor_line(dt.date(2036, 10, 13)) == (
            "Current time: 2036-10-13T10:00:00+00:00 (Monday late morning)."
        )


class TestSystemPrompt:
    def test_it_fills_the_panel_template_and_ends_with_the_instruction(self) -> None:
        plan = _first(MeetingKind.PLAN)
        blocks = "\n\n".join("\n\n".join(identity_sections(a)) for a in PANEL.advisers)
        assert system_prompt(PANEL, SERIES, plan) == (
            f"{now_anchor_line(plan.story_date)}\n"
            "You are an advisory panel of four working for Linden Loaf, a family bakery "
            "with four shops and a central bakehouse. The\n"
            "advisers are:\n"
            f"{blocks}\n"
            "\n"
            "Consider the message from each adviser's point of view, then reply\n"
            "once, as the panel.\n"
            "\n"
            f"{PANEL.memo_format}"
        )

    def test_a_filled_value_is_never_filled_again(self) -> None:
        series = replace(SERIES, organisation="Acme {adviser_identity_blocks}")
        prompt = system_prompt(PANEL, series, _first(MeetingKind.PLAN))
        assert "working for Acme {adviser_identity_blocks}. The\n" in prompt

    @pytest.mark.parametrize("kind", list(MeetingKind))
    def test_each_meeting_kind_ends_with_its_own_instruction(self, kind: MeetingKind) -> None:
        prompt = system_prompt(PANEL, SERIES, _first(kind))
        assert prompt.endswith("\n\n" + PANEL.arm_a_instruction(kind))

    def test_the_identities_come_in_panel_order(self) -> None:
        prompt = system_prompt(PANEL, SERIES, _first(MeetingKind.BRIEFING))
        starts = [prompt.index(f"You are {a.name}.\n") for a in PANEL.advisers]
        assert starts == sorted(starts)


class TestTheAdvisersReadAsTheirPersonaAgentsDo:
    """Checks 7 and 8: each adviser, built as a persona agent from the same
    config the channel arms deploy, on its clock at the meeting's start."""

    async def _persona_view(self, adviser_index: int, at: dt.datetime) -> tuple[str, list[str]]:
        """The persona agent's prompt, and every section in it that says who it is:
        all its persona sections but the grounding and state ones."""
        adviser = PANEL.advisers[adviser_index]
        config = {**adviser_agent_config(adviser), "memory": {"db_path": ":memory:"}}
        agent = create_persona_agent(
            agent_id=adviser.id, config=config, llm_client=_make_client(),
            clock=FrozenClock(at.timestamp()),
        )
        await agent.initialize_memory()
        try:
            seen = (agent.persona, agent._state, agent.name, agent.role)
            left_out = render_persona_sections(*seen, only=("grounding", "current-state"))
            own = [s for s in render_persona_sections(*seen) if s not in left_out]
            return agent._build_system_prompt(), own
        finally:
            await agent.close_memory()

    @pytest.mark.parametrize("index", range(4))
    async def test_every_identity_section_is_the_persona_agents_own(self, index: int) -> None:
        plan = _first(MeetingKind.PLAN)
        persona, own = await self._persona_view(index, story_start(plan.story_date))
        sections = identity_sections(PANEL.advisers[index])
        assert [s.split("\n")[0] for s in sections] == [
            f"You are {PANEL.advisers[index].name}.",
            "Background:",
            "Communication style:",
            "Goals:",
        ]
        assert sections == own
        where = [persona.index(section) for section in sections]
        assert where == sorted(where)
        assert "\n\n".join(sections) in system_prompt(PANEL, SERIES, plan)
        assert now_anchor_line(plan.story_date) in persona

    def test_what_only_a_persona_is_told_stays_out(self) -> None:
        prompt = system_prompt(PANEL, SERIES, _first(MeetingKind.PLAN))
        assert "you are not the user" not in prompt
        assert "Current state:" not in prompt
        assert "Current mood:" not in prompt


class _Provider:
    """Answers each call in turn and keeps what it was sent. A plain text
    answer comes back as a finished reply."""

    name = "anthropic"
    supports_prompt_cache = True  # so a cache prefix, if sent, would show

    def __init__(self, *replies: str | LLMResponse, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._replies = list(replies)
        self._error = error

    async def create_message(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        reply = self._replies.pop(0)
        if isinstance(reply, LLMResponse):
            return reply
        return LLMResponse(text=reply, stop_reason=StopReason.END_TURN, usage=Usage(1200, 300))

    def format_tool_definitions(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tools

    def append_tool_round(
        self, messages: list[Any], response: LLMResponse, tool_results: list[LLMToolResult],
    ) -> list[Any]:
        raise AssertionError("arm A sends no tools")


_ASKED = dt.datetime(2026, 10, 1, 9, 30, tzinfo=dt.UTC)
_ANSWERED = dt.datetime(2026, 10, 1, 9, 30, 41, tzinfo=dt.UTC)


def _clock() -> Any:
    return iter([_ASKED, _ANSWERED]).__next__


@pytest.fixture
def log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(call_log.CALL_LOG_ENV, raising=False)
    call_log.reset_call_log()
    return tmp_path / "calls.jsonl"


class TestRunMeeting:
    async def test_one_call_with_the_meetings_prompt_logged_as_arm_a(self, log_path: Path) -> None:
        plan = _first(MeetingKind.PLAN)
        provider = _Provider("## Recommendation\nOption B.")
        reply = await run_meeting(
            LLMClient(provider), PANEL, SERIES, plan, log_path=log_path, attempt=2, now=_clock(),
        )
        assert provider.calls == [{
            "model": ARMS_MODEL,
            "system": system_prompt(PANEL, SERIES, plan),
            "messages": [{"role": "user", "content": plan.message}],
            "tools": [],
            "max_tokens": MAX_TOKENS,
            "temperature": PANEL.temperature,
        }]
        assert (reply.series, reply.meeting) == ("series-1", plan.id)
        assert reply.text == "## Recommendation\nOption B."
        assert reply.stop_reason is StopReason.END_TURN
        assert not reply.missing
        assert (reply.asked_at, reply.answered_at) == (_ASKED, _ANSWERED)
        [record] = read_call_log(log_path).records
        assert (
            record.arm, record.series, record.meeting, record.meeting_kind, record.attempt,
            record.adviser, record.purpose, record.model, record.input_tokens,
        ) == (
            "A", "series-1", plan.id, MeetingKind.PLAN, 2,
            None, CallPurpose.REPLY, ARMS_MODEL, 1200,
        )
        # The record reads turn, critic and revise alike, and keeps no alias.
        line = json.loads(log_path.read_text())
        assert (line["purpose"], line["model_alias"]) == ("turn", None)

    @pytest.mark.parametrize(("text", "stop_reason"), [
        (None, StopReason.END_TURN),
        (" \n", StopReason.END_TURN),
        ("## Recommendation\nOption B, because", StopReason.MAX_TOKENS),
    ])
    async def test_a_reply_with_no_text_or_cut_off_is_a_missing_memo(
        self, log_path: Path, text: str | None, stop_reason: StopReason,
    ) -> None:
        answer = LLMResponse(text=text, stop_reason=stop_reason, usage=Usage(1200, 4096))
        reply = await run_meeting(
            LLMClient(_Provider(answer)), PANEL, SERIES, _first(MeetingKind.PLAN),
            log_path=log_path, attempt=1,
        )
        assert (reply.text, reply.stop_reason, reply.missing) == (text or "", stop_reason, True)

    async def test_nothing_from_an_earlier_meeting_reaches_the_next(self, log_path: Path) -> None:
        briefing, plan = SERIES.meetings[0], SERIES.meetings[1]
        provider = _Provider("NOTED-THE-LEASE", "MEMO")
        client = LLMClient(provider)
        for meeting in (briefing, plan):
            await run_meeting(client, PANEL, SERIES, meeting, log_path=log_path, attempt=1)
        second = provider.calls[1]
        assert second["messages"] == [{"role": "user", "content": plan.message}]
        assert second["system"] == system_prompt(PANEL, SERIES, plan)
        assert "NOTED-THE-LEASE" not in repr(second)
        assert [r.meeting for r in read_call_log(log_path).records] == [briefing.id, plan.id]

    async def test_a_failed_call_is_logged_then_raised(self, log_path: Path) -> None:
        provider = _Provider(error=ConnectionResetError("reset by peer"))
        with pytest.raises(ConnectionResetError):
            await run_meeting(
                LLMClient(provider), PANEL, SERIES, SERIES.meetings[0],
                log_path=log_path, attempt=1,
            )
        log = read_call_log(log_path)
        assert log.records == ()
        assert [(f.arm, f.adviser, f.error) for f in log.failures] == [
            ("A", None, "ConnectionResetError"),
        ]

    def test_arm_a_may_write_as_long_a_reply_as_an_adviser(self) -> None:
        assert MAX_TOKENS == _PERSONA_DEFAULT_MAX_TOKENS
