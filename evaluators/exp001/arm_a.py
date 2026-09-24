"""Arm A: one model call plays all four advisers and replies once, as the panel.

Nothing carries from one meeting to the next. Each meeting is one call: the
panel's arm A template, then the meeting's instruction, as the system prompt,
and the operator's message, word for word, as the only user message.

The template's adviser identities are rendered by the persona runtime's own
prompt code, from the same agent configs the channel arms deploy, so arm A
reads the words the persona agents read (check 8). Its now-anchor line is the
one an adviser's prompt shows as the meeting begins: 10:00 UTC on the story
date (check 7). The call is logged under the meeting's tags like every other
call (check 4). A call that fails is logged, then raised to the caller.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agents.clock import DEFAULT_TIMEZONE
from agents.llm_client import LLMClient
from agents.llm_types import LLMCallPurpose, StopReason
from agents.persona_runtime.prompt_assembly import render_persona_sections
from agents.persona_types import PersonaState
from agents.prompt_loader import load_persona_section
from agents.temporal.rendering import format_now_anchor
from evaluators.exp001.costs import ARMS_MODEL
from evaluators.exp001.materials import Meeting, Series
from evaluators.exp001.panel import Adviser, Panel, adviser_agent_config
from evaluators.exp001.runtime import call_log_scope, story_start

ARM = "A"
# The persona prompt's sections that say who an adviser is: name, title and
# role, then background, behaviour and goals. The grounding section between
# the first two tells a persona to reply as itself, and the state section
# gives its mood; neither fits a prompt that replies as the panel.
IDENTITY_SECTIONS = ("identity", "background", "behavior", "goals")
# The persona agents' own reply limit; the shipped personas set none.
MAX_TOKENS = 4096


def identity_sections(adviser: Adviser) -> list[str]:
    """The adviser's identity sections, as its persona agent's prompt renders them."""
    config = adviser_agent_config(adviser)
    return render_persona_sections(
        config["persona"], PersonaState(), config["name"], config["role"],
        only=IDENTITY_SECTIONS,
    )


def now_anchor_line(story_date: dt.date) -> str:
    """The now-anchor line an adviser's prompt shows as a meeting on *story_date* begins."""
    at = story_start(story_date).timestamp()
    anchor = format_now_anchor(at, DEFAULT_TIMEZONE)
    return load_persona_section("now-anchor").format_map({"now_anchor": anchor})


def system_prompt(panel: Panel, series: Series, meeting: Meeting) -> str:
    """The panel's template, filled for *meeting*, then the meeting's instruction."""
    blocks = "\n\n".join("\n\n".join(identity_sections(a)) for a in panel.advisers)
    framing = panel.arm_a_template.format_map({
        "current_time_line": now_anchor_line(meeting.story_date),
        "organisation": series.organisation,
        "adviser_identity_blocks": blocks,
    })
    return f"{framing}\n\n{panel.arm_a_instruction(meeting.kind)}"


@dataclass(frozen=True)
class ArmAReply:
    """What arm A answered at one meeting, and when, in real time."""

    series: str
    meeting: str
    text: str
    stop_reason: StopReason
    asked_at: dt.datetime
    answered_at: dt.datetime


def _real_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def run_meeting(
    client: LLMClient,
    panel: Panel,
    series: Series,
    meeting: Meeting,
    *,
    log_path: Path,
    attempt: int,
    now: Callable[[], dt.datetime] = _real_now,
) -> ArmAReply:
    """Hold one meeting of arm A: one call, logged to *log_path* under its tags."""
    system = system_prompt(panel, series, meeting)
    with call_log_scope(
        log_path, arm=ARM, series=series.id, meeting=meeting.id,
        meeting_kind=meeting.kind, attempt=attempt,
    ):
        asked_at = now()
        response = await client.create_message(
            model=ARMS_MODEL,
            system=system,
            messages=[{"role": "user", "content": meeting.message}],
            tools=[],
            max_tokens=MAX_TOKENS,
            temperature=panel.temperature,
            purpose=LLMCallPurpose.TURN,
        )
    return ArmAReply(
        series=series.id,
        meeting=meeting.id,
        text=response.text or "",
        stop_reason=response.stop_reason,
        asked_at=asked_at,
        answered_at=now(),
    )
