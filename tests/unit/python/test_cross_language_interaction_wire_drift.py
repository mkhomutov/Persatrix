"""Cross-language drift pins for the interaction-id / end-vote wire keys.

Mirrors the established per-topic drift-test pattern
(``test_cross_language_floor_mentions_drift.py`` and siblings). The
producer plan (PR 2, §C item 4 posture) routes two metadata-key literals
across files that share no code:

* ``end_interaction_vote`` — written by the Python vote producer
  (``agents/end_vote_action.py``, the END_INTERACTION_VOTE publish) and
  read by Go's ``readEndInteractionVote``
  (``internal/channels/end_vote.go``). A one-sided rename keeps that
  side's suite green while every vote silently stops counting toward the
  quorum — the semantic terminator dies without a failing test.
* ``interaction_id`` — written by Go's resolver
  (``internal/channels/interaction_id.go``), lifted onto the event
  metadata by ``agents/channel_wire_metadata.py``, and read back by the
  lease-threading seam
  (``agents/persona_runtime/wallet_cause.py``); the action loop and the
  Tier B bid must pass it into their leased calls or Layer 1 attribution
  silently degrades to untracked.

Same posture as the sibling files: sources pinned as text so the test
runs anywhere the Python unit suite runs — no Go toolchain dependency.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NoReturn

import pytest

_END_VOTE_GO = Path("internal/channels/end_vote.go")
_INTERACTION_ID_GO = Path("internal/channels/interaction_id.go")
_END_VOTE_ACTION_PY = Path("agents/end_vote_action.py")
_WALLET_CAUSE_PY = Path("agents/persona_runtime/wallet_cause.py")
_ACTION_LOOP_PY = Path("agents/persona_runtime/action_loop.py")
_SALIENCE_GATE_PY = Path("agents/persona_runtime/salience_gate.py")
_EPISODE_ROUTING_PY = Path("agents/persona_runtime/episode_routing.py")
_CHANNEL_WIRE_METADATA_PY = Path("agents/channel_wire_metadata.py")


def _parse_miss(what: str, where: Path) -> NoReturn:
    """Fail with an actionable message on a parse miss (vs a silent
    ``None``-vs-value ``AssertionError``): a refactor that hides the
    declaration must land as a deliberate update to this test's parse
    rules — the cross-language drift pin is part of the contract.

    ``NoReturn`` (``pytest.fail`` raises unconditionally) so mypy narrows
    ``Match | None`` after a guarded call — the committed ``-> None``
    annotation failed ``mypy tests/`` on ``_go_const``'s ``m.group(1)``,
    masked in CI because the ruff step failed first.
    """
    pytest.fail(
        f"could not find {what} in {where}. If it was renamed or "
        f"restructured, update the parse rule in this test to match the "
        f"new shape — the cross-language drift pin is part of the "
        f"contract.",
    )


def _go_const(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    m = re.search(rf'^\s*const\s+{name}\s*=\s*"([^"]+)"\s*$', src, re.MULTILINE)
    if m is None:
        _parse_miss(f"`const {name} = \"<value>\"`", path)
    return m.group(1)


def test_end_vote_metadata_key_agrees() -> None:
    """Go's ``endVoteMetadataKey`` and the Python vote producer's publish
    metadata literal MUST be equal."""
    go_value = _go_const(_END_VOTE_GO, "endVoteMetadataKey")
    py_src = _END_VOTE_ACTION_PY.read_text(encoding="utf-8")
    lift = f'"{go_value}": True'
    if lift not in py_src:
        _parse_miss(
            f"the vote publish metadata literal `{lift}` "
            f"(Go endVoteMetadataKey = {go_value!r})",
            _END_VOTE_ACTION_PY,
        )


def test_interaction_id_metadata_key_agrees() -> None:
    """Go's ``interactionIDMetadataKey`` and the ONE Python home of the key
    literal (``channel_wire_metadata`` — the ingress seed plus the
    ``wire_interaction_id`` / ``same_channel_claim`` pair) MUST be equal,
    and the two former inline readers MUST delegate to the shared reader
    (PR #716 review, applied): ``wallet_cause``'s lease-threading read and
    ``episode_routing``'s rotation-boundary wire-id read were byte-identical
    copies sitting beside the pinned home — a semantics change applied to
    one and not the others would have wallet spend billed under an id the
    no-reopen latch and the soft-budget close trigger never see. Pinning
    the delegation (not the literal) in those two files means the key
    literal now lives in exactly one Python module."""
    go_value = _go_const(_INTERACTION_ID_GO, "interactionIDMetadataKey")
    if f'"{go_value}"' not in _CHANNEL_WIRE_METADATA_PY.read_text(encoding="utf-8"):
        _parse_miss(
            f"a read of the {go_value!r} metadata key "
            f"(Go interactionIDMetadataKey)",
            _CHANNEL_WIRE_METADATA_PY,
        )
    for py_path, pin in {
        _WALLET_CAUSE_PY: "return wire_interaction_id(event)",
        _EPISODE_ROUTING_PY: "wire_interaction_id(event)",
    }.items():
        if pin not in py_path.read_text(encoding="utf-8"):
            _parse_miss(
                f"the shared-reader delegation `{pin}`",
                py_path,
            )


# The exact kwarg-with-value literal each call site must pass. The pin
# names the kwarg AND the value expression deliberately: a bare
# ``interaction_id\s*=`` regex is vacuously satisfied in ``action_loop.py``
# by the attribution tuple unpack alone (``…, lease_interaction_id =
# lease_attribution_for_event(…)`` — ``interaction_id`` is the tail of
# ``lease_interaction_id``), so the precise regression this test exists to
# catch — the kwarg dropped from the leased ``create_message`` call while
# the unpack stays — would keep the suite green. Pinning the full
# ``kwarg=value`` literal also guards the value side: passing some *other*
# id under the kwarg would be an attribution bug the looser regex can't see.
_LEASE_THREADING_PINS = {
    _ACTION_LOOP_PY: "interaction_id=lease_interaction_id",
    _SALIENCE_GATE_PY: "interaction_id=lease_interaction_id_for_event(",
}


def test_lease_call_sites_thread_the_interaction() -> None:
    """The two channel-path leased calls — the Tier C quality turn
    (action_loop) and the Tier B salience bid (salience_gate's
    evaluate_salience call) — MUST pass ``interaction_id`` into their
    ``create_message``/bid invocations, or Layer 1 attribution silently
    degrades to untracked on the path it exists for."""
    for path, pin in _LEASE_THREADING_PINS.items():
        src = path.read_text(encoding="utf-8")
        if pin not in src:
            _parse_miss(f"the lease-threading literal `{pin}`", path)


# The RFC 0052 no-reopen claim's shared seams (PR #716 review): the executor
# entry points read the origin id through the ONE drift-pinned reader, and
# both publish sites build the claim through the ONE shared rule. Exact
# call-site literals, the _LEASE_THREADING_PINS posture: a site quietly
# reverting to an inline read/build would sit outside the drift pin again —
# and a coordinated key rename would then leave it echoing no claim, the
# latch blind, and post-close stragglers minting fresh and reopening the
# closed discussion (the exact regression the shared home closed).
_NO_REOPEN_CLAIM_PINS = {
    Path("agents/dispatch.py"): [
        "context=DispatchContext.for_event(event",
    ],
    Path("agents/chat_reply.py"): [
        "context = DispatchContext.for_event(event",
        # The error-recovery publish is the THIRD same-channel path (PR #716
        # review: it was missed when the reply and end-vote publishes were
        # stamped, so a post-close budget-denial straggler minted fresh and
        # reopened the closed discussion).
        "claim = same_channel_claim(",
    ],
    Path("agents/action_executor.py"): [
        "publish_metadata = same_channel_claim(",
    ],
    _END_VOTE_ACTION_PY: [
        "claim = same_channel_claim(",
    ],
}


def test_no_reopen_claim_sites_share_reader_and_rule() -> None:
    """Every event-driven executor ingress MUST build its context through
    ``DispatchContext.for_event`` (which derives the origin pair through the
    drift-pinned ``wire_interaction_id`` reader, structurally — PR #716
    review applied: the pair was previously threaded as parallel kwargs a
    site could half-forget) and every same-channel publish site MUST build
    its claim through ``same_channel_claim`` — the shared, drift-pinned
    home (see ``test_interaction_id_metadata_key_agrees``)."""
    for path, pins in _NO_REOPEN_CLAIM_PINS.items():
        src = path.read_text(encoding="utf-8")
        for pin in pins:
            if pin not in src:
                _parse_miss(f"the no-reopen claim literal `{pin}`", path)


# ─── Producer plan OQ 5: the retired-close cause pair ────────────────

_INTERACTION_RESOLVER_GO = Path("internal/channels/interaction_resolver.go")
_INTERACTION_BOUNDARY_PY = Path("agents/persona_runtime/interaction_boundary.py")


def _go_grouped_const(path: Path, name: str) -> str:
    """Parse a NAME = "value" entry inside a grouped ``const (...)`` block
    (the shape ``previousInteraction*MetadataKey`` use; :func:`_go_const`
    only matches the single-declaration ``const NAME = ...`` form)."""
    src = path.read_text(encoding="utf-8")
    m = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"\s*$', src, re.MULTILINE)
    if m is None:
        _parse_miss(f"`{name} = \"<value>\"` (grouped const)", path)
    return m.group(1)


def test_previous_interaction_metadata_keys_agree() -> None:
    """Go's OQ 5 metadata keys (stamped by publishCommit, lifted by the
    dispatcher) MUST equal the literals the Python seed point writes
    (``channel_wire_metadata.seed_wire_metadata``) and the rotation-close
    seam reads (``interaction_boundary.wire_rotation_close_reason``) — a
    one-sided rename silently reverts every rotation close to the legacy
    structural label while both suites stay green."""
    for go_name in (
        "previousInteractionIDMetadataKey",
        "previousInteractionTriggerMetadataKey",
    ):
        go_value = _go_grouped_const(_INTERACTION_ID_GO, go_name)
        for py_path in (_CHANNEL_WIRE_METADATA_PY, _INTERACTION_BOUNDARY_PY):
            if f'"{go_value}"' not in py_path.read_text(encoding="utf-8"):
                _parse_miss(
                    f"the {go_value!r} metadata-key literal (Go {go_name})",
                    py_path,
                )


_BOUNDED_CLOSE_GO = Path("internal/channels/bounded_close.go")


def test_close_trigger_values_agree() -> None:
    """The trigger vocabulary the close sites stamp (Go ``idleTrigger`` /
    ``endVotesTrigger`` / ``structuralTrigger`` / ``costTrigger``) MUST
    equal the single Python source (``channel_wire_metadata`` — the PR 607
    second-pass review collapsed the boundary seam's re-declared copy into
    an import, so growing the vocabulary is one edit per language).
    Imported and compared directly — stronger than a text pin — while the
    Go side stays text-pinned (no Go toolchain dependency).

    The RFC 0052 bounded-close pair is compared against the WHOLE-set
    equality below deliberately (PR #716 review): the bounded close shipped
    stamping ``structural``/``cost`` while both allowlists still held only
    the original two values, and this test stayed green because it never
    parsed ``bounded_close.go`` — the seed's pair-or-nothing validation
    then zeroed ``previous_interaction_id`` along with the trigger,
    silently disabling the ``predecessor_wire_id`` straggler defence on
    every bounded-close boundary."""
    from agents.channel_wire_metadata import (
        WIRE_CLOSE_TRIGGER_COST,
        WIRE_CLOSE_TRIGGER_END_VOTES,
        WIRE_CLOSE_TRIGGER_IDLE,
        WIRE_CLOSE_TRIGGER_STRUCTURAL,
        WIRE_CLOSE_TRIGGERS,
    )
    from agents.persona_runtime import interaction_boundary

    idle = _go_const(_INTERACTION_RESOLVER_GO, "idleTrigger")
    end_votes = _go_const(_END_VOTE_GO, "endVotesTrigger")
    structural = _go_grouped_const(_BOUNDED_CLOSE_GO, "structuralTrigger")
    cost = _go_grouped_const(_BOUNDED_CLOSE_GO, "costTrigger")

    assert WIRE_CLOSE_TRIGGER_IDLE == idle
    assert WIRE_CLOSE_TRIGGER_END_VOTES == end_votes
    assert WIRE_CLOSE_TRIGGER_STRUCTURAL == structural
    assert WIRE_CLOSE_TRIGGER_COST == cost
    assert WIRE_CLOSE_TRIGGERS == {idle, end_votes, structural, cost}
    # The boundary seam consumes the import, not a re-declaration.
    assert interaction_boundary.WIRE_CLOSE_TRIGGER_IDLE is WIRE_CLOSE_TRIGGER_IDLE


_GRPC_DISPATCHER_GO = Path("internal/channels/grpc_dispatcher.go")
_PROMPT_ASSEMBLY_PY = Path("agents/persona_runtime/prompt_assembly.py")
_RESPONSE_GATE_PY = Path("agents/response_gate.py")
_TASK_PROTO = Path("proto/task.proto")


def test_chair_escalation_marker_agrees() -> None:
    """The chair-stall-escalation forced-turn marker (amendment §C items
    1–2) MUST agree across the proto field, the Go dispatcher lift, the
    Python payload lift, and both strict consumers. A one-sided rename
    leaves the orchestrator escalating into a marker nobody reads: the
    chair's gate re-runs the very bid that produced the stall, and the
    escalation silently degrades to the pre-amendment silence with every
    suite green.
    """
    proto_src = _TASK_PROTO.read_text(encoding="utf-8")
    if not re.search(r"^\s*bool chair_escalation = 22;", proto_src, re.MULTILINE):
        _parse_miss("`bool chair_escalation = 22;`", _TASK_PROTO)

    go_src = _GRPC_DISPATCHER_GO.read_text(encoding="utf-8")
    if "ChairEscalation: env.ChairEscalation" not in go_src:
        _parse_miss(
            "the dispatcher lift `ChairEscalation: env.ChairEscalation`",
            _GRPC_DISPATCHER_GO,
        )

    lift_src = _CHANNEL_WIRE_METADATA_PY.read_text(encoding="utf-8")
    if '"chair_escalation": request.chair_escalation' not in lift_src:
        _parse_miss(
            'the payload lift `"chair_escalation": request.chair_escalation`',
            _CHANNEL_WIRE_METADATA_PY,
        )

    # Both consumers honour the marker ONLY as the strict boolean — the
    # floor_mentions_resolved posture (a spoofed truthy non-bool on the
    # cleartext port must not widen admission or rewrite the prompt).
    # One substring covers both shapes: the gate reads a local
    # `payload.get(...)`, prompt_assembly reads `event.payload.get(...)`,
    # and the latter contains the former — the PR 610 second-pass review
    # dropped a second `event.`-prefixed clause that could therefore
    # never be the deciding one.
    for path in (_RESPONSE_GATE_PY, _PROMPT_ASSEMBLY_PY):
        src = path.read_text(encoding="utf-8")
        if 'payload.get("chair_escalation") is True' not in src:
            _parse_miss(
                'a strict `…get("chair_escalation") is True` read', path,
            )


_CLOSE_NOTIFICATION_PY = Path("agents/persona_runtime/close_notification.py")


def test_close_notification_marker_agrees() -> None:
    """The end-vote close-notification marker (the close-propagation
    amendment, CP2/CP3) MUST agree across the proto field, the Go
    dispatcher lift, the Python payload lift, and both strict consumers.
    A one-sided rename leaves the orchestrator announcing closes into a
    marker nobody reads: every member's gate runs an ordinary bid on the
    closing vote, no local tracker closes, and the converged discussion
    silently degrades to the pre-amendment "went idle" burial with every
    suite green.
    """
    proto_src = _TASK_PROTO.read_text(encoding="utf-8")
    if not re.search(
        r"^\s*bool interaction_close_notification = 23;", proto_src, re.MULTILINE,
    ):
        _parse_miss("`bool interaction_close_notification = 23;`", _TASK_PROTO)

    go_src = _GRPC_DISPATCHER_GO.read_text(encoding="utf-8")
    if "InteractionCloseNotification: env.InteractionCloseNotification" not in go_src:
        _parse_miss(
            "the dispatcher lift `InteractionCloseNotification: "
            "env.InteractionCloseNotification`",
            _GRPC_DISPATCHER_GO,
        )

    # The lift is CONDITIONAL, unlike chair_escalation's: the committed
    # acceptance pins key-ABSENCE on unmarked events (typed-field-only).
    lift_src = _CHANNEL_WIRE_METADATA_PY.read_text(encoding="utf-8")
    if 'payload["interaction_close_notification"] = True' not in lift_src:
        _parse_miss(
            'the conditional payload lift '
            '`payload["interaction_close_notification"] = True`',
            _CHANNEL_WIRE_METADATA_PY,
        )

    # Both consumers honour the marker ONLY as the strict boolean — the
    # gate's refusal branch and the close dispatch's defence-in-depth
    # re-check (a spoofed truthy non-bool must neither suppress a turn
    # into oblivion nor fabricate a close).
    for path in (_RESPONSE_GATE_PY, _CLOSE_NOTIFICATION_PY):
        src = path.read_text(encoding="utf-8")
        if 'payload.get("interaction_close_notification") is True' not in src:
            _parse_miss(
                'a strict `…get("interaction_close_notification") is True` read',
                path,
            )


def test_chair_escalation_resynthesize_marker_agrees() -> None:
    """The chair-escalation RESYNTHESIZE refinement (ISSUE-0099) MUST agree
    across the proto field, the Python payload lift, and its sole strict
    consumer — the same one-sided-rename guard the ``chair_escalation`` and
    ``interaction_close_notification`` markers carry, extended to their
    sibling so the second-forced-turn framing cannot silently stop firing
    with every suite green (the exact degradation this file exists to catch).

    Scope note — this is the AGENT half (PR 1, lands dormant). The producer
    (Go ``maybeEscalateStall`` setting the new field on the second forced
    turn) is PR 2, so there is deliberately NO Go-dispatcher lift to pin yet;
    the cross-language half (``ChairEscalationResynthesize: env.…``) joins
    this pin when PR 2 adds the producer. Two things the agent half DOES pin:

    * The lift is CONDITIONAL — the ``interaction_close_notification``
      posture, NOT ``chair_escalation``'s unconditional copy — so ordinary
      traffic keeps key-ABSENCE and the strict ``is True`` selector never
      fires on unmarked events.
    * The strict read lives ONLY in prompt_assembly, NOT the response gate:
      the refinement swaps FRAMING, while admission and the Tier-B-bid skip
      still key on ``chair_escalation = 22`` (covered by
      ``test_chair_escalation_marker_agrees`` above). Pinning the gate here
      would wrongly assert the gate reads the new field — it does not.
    """
    proto_src = _TASK_PROTO.read_text(encoding="utf-8")
    if not re.search(
        r"^\s*bool chair_escalation_resynthesize = 24;", proto_src, re.MULTILINE,
    ):
        _parse_miss("`bool chair_escalation_resynthesize = 24;`", _TASK_PROTO)
    # The refinement is paired with `chair_escalation = 22`, never a new
    # number standing alone: the lift rides field 22 and this field only
    # swaps framing, so the two field numbers must stay adjacent siblings.
    if not re.search(r"^\s*bool chair_escalation = 22;", proto_src, re.MULTILINE):
        _parse_miss("`bool chair_escalation = 22;` (the field 24 lifts on)", _TASK_PROTO)

    # The lift is CONDITIONAL, like interaction_close_notification's.
    lift_src = _CHANNEL_WIRE_METADATA_PY.read_text(encoding="utf-8")
    if 'payload["chair_escalation_resynthesize"] = True' not in lift_src:
        _parse_miss(
            'the conditional payload lift '
            '`payload["chair_escalation_resynthesize"] = True`',
            _CHANNEL_WIRE_METADATA_PY,
        )

    # The sole strict consumer honours the marker only as the strict boolean
    # (a spoofed truthy non-bool on the cleartext port must not swap the
    # framing) — and it is the framing seam, not the gate.
    prompt_src = _PROMPT_ASSEMBLY_PY.read_text(encoding="utf-8")
    if 'payload.get("chair_escalation_resynthesize") is True' not in prompt_src:
        _parse_miss(
            'a strict `…get("chair_escalation_resynthesize") is True` read',
            _PROMPT_ASSEMBLY_PY,
        )
    if 'payload.get("chair_escalation_resynthesize")' in _RESPONSE_GATE_PY.read_text(
        encoding="utf-8",
    ):
        pytest.fail(
            "the response gate must NOT read chair_escalation_resynthesize: the "
            "refinement swaps framing only; admission still keys on "
            "chair_escalation = 22. A gate read here means the lift posture "
            "drifted — update the contract deliberately.",
        )


def test_synthesis_close_wire_fields_agree() -> None:
    """The RFC 0052 PR 4b-ii wire fields MUST agree across the proto, the
    Go dispatcher lift, the Python payload lift, and their strict
    consumers — the same one-sided-rename guard as the sibling markers. A
    drifted `synthesis_turn` leaves the bounded close dispatching a
    directive nobody answers (every §D close degrades to the timeout
    fallback); a drifted redelivery/trigger pair silently reverts the
    duplicate-final-turn fix and unmeters every close summary (OQ #6) with
    all suites green.
    """
    proto_src = _TASK_PROTO.read_text(encoding="utf-8")
    for decl in (
        r"^\s*bool close_notification_redelivery = 28;",
        r"^\s*string close_notification_close_trigger = 29;",
        r"^\s*bool synthesis_turn = 30;",
    ):
        if not re.search(decl, proto_src, re.MULTILINE):
            _parse_miss(f"`{decl}`", _TASK_PROTO)

    # gofmt aligns the struct-literal lift block, so the pin tolerates the
    # alignment whitespace rather than a byte-exact template.
    go_src = _GRPC_DISPATCHER_GO.read_text(encoding="utf-8")
    for field, env in (
        ("CloseNotificationRedelivery", "env.InteractionCloseRedelivery"),
        ("CloseNotificationCloseTrigger", "env.InteractionCloseTrigger"),
        ("SynthesisTurn", "env.SynthesisTurn"),
    ):
        if not re.search(rf"{field}:\s+{re.escape(env)}", go_src):
            _parse_miss(f"the dispatcher lift `{field}: {env}`", _GRPC_DISPATCHER_GO)

    # All three lifts are CONDITIONAL (typed-field-only, key-ABSENCE on
    # ordinary traffic); the trigger lift is additionally allowlisted to the
    # two causes the bounded close stamps.
    lift_src = _CHANNEL_WIRE_METADATA_PY.read_text(encoding="utf-8")
    for lift in (
        'payload["close_notification_redelivery"] = True',
        'payload["close_notification_close_trigger"] = (',
        'payload["synthesis_turn"] = True',
    ):
        if lift not in lift_src:
            _parse_miss(
                f"the conditional payload lift `{lift}`", _CHANNEL_WIRE_METADATA_PY,
            )

    # Strict consumers: the close dispatch reads the redelivery marker as a
    # strict boolean and the trigger through the allowlisted vocabulary; the
    # gate and the framing seam read the synthesis marker as strict booleans.
    close_src = _CLOSE_NOTIFICATION_PY.read_text(encoding="utf-8")
    if 'payload.get("close_notification_redelivery") is True' not in close_src:
        _parse_miss(
            'a strict `…get("close_notification_redelivery") is True` read',
            _CLOSE_NOTIFICATION_PY,
        )
    if 'payload.get("close_notification_close_trigger")' not in close_src:
        _parse_miss(
            'the `…get("close_notification_close_trigger")` read',
            _CLOSE_NOTIFICATION_PY,
        )
    for path in (_RESPONSE_GATE_PY, _PROMPT_ASSEMBLY_PY):
        src = path.read_text(encoding="utf-8")
        if 'payload.get("synthesis_turn") is True' not in src:
            _parse_miss('a strict `…get("synthesis_turn") is True` read', path)
