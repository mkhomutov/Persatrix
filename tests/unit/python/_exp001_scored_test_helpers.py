"""Shared pieces of the EXP-001 scored run's tests: the stand-ins for each
arm's hold and for the judge, the harness's clock, and one call that starts
a scored run in a test's directory.

Extracted so ``test_exp001_scored.py`` (holding the run, its faults, cap and
window) and ``test_exp001_scored_stops.py`` (what the run records when it
stops) share them without either file passing the 500-line review cap. The
pattern mirrors ``_exp001_run_test_helpers.py``: a private module beside the
test files, imported by name.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agents.llm_client import LLMClient, LLMResponse, Usage
from agents.llm_types import LLMToolResult
from evaluators.exp001.attempts import Held, Hold, try_directory
from evaluators.exp001.costs import ARMS, ARMS_MODEL
from evaluators.exp001.deployed_meeting import CALL_LOG
from evaluators.exp001.deployment import ARMS_ALIAS, Alias
from evaluators.exp001.materials import Meeting, Series
from evaluators.exp001.packets import load_adviser_names
from evaluators.exp001.scored import RUN, run_scored

from ._exp001_judge_test_helpers import PROMPTS, _memo_json
from ._exp001_run_test_helpers import EXP, PANEL, SCORED, T0, arm_a_reply, channel_meeting, no_wait

__all__ = [
    "NAMES",
    "_BINARY",
    "_EVERY_TRY",
    "_OFFLINE",
    "_RATE_LIMITED",
    "_TOKENS",
    "_Clock",
    "_Crash",
    "_Holds",
    "_Judge",
    "_failing",
    "_scored",
    "_state",
]

NAMES = load_adviser_names(EXP / "panel.yaml")
_BINARY = Path("/repo/bin/persatrix-server")
_OFFLINE = Alias("mock", "offline", 0, 0)
_EVERY_TRY = sum(len(s.meetings) for s in SCORED) * len(ARMS)  # 150, when none fails
_RATE_LIMITED: Held[Any] = Held(None, errors=("RateLimitError",))
# A try that logs this many output tokens on the arms' model costs $0.15.
_TOKENS = 10_000

TryKey = tuple[str, str, str, int, int]  # arm, series, meeting, attempt, try


class _Crash(BaseException):
    """Stands in for Ctrl-C, a hangup or a crash: no Exception, so no fault."""


class _Clock:
    """The harness's real time, moved on only by the holds."""

    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> dt.datetime:
        return self.at


class _Judge:
    """Answers every packet readably, as the judge would, and keeps what it
    was asked. *error* is raised by every call instead, as a provider error
    the retries do not clear would be. *corrupt* is a call log the first
    call leaves a line in that the harness cannot read."""

    name = "anthropic"
    supports_prompt_cache = True

    def __init__(
        self, *, readable: bool = True, error: type[Exception] | None = None,
        corrupt: Path | None = None,
    ) -> None:
        self.asked: list[str] = []
        self.readable, self.error, self.corrupt = readable, error, corrupt

    async def create_message(self, **kwargs: Any) -> LLMResponse:
        packet = kwargs["messages"][0]["content"]
        self.asked.append(packet)
        if self.error is not None:
            raise self.error("the provider is overloaded")
        if self.corrupt is not None:
            with self.corrupt.open("a") as log:
                log.write('{"half a line\n')
            self.corrupt = None
        if not self.readable:
            answer = "I would rather not score this."
        elif kwargs["system"] == PROMPTS.recall:
            questions = re.findall(r"^(R\d+), question", packet, flags=re.MULTILINE)
            answer = json.dumps(dict.fromkeys(questions, "right"))
        else:
            answer = _memo_json(c2=None if "decision: none." in packet else 2)
        return LLMResponse(text=answer, usage=Usage(3000, 900))

    def format_tool_definitions(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tools

    def append_tool_round(
        self, messages: list[Any], response: LLMResponse, tool_results: list[LLMToolResult],
    ) -> list[Any]:
        raise AssertionError("the judge sends no tools")


class _Holds:
    """Stands in for each arm's hold: every meeting held at its first try,
    each try logging one call of *tokens* output tokens on *model*, or on
    the model *models* names for that try. *acts* replaces the try at
    (arm, series, meeting, attempt, try), once it has logged its call, with
    a Held, or raises. Each try moves *clock* on by *step*. The try *paused*
    at waits until ``go_on`` is set."""

    def __init__(
        self, *, tokens: int = _TOKENS, model: str = ARMS_MODEL,
        models: Mapping[TryKey, str] | None = None,
        acts: Mapping[TryKey, Held[Any] | BaseException] | None = None,
        clock: _Clock | None = None, step: dt.timedelta = dt.timedelta(0),
        paused: TryKey | None = None,
    ) -> None:
        self.tokens, self.model, self.models = tokens, model, dict(models or {})
        self.acts = dict(acts or {})
        self.clock, self.step, self.paused = clock, step, paused
        self.go_on = asyncio.Event()
        self.held: list[tuple[str, str, str]] = []

    def __call__(self, arm: str, series: Series, directory: Path) -> Hold[Any]:
        async def hold(meeting: Meeting, attempt: int, meeting_try: int) -> Held[Any]:
            key = (arm, series.id, meeting.id, attempt, meeting_try)
            if key == self.paused:
                await self.go_on.wait()
            self.held.append((series.id, arm, meeting.id))
            if self.clock is not None:
                self.clock.at += self.step
            self._log(key, meeting, directory)
            act = self.acts.get(key)
            if isinstance(act, BaseException):
                raise act
            if act is not None:
                return act
            if arm == "A":
                return Held(arm_a_reply(meeting, f"A at {meeting.id}.", series=series))
            return Held(channel_meeting(meeting, attempt, meeting_try, arm=arm, series=series,
                                        memo=f"{arm}'s chair at {meeting.id}."))
        return hold

    def _log(self, key: TryKey, meeting: Meeting, directory: Path) -> None:
        """One call, where the arm's own hold logs it: arm A's series to one
        file, each channel-arm try to its own."""
        arm, series, _, attempt, meeting_try = key
        path = directory / CALL_LOG if arm == "A" else (
            try_directory(directory, attempt, meeting, meeting_try) / CALL_LOG
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "tags": {"arm": arm, "series": series, "meeting": meeting.id,
                     "meeting_kind": meeting.kind.value, "attempt": str(attempt),
                     "try": str(meeting_try)},
            "agent_id": PANEL.chair.id, "purpose": "turn", "provider": "anthropic",
            "model": self.models.get(key, self.model), "model_alias": "quality",
            "started_at": T0.isoformat(), "input_tokens": 0, "output_tokens": self.tokens,
            "cache_write_tokens": 0, "cache_read_tokens": 0, "cache_prefix_sha256": None,
            "error": None,
        }
        with path.open("a") as log:
            log.write(json.dumps(line) + "\n")


def _failing(arm: str, series: str, meeting: str) -> dict[TryKey, Any]:
    """*arm*'s *meeting* cut short at every try of both attempts: the series is dropped."""
    return {(arm, series, meeting, attempt, n): _RATE_LIMITED
            for attempt in (1, 2) for n in range(1, 5)}


async def _scored(
    root: Path, holds: _Holds, judge: _Judge | None, *, alias: Alias = ARMS_ALIAS,
    fixed_by: str | None = None, clock: _Clock | None = None,
    series: tuple[Series, ...] = SCORED,
) -> dict[str, Any]:
    return await run_scored(
        root, panel=PANEL, series=series, names=NAMES, client=LLMClient(judge or _Judge()),
        binary=_BINARY, alias=alias, prompts=None if judge is None else PROMPTS,
        fixed_by=fixed_by, sleep=no_wait, now=clock or _Clock(), make_hold=holds,
    )


def _state(root: Path) -> dict[str, Any]:
    return dict(json.loads((root / RUN).read_text()))
