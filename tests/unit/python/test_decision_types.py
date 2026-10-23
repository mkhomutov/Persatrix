"""The decision contract's types (RFC 0028 §A and §D; PR plan PR 1, E-4 and E-5).

These pin the vocabulary every later decision PR builds on: §A's six action
classes and four checkpoints, a class for every persona action and every
task-agent step, frozen types, and the one JSON line a record becomes — the
decision log's line and the replay harness's input, so it must come back
unchanged.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence

import pytest

from agents.decision import (
    ACTION_TYPE_CLASSES,
    STOP_REASON_CLASSES,
    ActionClass,
    Checkpoint,
    DecisionCandidate,
    DecisionContext,
    DecisionOutcome,
    DecisionPolicy,
    DecisionRecord,
    DecisionSubject,
    DirectiveVerdict,
    Rejection,
    class_of_action,
    class_of_step,
    hash_content,
)
from agents.llm_types import StopReason
from agents.persona_types import ActionType

# ─── The closed vocabularies ───────────────────────────────


def test_action_classes_are_section_a_six() -> None:
    assert {c.value for c in ActionClass} == {
        "respond",
        "ask_clarification",
        "tool_call",
        "delegate",
        "publish_channel",
        "defer",
    }


def test_checkpoints_are_section_a_four() -> None:
    assert {c.value for c in Checkpoint} == {
        "pre_act",
        "pre_tool",
        "pre_delegate",
        "post_outcome",
    }


def test_a_record_decides_an_action_or_a_directive() -> None:
    assert {s.value for s in DecisionSubject} == {"action", "directive"}
    assert {v.value for v in DirectiveVerdict} == {"accept", "adapt", "refuse"}


# ─── E-4: every action and every step has a class ──────────

EXPECTED_ACTION_CLASSES = {
    ActionType.SEND_CHANNEL_MESSAGE: ActionClass.PUBLISH_CHANNEL,
    ActionType.COMPLETE_TASK: ActionClass.RESPOND,
    ActionType.DELEGATE: ActionClass.DELEGATE,
    ActionType.SPAWN_SUB_AGENT: ActionClass.DELEGATE,
    ActionType.USE_TOOL: ActionClass.TOOL_CALL,
    ActionType.REQUEST_APPROVAL: ActionClass.ASK_CLARIFICATION,
    ActionType.GRANT_APPROVAL: ActionClass.RESPOND,
    ActionType.DENY_APPROVAL: ActionClass.RESPOND,
    ActionType.DO_NOTHING: ActionClass.DEFER,
    ActionType.END_INTERACTION_VOTE: ActionClass.PUBLISH_CHANNEL,
}


def test_every_action_type_member_has_a_class() -> None:
    # A new ActionType member fails here until it is given a class.
    assert set(ACTION_TYPE_CLASSES) == set(ActionType)


@pytest.mark.parametrize(("action_type", "expected"), list(EXPECTED_ACTION_CLASSES.items()))
def test_action_type_class(action_type: ActionType, expected: ActionClass) -> None:
    assert class_of_action(action_type) is expected


def test_every_task_agent_step_has_a_class() -> None:
    assert set(STOP_REASON_CLASSES) == set(StopReason)
    assert class_of_step(StopReason.END_TURN) is ActionClass.RESPOND
    assert class_of_step(StopReason.TOOL_USE) is ActionClass.TOOL_CALL
    assert class_of_step(StopReason.MAX_TOKENS) is ActionClass.DEFER


def test_the_class_tables_are_read_only() -> None:
    with pytest.raises(TypeError):
        ACTION_TYPE_CLASSES[ActionType.DO_NOTHING] = ActionClass.RESPOND  # type: ignore[index]


# ─── Builders ──────────────────────────────────────────────


def _context(**overrides: object) -> DecisionContext:
    values: dict[str, object] = {"checkpoint": Checkpoint.PRE_ACT, "agent_id": "ember-owl"}
    values.update(overrides)
    return DecisionContext(**values)  # type: ignore[arg-type]


def _publish(candidate_id: str = "c0", text: str = "hello") -> DecisionCandidate:
    return DecisionCandidate(
        candidate_id=candidate_id,
        action_class=ActionClass.PUBLISH_CHANNEL,
        action=ActionType.SEND_CHANNEL_MESSAGE.value,
        target="general",
        content_hash=hash_content(text),
    )


def _full_record() -> DecisionRecord:
    """A record with every field set away from its default."""
    tool = DecisionCandidate(
        candidate_id="c1",
        action_class=ActionClass.TOOL_CALL,
        action=ActionType.USE_TOOL.value,
        tool_name="web_search",
        argument_names=("query", "limit"),
        content_hash=hash_content('{"query": "x", "limit": 3}'),
    )
    return DecisionRecord(
        record_id="r-1",
        recorded_at=1_790_000_000.25,
        context=_context(
            agent_type="persona",
            event_type="channel_message",
            event_id="m-42",
            workflow_id="wf-7",
            channel_id="general",
            protection_level="internal",
            autonomy_level="reactive",
            mood="curious",
        ),
        candidates=(_publish(), tool),
        outcome=DecisionOutcome(
            action_class=ActionClass.PUBLISH_CHANNEL,
            selected_id="c0",
            reason="runtime_proposal",
            confidence=0.75,
            rejected=(Rejection(candidate_id="c1", reason="permission_denied"),),
            rationale="the runtime's proposal ranks first",
        ),
    )


# ─── Frozen types and the JSON line ────────────────────────


def test_every_type_is_frozen() -> None:
    record = _full_record()
    for value in (record, record.context, record.candidates[0], record.outcome,
                  record.outcome.rejected[0]):
        name = dataclasses.fields(value)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, name, None)


def test_a_record_is_one_json_line_and_round_trips() -> None:
    record = _full_record()
    line = record.to_json()
    assert "\n" not in line
    assert json.loads(line)["v"] == 1
    again = DecisionRecord.from_json(line)
    assert again == record
    assert again.to_json() == line


def test_a_minimal_record_round_trips() -> None:
    record = DecisionRecord(
        context=_context(),
        candidates=(),
        outcome=DecisionOutcome(action_class=ActionClass.DEFER, reason="no_candidate"),
    )
    assert DecisionRecord.from_json(record.to_json()) == record


def test_a_directive_record_round_trips() -> None:
    record = DecisionRecord(
        subject=DecisionSubject.DIRECTIVE,
        subject_id="dir-9",
        context=_context(),
        candidates=(),
        outcome=DecisionOutcome(action_class=None, verdict=DirectiveVerdict.ADAPT),
    )
    assert DecisionRecord.from_json(record.to_json()) == record


def _reversed_keys(value: object) -> object:
    if isinstance(value, dict):
        return {k: _reversed_keys(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reversed_keys(item) for item in value]
    return value


def test_the_line_does_not_depend_on_key_order() -> None:
    line = _full_record().to_json()
    shuffled = json.dumps(_reversed_keys(json.loads(line)), separators=(",", ":"))
    assert shuffled != line
    assert DecisionRecord.from_json(shuffled).to_json() == line


def test_new_records_get_distinct_ids() -> None:
    def make() -> DecisionRecord:
        return DecisionRecord(
            context=_context(),
            candidates=(),
            outcome=DecisionOutcome(action_class=ActionClass.DEFER, reason="no_candidate"),
        )

    assert make().record_id != make().record_id


def _mutated(path: Sequence[str | int], value: object) -> str:
    data = json.loads(_full_record().to_json())
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return json.dumps(data)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["v"], 2),
        (["surprise"], 1),
        (["context", "surprise"], 1),
        (["context", "checkpoint"], "pre_lunch"),
        (["candidates", 0, "action_class"], "shout"),
        (["outcome", "confidence"], "high"),
        (["outcome", "confidence"], True),
        (["candidates", 0, "argument_names"], "query"),
        (["context", "agent_id"], 7),
    ],
)
def test_from_json_refuses_a_malformed_line(path: list[str | int], value: object) -> None:
    with pytest.raises(ValueError):
        DecisionRecord.from_json(_mutated(path, value))


def test_from_json_refuses_a_missing_required_field() -> None:
    data = json.loads(_full_record().to_json())
    del data["context"]["agent_id"]
    with pytest.raises(ValueError):
        DecisionRecord.from_json(json.dumps(data))


@pytest.mark.parametrize("key", ["record_id", "recorded_at"])
def test_from_json_does_not_invent_an_id_or_a_time(key: str) -> None:
    # A default would give the same line a new id, or the reader's clock,
    # on every read.
    data = json.loads(_full_record().to_json())
    del data[key]
    with pytest.raises(ValueError):
        DecisionRecord.from_json(json.dumps(data))


def test_an_older_line_without_a_defaulted_field_reads_with_the_default() -> None:
    data = json.loads(_full_record().to_json())
    del data["outcome"]["rationale"]
    del data["context"]["mood"]
    record = DecisionRecord.from_json(json.dumps(data))
    assert record.outcome.rationale == ""
    assert record.context.mood is None


# ─── Numbers: one spelling, so the line reads back identical ───


def test_integer_numbers_are_stored_as_floats() -> None:
    record = DecisionRecord(
        recorded_at=1_790_000_000,
        context=_context(),
        candidates=(),
        outcome=DecisionOutcome(ActionClass.DEFER, reason="x", confidence=1),
    )
    assert isinstance(record.recorded_at, float)
    assert isinstance(record.outcome.confidence, float)
    line = record.to_json()
    assert DecisionRecord.from_json(line).to_json() == line


@pytest.mark.parametrize("value", [True, float("inf"), float("nan")])
def test_a_time_is_a_finite_number(value: float) -> None:
    with pytest.raises(ValueError):
        DecisionRecord(
            recorded_at=value,
            context=_context(),
            candidates=(),
            outcome=DecisionOutcome(ActionClass.DEFER, reason="x"),
        )


# ─── E-10: text never enters a record ──────────────────────


def test_a_candidate_carries_a_hash_not_its_text() -> None:
    record = DecisionRecord(
        context=_context(),
        candidates=(_publish(text="the launch is on Friday"),),
        outcome=DecisionOutcome(action_class=ActionClass.PUBLISH_CHANNEL, selected_id="c0"),
    )
    assert "launch" not in record.to_json()
    assert record.candidates[0].content_hash == hash_content("the launch is on Friday")


def test_any_python_string_can_be_hashed() -> None:
    # A reply cut mid-emoji can hold a lone surrogate, which strict UTF-8
    # refuses to encode.
    assert hash_content("a\ud83d") != hash_content("a")
    assert len(hash_content("a\ud83d")) == 64


def test_content_hash_must_be_a_sha256_digest() -> None:
    with pytest.raises(ValueError):
        DecisionCandidate(
            candidate_id="c0",
            action_class=ActionClass.RESPOND,
            action="complete_task",
            content_hash="the launch is on Friday",
        )


# ─── A record's invariants ─────────────────────────────────


def _record(outcome: DecisionOutcome, candidates: tuple[DecisionCandidate, ...] = (),
            **fields: object) -> DecisionRecord:
    return DecisionRecord(context=_context(), candidates=candidates, outcome=outcome,
                          **fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("outcome", "candidates", "fields"),
    [
        # The selection must name a candidate, of the class the outcome says.
        (DecisionOutcome(ActionClass.PUBLISH_CHANNEL, selected_id="c9"), (_publish(),), {}),
        (DecisionOutcome(ActionClass.RESPOND, selected_id="c0"), (_publish(),), {}),
        # A rejection names a candidate, never the selected one, once.
        (DecisionOutcome(ActionClass.DEFER, reason="x", rejected=(Rejection("c9", "x"),)),
         (_publish(),), {}),
        (DecisionOutcome(ActionClass.PUBLISH_CHANNEL, selected_id="c0",
                         rejected=(Rejection("c0", "x"),)), (_publish(),), {}),
        (DecisionOutcome(ActionClass.DEFER, reason="x",
                         rejected=(Rejection("c0", "a"), Rejection("c0", "b"))),
         (_publish(),), {}),
        # Candidate ids are unique.
        (DecisionOutcome(ActionClass.PUBLISH_CHANNEL, selected_id="c0"),
         (_publish(), _publish()), {}),
        # §C: when nothing is selected the outcome is defer, with its reason.
        (DecisionOutcome(ActionClass.RESPOND, reason="x"), (), {}),
        (DecisionOutcome(ActionClass.DEFER), (), {}),
        # E-5: only a directive record carries a verdict ...
        (DecisionOutcome(ActionClass.DEFER, reason="x", verdict=DirectiveVerdict.ACCEPT),
         (), {}),
        # ... names its directive ...
        (DecisionOutcome(None, verdict=DirectiveVerdict.ACCEPT),
         (), {"subject": DecisionSubject.DIRECTIVE}),
        # ... and either reaches a verdict, with no action class of its own,
        # or defers with a reason and no verdict.
        (DecisionOutcome(ActionClass.PUBLISH_CHANNEL, verdict=DirectiveVerdict.ACCEPT), (),
         {"subject": DecisionSubject.DIRECTIVE, "subject_id": "dir-9"}),
        (DecisionOutcome(ActionClass.DEFER, reason="authority_tie",
                         verdict=DirectiveVerdict.ACCEPT), (),
         {"subject": DecisionSubject.DIRECTIVE, "subject_id": "dir-9"}),
        (DecisionOutcome(None), (),
         {"subject": DecisionSubject.DIRECTIVE, "subject_id": "dir-9"}),
        (DecisionOutcome(ActionClass.DEFER), (),
         {"subject": DecisionSubject.DIRECTIVE, "subject_id": "dir-9"}),
        # An action record has an action class and names no directive.
        (DecisionOutcome(None, reason="x"), (), {}),
        (DecisionOutcome(ActionClass.DEFER, reason="x"), (), {"subject_id": "dir-9"}),
    ],
)
def test_an_inconsistent_record_is_refused(
    outcome: DecisionOutcome,
    candidates: tuple[DecisionCandidate, ...],
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _record(outcome, candidates, **fields)


def test_a_tied_directive_defers_with_its_reason() -> None:
    record = _record(
        DecisionOutcome(ActionClass.DEFER, reason="authority_tie"),
        subject=DecisionSubject.DIRECTIVE,
        subject_id="dir-9",
    )
    assert record.outcome.verdict is None


@pytest.mark.parametrize("confidence", [-0.1, 1.5, float("nan"), float("inf"), True])
def test_confidence_is_a_probability(confidence: float) -> None:
    with pytest.raises(ValueError):
        DecisionOutcome(ActionClass.DEFER, reason="x", confidence=confidence)


# ─── The policy protocol ───────────────────────────────────


class _FirstCandidate:
    def select(
        self, context: DecisionContext, candidates: Sequence[DecisionCandidate]
    ) -> DecisionOutcome:
        if not candidates:
            return DecisionOutcome(ActionClass.DEFER, reason="no_candidate")
        first = candidates[0]
        return DecisionOutcome(first.action_class, selected_id=first.candidate_id)


def test_a_policy_is_anything_with_select() -> None:
    policy: DecisionPolicy = _FirstCandidate()
    assert isinstance(policy, DecisionPolicy)
    outcome = policy.select(_context(), (_publish(),))
    record = DecisionRecord(context=_context(), candidates=(_publish(),), outcome=outcome)
    assert record.outcome.selected_id == "c0"
