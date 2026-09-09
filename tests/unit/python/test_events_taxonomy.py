"""TurnEvent taxonomy tests (RFC 0041 Phase 1, PR 2).

Covers the frozen-dataclass event vocabulary from
[RFC 0041 §A](../../../docs/rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md#a-event-taxonomy):
identity, ordering, immutability, the closed class set, and the two rename /
discriminator decisions the RFC makes deliberately —

* ``ToolCallEvent`` / ``ToolResultEvent`` are *not* ``ToolCall`` / ``ToolResult``
  because both of those names are already taken by the agent runtime.  The
  collision is the whole reason for the rename, so it is pinned here.
* ``CallbackModelOutput`` is a distinct class from ``ModelOutput`` (OQ #3) so a
  channel-publish subscriber cannot mistake a callback's own moderation call
  for the assistant's turn output.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from agents.events import (
    CallbackModelOutput,
    Control,
    Error,
    ErrorKind,
    ModelOutput,
    StateDelta,
    ToolCallEvent,
    ToolErrorKind,
    ToolResultEvent,
    TurnEvent,
    new_event_id,
)
from agents.llm_types import StopReason, Usage

TURN = "turn-abc"
WHEN = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _base(seq: int = 0) -> dict[str, object]:
    return {"event_id": new_event_id(), "turn_id": TURN, "seq": seq, "occurred_at": WHEN}


def _one_of_each() -> list[TurnEvent]:
    """One instance of every concrete event type, in a plausible turn order."""
    return [
        Control(**_base(0), kind="turn_started"),
        ModelOutput(
            **_base(1), role="assistant", content="hi",
            stop_reason=StopReason.TOOL_USE, token_usage=Usage(10, 5),
        ),
        ToolCallEvent(**_base(2), tool_name="recall", args={"q": "x"}, tool_call_id="tc1"),
        ToolResultEvent(**_base(3), tool_call_id="tc1", ok=True, content="result"),
        StateDelta(**_base(4), scope="persona", key="trust.alice", op="set", value=0.5),
        CallbackModelOutput(
            **_base(5), callback_name="moderator", content="ok", token_usage=Usage(1, 1),
        ),
        Error(**_base(6), kind=ErrorKind.INTERNAL, message="boom", retryable=False),
        Control(**_base(7), kind="turn_aborted", reason="internal error"),
    ]


class TestIdentity:
    """RFC 0041 §A — ``event_id`` is the reference key, ``(turn_id, seq)`` orders."""

    def test_new_event_id_is_unique_per_call(self) -> None:
        ids = {new_event_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_new_event_id_is_an_opaque_string(self) -> None:
        eid = new_event_id()
        assert isinstance(eid, str)
        assert eid  # non-empty
        assert "-" not in eid  # uuid4().hex form, not the dashed representation

    def test_every_event_carries_a_distinct_event_id(self) -> None:
        events = _one_of_each()
        assert len({e.event_id for e in events}) == len(events)

    def test_events_in_a_turn_share_turn_id(self) -> None:
        assert {e.turn_id for e in _one_of_each()} == {TURN}


class TestOrdering:
    """``seq`` is the within-turn ordering key (§B)."""

    def test_seq_orders_events_within_a_turn(self) -> None:
        events = _one_of_each()
        shuffled = [events[3], events[0], events[6], events[1]]
        assert [e.seq for e in sorted(shuffled, key=lambda e: e.seq)] == [0, 1, 3, 6]

    def test_turn_opens_and_closes_with_control(self) -> None:
        events = _one_of_each()
        assert isinstance(events[0], Control) and events[0].kind == "turn_started"
        assert isinstance(events[-1], Control) and events[-1].kind == "turn_aborted"

    def test_terminal_error_precedes_turn_aborted(self) -> None:
        # §B: an aborting turn emits exactly one terminal Error before the
        # turn_aborted control event.
        events = _one_of_each()
        err = next(e for e in events if isinstance(e, Error))
        aborted = next(
            e for e in events if isinstance(e, Control) and e.kind == "turn_aborted"
        )
        assert err.seq < aborted.seq


class TestImmutability:
    """Every event is a frozen dataclass — the stream is an audit record."""

    @pytest.mark.parametrize("event", _one_of_each(), ids=lambda e: type(e).__name__)
    def test_events_are_frozen(self, event: TurnEvent) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.seq = 99  # type: ignore[misc]

    @pytest.mark.parametrize("event", _one_of_each(), ids=lambda e: type(e).__name__)
    def test_events_are_dataclasses(self, event: TurnEvent) -> None:
        assert dataclasses.is_dataclass(event)

    def test_events_with_scalar_payloads_are_hashable(self) -> None:
        # Frozen dataclasses generate __hash__; it works when every field is
        # hashable.  Events carrying dict/Usage payloads are NOT hashable (see
        # the companion test) — nothing in the design hashes events, identity
        # is event_id.
        assert hash(Control(**_base(0), kind="turn_started"))
        assert hash(Error(**_base(1), kind=ErrorKind.INTERNAL, message="m", retryable=False))

    def test_events_with_mutable_payloads_are_not_hashable(self) -> None:
        call = ToolCallEvent(**_base(0), tool_name="t", args={"a": 1}, tool_call_id="tc")
        with pytest.raises(TypeError):
            hash(call)


class TestClosedTaxonomy:
    """RFC 0041 Goal 5 — the event set is closed; growth is an RFC-level change."""

    def test_concrete_event_types_are_exactly_the_closed_set(self) -> None:
        assert {c.__name__ for c in TurnEvent.__subclasses__()} == {
            "ModelOutput",
            "ToolCallEvent",
            "ToolResultEvent",
            "StateDelta",
            "Error",
            "Control",
            "CallbackModelOutput",
        }

    def test_every_event_is_a_turnevent(self) -> None:
        assert all(isinstance(e, TurnEvent) for e in _one_of_each())

    def test_turnevent_carries_the_four_common_fields(self) -> None:
        assert [f.name for f in dataclasses.fields(TurnEvent)] == [
            "event_id",
            "turn_id",
            "seq",
            "occurred_at",
        ]


class TestNameCollisionsAvoided:
    """The renames exist to avoid real collisions in the agent runtime (§A)."""

    def test_tool_event_names_differ_from_the_runtime_types(self) -> None:
        from agents.llm_types import ToolCall as RuntimeToolCall
        from agents.tools.registry import ToolResult as RuntimeToolResult

        assert ToolCallEvent is not RuntimeToolCall
        assert ToolResultEvent is not RuntimeToolResult
        assert ToolCallEvent.__name__ == "ToolCallEvent"
        assert ToolResultEvent.__name__ == "ToolResultEvent"

    def test_events_module_exports_no_bare_toolcall_or_toolresult(self) -> None:
        import agents.events as events_mod

        assert not hasattr(events_mod, "ToolCall")
        assert not hasattr(events_mod, "ToolResult")


class TestLeafDiscipline:
    """``agents.events`` must not import the agent loop (PR-plan checklist).

    Asserted **statically**, on the module source: a runtime check is useless
    here because importing any ``agents.X`` submodule first executes
    ``agents/__init__.py``, which eagerly pulls the whole runtime — the
    documented leaf ``agents.llm_types`` pulls the identical set.  The real
    invariant is which modules ``events.py`` itself names.
    """

    def test_only_project_import_is_llm_types(self) -> None:
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[3] / "agents" / "events.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        relative = [
            "." * n.level + (n.module or "")
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.level > 0
        ]
        absolute = [
            alias.name
            for n in ast.walk(tree)
            if isinstance(n, ast.Import)
            for alias in n.names
            if alias.name.startswith("agents")
        ]
        assert relative == [".llm_types"], f"unexpected project imports: {relative}"
        assert absolute == [], f"unexpected absolute agents imports: {absolute}"

    def test_llm_types_is_itself_a_leaf(self) -> None:
        # The one dependency must not drag anything else in, or the leaf
        # guarantee is hollow.
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[3] / "agents" / "llm_types.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        relative = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level > 0]
        assert relative == []


class TestModelOutput:
    """``stop_reason`` reuses the runtime enum; errors are Error events (§A)."""

    @pytest.mark.parametrize("reason", list(StopReason))
    def test_accepts_every_stop_reason_member(self, reason: StopReason) -> None:
        out = ModelOutput(
            **_base(0), role="assistant", content="c",
            stop_reason=reason, token_usage=Usage(1, 2),
        )
        assert out.stop_reason is reason

    def test_stop_reason_has_no_error_member(self) -> None:
        # The RFC's original sketch had an "error" literal; errors are a
        # separate Error event, not a stop_reason value.
        assert "ERROR" not in {m.name for m in StopReason}
        assert "error" not in {m.value for m in StopReason}

    def test_role_is_assistant(self) -> None:
        out = ModelOutput(
            **_base(0), role="assistant", content="c",
            stop_reason=StopReason.END_TURN, token_usage=Usage(0, 0),
        )
        assert out.role == "assistant"


class TestCallbackModelOutputIsDistinct:
    """OQ #3 — an in-callback model call must not look like the turn output."""

    def test_is_not_a_modeloutput(self) -> None:
        cb = CallbackModelOutput(
            **_base(0), callback_name="moderator", content="c", token_usage=Usage(1, 1),
        )
        assert not isinstance(cb, ModelOutput)

    def test_carries_the_originating_callback_name(self) -> None:
        cb = CallbackModelOutput(
            **_base(0), callback_name="quality_bar", content="c", token_usage=Usage(1, 1),
        )
        assert cb.callback_name == "quality_bar"

    def test_channel_publish_filter_selects_only_modeloutput(self) -> None:
        # The §E channel-publish rule: subscribe to ModelOutput, ignore
        # CallbackModelOutput.  isinstance must not conflate them.
        published = [e for e in _one_of_each() if isinstance(e, ModelOutput)]
        assert len(published) == 1
        assert published[0].content == "hi"


class TestErrorEvent:
    """``Error`` is an event, not an exception (§A)."""

    def test_is_not_an_exception(self) -> None:
        assert not issubclass(Error, BaseException)

    def test_cause_event_id_references_another_events_id(self) -> None:
        call = ToolCallEvent(**_base(0), tool_name="t", args={}, tool_call_id="tc")
        err = Error(
            **_base(1), kind=ErrorKind.TOOL_DENIED, message="denied",
            retryable=False, cause_event_id=call.event_id,
        )
        assert err.cause_event_id == call.event_id

    def test_cause_event_id_defaults_to_none(self) -> None:
        err = Error(**_base(0), kind=ErrorKind.INTERNAL, message="m", retryable=True)
        assert err.cause_event_id is None

    def test_kind_is_the_closed_enum(self) -> None:
        err = Error(**_base(0), kind=ErrorKind.WALLET_DENIED, message="m", retryable=False)
        assert err.kind is ErrorKind.WALLET_DENIED


class TestToolResultEvent:
    def test_error_kind_defaults_to_none_on_success(self) -> None:
        res = ToolResultEvent(**_base(0), tool_call_id="tc", ok=True, content="fine")
        assert res.error_kind is None

    def test_failure_carries_a_tool_error_kind(self) -> None:
        res = ToolResultEvent(
            **_base(0), tool_call_id="tc", ok=False, content="nope",
            error_kind=ToolErrorKind.DENIED,
        )
        assert res.error_kind is ToolErrorKind.DENIED

    def test_content_accepts_str_or_dict(self) -> None:
        assert ToolResultEvent(**_base(0), tool_call_id="t", ok=True, content="s").content == "s"
        dict_res = ToolResultEvent(**_base(1), tool_call_id="t", ok=True, content={"k": 1})
        assert dict_res.content == {"k": 1}


class TestStateDelta:
    """``scope`` is an opaque str in Phase 1; RFC 0042 closes it in Phase 2."""

    def test_scope_is_a_plain_string_in_phase_1(self) -> None:
        delta = StateDelta(**_base(0), scope="persona", key="k", op="set", value=1)
        assert isinstance(delta.scope, str)

    @pytest.mark.parametrize(
        "scope", ["app", "persona", "channel", "session", "interaction", "temp"]
    )
    def test_accepts_every_rfc_0042_scope_prefix(self, scope: str) -> None:
        assert StateDelta(**_base(0), scope=scope, key="k", op="set", value=1).scope == scope

    @pytest.mark.parametrize("op", ["set", "delete", "increment"])
    def test_accepts_each_op(self, op: str) -> None:
        assert StateDelta(**_base(0), scope="temp", key="k", op=op, value=None).op == op
