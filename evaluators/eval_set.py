"""RFC 0044 Phase 1 — eval-set recipe loader + assertion evaluation.

``load_eval_set`` parses a recipe YAML (validated against
``schemas/eval_set.json``) into typed dataclasses and enforces the RFC 0044 §D
structural rule (``match: exact`` is never used for stochastic ``assistant:``
content). ``evaluate`` runs a recipe's assertions against an observed
:class:`~evaluators.assertions.EvalRun` and returns a per-assertion report.

The replay runner that *produces* an ``EvalRun`` from a recipe (executing the
turns against a recorded-response LLM client) is a subsequent PR; this module is
the format + assertion half of Phase 1 and stands alone with no persona-runtime
or LLM dependency.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import yaml

from evaluators.assertions import (
    NUMERIC_OPS,
    AssertionResult,
    EvalRun,
    MatchOp,
    match_content,
    match_event_count,
    match_event_sequence,
    match_exact,
    match_numeric,
)

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "eval_set.json"


# ─── Recipe model ───────────────────────────────────────────────────────────


@dataclass
class ContentAssertion:
    """A content matcher (assistant turn or a ``final_transcript`` entry)."""

    op: MatchOp
    value: str | None = None
    values: list[str] | None = None


@dataclass
class StateMatcher:
    """A matcher over a single terminal-state value."""

    op: MatchOp
    value: Any = None


@dataclass
class EventAssertion:
    """A per-turn event expectation. ``type`` is the event-type name; any extra
    keys (``scope``, ``kind``, ``key_pattern``, …) ride in ``fields`` for the
    runner PR that consumes the typed-event stream (RFC 0041).

    Phase 1 note: these are parsed and stored so recipes can be authored ahead
    of the runner, but :func:`evaluate` does **not** check them — a recipe with
    per-turn ``events`` is asserting nothing that runs today. Event checks that
    run now live in the top-level ``event_count`` / ``event_sequence`` block,
    evaluated against the flat :attr:`EvalRun.events` stream."""

    type: str
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    user_text: str | None = None
    expect: ContentAssertion | None = None
    events: list[EventAssertion] = field(default_factory=list)


@dataclass
class Interaction:
    id: str
    turns: list[Turn]
    elapsed: str | None = None


@dataclass
class Setup:
    persona: str
    user: str | None = None
    channel: str | None = None
    session_id: str | None = None
    seed_state: dict[str, Any] = field(default_factory=dict)
    llm_mode: str = "replay"


@dataclass
class Assertions:
    terminal_state: dict[str, StateMatcher] = field(default_factory=dict)
    event_count: dict[str, int] = field(default_factory=dict)
    event_sequence: list[str] | None = None
    final_transcript: list[ContentAssertion] = field(default_factory=list)


@dataclass
class EvalSet:
    id: str
    title: str
    setup: Setup
    interactions: list[Interaction]
    assertions: Assertions
    tier: str = "experimental"
    description: str | None = None
    spawned_from: str | None = None


@dataclass
class EvalReport:
    """The outcome of evaluating one recipe against one run."""

    eval_id: str
    results: list[AssertionResult]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    def failures(self) -> list[AssertionResult]:
        return [r for r in self.results if not r.passed]


# ─── Loading ────────────────────────────────────────────────────────────────


def _load_schema() -> dict[str, Any]:
    with _SCHEMA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_eval_set(path: str | Path) -> EvalSet:
    """Parse and validate a recipe file, returning a typed :class:`EvalSet`.

    Raises :class:`ValueError` on any malformed recipe — schema violations and
    the RFC 0044 §D ``exact``-on-content rule alike — so a single ``except
    ValueError`` at the call site catches every load failure.
    """
    with Path(path).open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"eval-set must be a mapping, got {type(data).__name__}")

    try:
        jsonschema.validate(instance=data, schema=_load_schema())
    except jsonschema.ValidationError as exc:
        raise ValueError(f"eval-set schema validation failed: {exc.message}") from exc

    setup = _parse_setup(data["setup"])
    interactions = [_parse_interaction(i) for i in data["interactions"]]
    assertions = _parse_assertions(data.get("assertions", {}))

    return EvalSet(
        id=data["id"],
        title=data["title"],
        setup=setup,
        interactions=interactions,
        assertions=assertions,
        tier=data.get("tier", "experimental"),
        description=data.get("description"),
        spawned_from=data.get("spawned_from"),
    )


def _parse_setup(raw: dict[str, Any]) -> Setup:
    return Setup(
        persona=raw["persona"],
        user=raw.get("user"),
        channel=raw.get("channel"),
        session_id=raw.get("session_id"),
        seed_state=dict(raw.get("seed_state") or {}),
        llm_mode=raw.get("llm_mode", "replay"),
    )


def _parse_interaction(raw: dict[str, Any]) -> Interaction:
    return Interaction(
        id=raw["id"],
        elapsed=raw.get("elapsed"),
        turns=[_parse_turn(t) for t in raw["turns"]],
    )


def _parse_turn(raw: dict[str, Any]) -> Turn:
    if "user" in raw:
        return Turn(role="user", user_text=raw["user"])

    spec = raw["assistant"]
    op = MatchOp(spec["match"])
    if op is MatchOp.EXACT:
        # RFC 0044 §D — LLM output is not byte-stable, so `exact` is banned on
        # assistant content. State values may still use it (see StateMatcher).
        raise ValueError(
            "RFC 0044 §D: `match: exact` is not allowed on assistant content "
            "(LLM output is not byte-stable) — use contains / must_reference / regex"
        )
    value, values = spec.get("value"), spec.get("values")
    _validate_content_operand(op, value, values, where="assistant turn")
    events = [_parse_event(e) for e in (raw.get("events") or [])]
    return Turn(
        role="assistant",
        expect=ContentAssertion(op=op, value=value, values=values),
        events=events,
    )


def _validate_content_operand(
    op: MatchOp, value: str | None, values: list[str] | None, *, where: str
) -> None:
    """Enforce that a content operator carries its required operand.

    A missing operand (``{match: contains}`` with no ``value``, ``must_reference``
    with no ``values``) would otherwise coalesce to a vacuously-passing assertion
    — a regression check that can never fail. A mis-keyed operand (``values`` on a
    scalar operator) is rejected for the same reason. The regex pattern is
    compiled here so an author's bad pattern fails loudly at load time rather than
    raising mid-evaluation.
    """
    if op in (MatchOp.MUST_REFERENCE, MatchOp.MUST_NOT_REFERENCE):
        if not values:
            raise ValueError(f"{where}: `{op.value}` requires a non-empty `values` list")
        if value is not None:
            raise ValueError(f"{where}: `{op.value}` takes `values`, not `value`")
        return
    # contains / regex — `exact` is rejected for content before this call.
    if not value:
        raise ValueError(f"{where}: `{op.value}` requires a non-empty `value` string")
    if values is not None:
        raise ValueError(f"{where}: `{op.value}` takes `value`, not `values`")
    if op is MatchOp.REGEX:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"{where}: `regex` value is not a valid pattern ({exc})") from exc


def _parse_event(raw: dict[str, Any]) -> EventAssertion:
    extra = {k: v for k, v in raw.items() if k != "type"}
    return EventAssertion(type=raw["type"], fields=extra)


def _parse_assertions(raw: dict[str, Any]) -> Assertions:
    terminal_state = {
        key: StateMatcher(op=MatchOp(spec["match"]), value=spec.get("value"))
        for key, spec in (raw.get("terminal_state") or {}).items()
    }
    final_transcript: list[ContentAssertion] = []
    for op_name, payload in (raw.get("final_transcript") or {}).items():
        op = MatchOp(op_name)
        if op in (MatchOp.MUST_REFERENCE, MatchOp.MUST_NOT_REFERENCE):
            values = list(payload)
            _validate_content_operand(op, None, values, where="final_transcript")
            final_transcript.append(ContentAssertion(op=op, values=values))
        else:  # contains / regex → scalar
            _validate_content_operand(op, payload, None, where="final_transcript")
            final_transcript.append(ContentAssertion(op=op, value=payload))

    return Assertions(
        terminal_state=terminal_state,
        event_count=dict(raw.get("event_count") or {}),
        event_sequence=raw.get("event_sequence"),
        final_transcript=final_transcript,
    )


# ─── Evaluation ─────────────────────────────────────────────────────────────


def evaluate(eval_set: EvalSet, run: EvalRun) -> EvalReport:
    """Run every assertion in ``eval_set`` against ``run`` and collect results.

    Per-turn ``assistant`` expectations are aligned positionally with
    ``run.turn_outputs`` (the runner emits one output per assistant turn, in
    order). The top-level ``assertions`` block runs against the whole run.

    Phase 1 scope: per-turn ``Turn.events`` are *not* evaluated here — the flat
    event stream is covered only by the top-level ``event_count`` /
    ``event_sequence`` assertions. Per-turn typed-event checking lands with the
    runner (RFC 0041); until then a per-turn ``events`` block asserts nothing.
    """
    results: list[AssertionResult] = []

    # Per-turn assistant content expectations.
    assistant_turns = [t for i in eval_set.interactions for t in i.turns if t.role == "assistant"]
    for idx, turn in enumerate(assistant_turns):
        if turn.expect is None:
            continue
        name = f"turn[{idx}].{turn.expect.op.value}"
        if idx >= len(run.turn_outputs):
            results.append(
                AssertionResult(name, False, "no assistant output produced for this turn")
            )
            continue
        ok, detail = match_content(
            turn.expect.op,
            run.turn_outputs[idx],
            value=turn.expect.value,
            values=turn.expect.values,
        )
        results.append(AssertionResult(name, ok, detail))

    # final_transcript content assertions over the joined assistant text.
    for ca in eval_set.assertions.final_transcript:
        ok, detail = match_content(ca.op, run.transcript, value=ca.value, values=ca.values)
        results.append(AssertionResult(f"final_transcript.{ca.op.value}", ok, detail))

    # terminal_state per-key matchers.
    for key, matcher in eval_set.assertions.terminal_state.items():
        actual = run.terminal_state.get(key)
        if matcher.op in NUMERIC_OPS:
            ok, detail = match_numeric(matcher.op, actual, matcher.value)
        else:  # exact — strict equality with the bool↔number firewall.
            ok, detail = match_exact(actual, matcher.value)
        results.append(AssertionResult(f"terminal_state.{key}", ok, detail))

    # event assertions.
    if eval_set.assertions.event_count:
        results.extend(match_event_count(eval_set.assertions.event_count, run.events))
    if eval_set.assertions.event_sequence:
        results.append(match_event_sequence(eval_set.assertions.event_sequence, run.events))

    return EvalReport(eval_id=eval_set.id, results=results)
