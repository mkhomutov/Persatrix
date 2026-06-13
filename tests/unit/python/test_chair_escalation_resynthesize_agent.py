"""The chair-escalation *resynthesize* forced turn — agent half (ISSUE-0099).

The chair-stall-escalation amendment rations the interaction to ONE forced
turn (CE5) and does not refund a failure. One failure is provable at publish
time: the chair's forced-turn reply names a hand-off target that lifts to a
real id but is **not** floor-capable — a ``respond: never`` observer, the human
operator, or the chair itself — so ``resolveFloorMentions`` comes up empty and
the hand-off reached nobody. The fix (option 2) re-dispatches ONE second forced
turn with a synthesize-only framing: handing off again is the move that just
failed, so the variant drops outcome (b) and forces the end-vote.

This is the agent half (PR 1 — the wire field + the framing). It lands dormant:
the orchestrator does not set ``chair_escalation_resynthesize`` yet (PR 2). The
contract pinned here:

* The flag is a REFINEMENT of ``chair_escalation`` — the lift is unchanged, so
  the gate is *not* re-tested (covered by ``test_chair_escalation_agent.py``);
  the flag only swaps the framing.
* It is lifted off the wire typed-field-only
  (:func:`agents.channel_wire_metadata.channel_event_payload`), like
  ``interaction_close_notification`` and unlike ``chair_escalation``'s
  unconditional copy, so ordinary traffic keeps key-ABSENCE.
* The selector is strict ``is True`` (the ``floor_mentions_resolved`` posture):
  a truthy non-bool on the cleartext port must not swap the framing.
* The variant snippet forces the end-vote and forbids a second hand-off.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from agents.persona_types import AgentEvent, EventType


def _resynthesize_event(
    *,
    chair_escalation: object = True,
    chair_escalation_resynthesize: object = True,
    sender_id: str = "alex",
) -> AgentEvent:
    payload: dict[str, object] = {
        "content": "thoughts, team?",
        "channel_type": "group",
        "mentions": [],
        "respond_policy": "always",
        "thread_parent_sender_id": "",
        "chair_escalation": chair_escalation,
    }
    if chair_escalation_resynthesize is not None:
        payload["chair_escalation_resynthesize"] = chair_escalation_resynthesize
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=payload,
        channel_id="group:planning",
        sender_id=sender_id,
    )


class TestWireLift:
    def test_payload_carries_the_refinement_typed_field_only(self) -> None:
        from agents.channel_wire_metadata import channel_event_payload
        from agents.generated import task_pb2

        request = task_pb2.ChannelMessageEvent(
            message_id="m-1", channel_id="group:planning", channel_type="group",
            sender_id="alex", content="thoughts?", respond_policy="always",
            chair_escalation=True, chair_escalation_resynthesize=True,
        )
        payload = channel_event_payload(request)
        assert payload["chair_escalation_resynthesize"] is True

    def test_ordinary_traffic_keeps_key_absence(self) -> None:
        """Typed-field-only seeding: the proto3-default ``False`` leaves the key
        absent (the ``interaction_close_notification`` posture), so the strict
        ``is True`` selector never fires on unmarked traffic."""
        from agents.channel_wire_metadata import channel_event_payload
        from agents.generated import task_pb2

        request = task_pb2.ChannelMessageEvent(
            message_id="m-1", channel_id="group:planning", channel_type="group",
            sender_id="alex", content="thoughts?", respond_policy="always",
            chair_escalation=True,
        )
        assert "chair_escalation_resynthesize" not in channel_event_payload(request)


class TestResynthesizeFraming:
    def test_format_selects_the_variant_snippet(self) -> None:
        from agents.persona_runtime.prompt_assembly import format_chair_escalation

        body = "Message from alex:\n\nthoughts, team?"
        framed = format_chair_escalation(body, resynthesize=True)
        assert body in framed
        assert "reached no one" in framed.lower(), "the synthesize-only variant"
        # The default two-outcome framing is NOT what a resynthesize turn shows.
        assert format_chair_escalation(body, resynthesize=False) != framed

    def test_format_event_swaps_framing_only_on_the_strict_refinement(self) -> None:
        """Through ``_format_event`` itself: both flags strictly ``True`` →
        variant; ``chair_escalation`` alone → the two-outcome default; a truthy
        non-bool refinement must not swap the framing (it falls back to the
        default, still a valid escalation framing because field 22 still lifts).
        Called unbound with ``self=None``, the seam's pinned self-independence
        (mirrors ``test_chair_escalation_agent.py``)."""
        from agents.persona_runtime.prompt_assembly import _PromptAssemblyMixin

        fmt = cast(
            "Callable[[object, AgentEvent], str]",
            _PromptAssemblyMixin._format_event,
        )
        variant = fmt(None, _resynthesize_event())
        assert variant.startswith("[Chair escalation")
        assert "reached no one" in variant.lower()

        default = fmt(None, _resynthesize_event(chair_escalation_resynthesize=None))
        assert "reached no one" not in default.lower()

        spoofed = fmt(None, _resynthesize_event(chair_escalation_resynthesize="true"))
        assert "reached no one" not in spoofed.lower()

    def test_snippet_forces_the_vote_and_forbids_a_second_handoff(self) -> None:
        """The distinguishing contract vs the default snippet: the synthesis
        routes through the vote's ``content`` (the one publish where synthesis
        and vote travel together), and a second hand-off is explicitly off the
        table — it is the move that just provably failed."""
        from agents.prompt_loader import load_snippet

        snippet = load_snippet("chair-escalation-resynthesize")
        lower = snippet.lower()
        assert "synthes" in lower and "vote" in lower, "outcome (a) is forced"
        assert "`content`" in snippet, "synthesis routes through the vote content"
        assert "do not hand off again" in lower, (
            "the variant forbids the hand-off that just reached nobody"
        )
