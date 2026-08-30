"""Cross-language drift pins for the RFC 0052 PR 4b-ii synthesis wire.

Split out of ``test_cross_language_interaction_wire_drift.py`` when the
PR #718 review's reply-echo pin pushed it past the 500-line review cap
(``scripts/checks/file_size.py --strict``) — the per-topic drift-test
pattern (``test_cross_language_floor_mentions_drift.py`` and siblings).
Two contracts live here:

* the three PR 4b-ii typed wire fields (28/29/30) across the proto, the
  Go dispatcher lift, the Python payload lift, and their strict
  consumers;
* the ``synthesis_reply`` reply-echo metadata key (PR #718 review) —
  the fanout-head claim's discriminating conjunct — between Go's
  ``claimSynthesisReply`` reader and the ONE Python producer.

Same posture as the sibling files: sources pinned as text so the tests
run anywhere the Python unit suite runs — no Go toolchain dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

# The shared parse helpers are imported from the family's parent file (the
# test_cross_language_convene_wire_drift.py posture) — the re-declared copies
# this file shipped with had already drifted their docstrings, and the
# parent's ``-> NoReturn`` mypy fix history shows why per-copy helpers rot
# (PR #718 review).
from .test_cross_language_interaction_wire_drift import _go_const, _parse_miss

_TASK_PROTO = Path("proto/task.proto")
# The in-process→wire translation split out of grpc_dispatcher.go at the
# 500-line cap (PR #718 review — the delivery-miss returns pushed it over);
# the envelope lifts this file pins all live in the proto-translation half.
_GRPC_DISPATCHER_GO = Path("internal/channels/grpc_dispatcher_proto.go")
# The reply-recognition seam split out of synthesis_close.go at the size
# cap (PR #718 follow-up review) — the marker constant lives with the claim.
_SYNTHESIS_CLAIM_GO = Path("internal/channels/synthesis_claim.go")
_CHANNEL_WIRE_METADATA_PY = Path("agents/channel_wire_metadata.py")
# DispatchContext (the claim rule + the synthesis derivation) moved out of
# channel_wire_metadata.py at the 500-line cap (ISSUE-0118 PR 1); the
# reply-echo pins follow it.
_DISPATCH_CONTEXT_PY = Path("agents/dispatch_context.py")
_CLOSE_NOTIFICATION_PY = Path("agents/persona_runtime/close_notification.py")
_PROMPT_ASSEMBLY_PY = Path("agents/persona_runtime/prompt_assembly.py")
_RESPONSE_GATE_PY = Path("agents/response_gate.py")
_CLOSE_NOTIFICATION_GO = Path("internal/channels/close_notification.go")
_BOUNDED_CLOSE_GO = Path("internal/channels/bounded_close.go")


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
    # prompt_assembly keeps the per-marker strict read; the gate's read
    # moved into the `_FORCED_TURN_MARKERS` registry loop (PR #718 review),
    # so its pin parses the registry entry plus the loop's strict read —
    # the sibling `test_cross_language_interaction_wire_drift.py` shape.
    pa_src = _PROMPT_ASSEMBLY_PY.read_text(encoding="utf-8")
    if 'payload.get("synthesis_turn") is True' not in pa_src:
        _parse_miss(
            'a strict `…get("synthesis_turn") is True` read',
            _PROMPT_ASSEMBLY_PY,
        )
    gate_src = _RESPONSE_GATE_PY.read_text(encoding="utf-8")
    registry = re.search(r"_FORCED_TURN_MARKERS\s*=\s*\(([^)]*)\)", gate_src)
    if registry is None:
        _parse_miss(
            "the `_FORCED_TURN_MARKERS = (…)` registry", _RESPONSE_GATE_PY,
        )
    if '"synthesis_turn"' not in registry.group(1):
        _parse_miss(
            '"synthesis_turn" in the gate\'s `_FORCED_TURN_MARKERS` registry',
            _RESPONSE_GATE_PY,
        )
    if "payload.get(marker) is True" not in gate_src:
        _parse_miss(
            "the strict `payload.get(marker) is True` registry read",
            _RESPONSE_GATE_PY,
        )


def test_synthesis_reply_metadata_key_agrees() -> None:
    """The synthesis reply-echo marker (PR #718 review) MUST agree between
    Go's fanout-head claim reader and the ONE Python producer. The claim's
    sender+claim conjuncts are shared with every ordinary reply in the
    interaction (the id spans every round; every reply echoes it), so the
    marker is the discriminating conjunct — a one-sided rename leaves every
    chair synthesis reply unclaimed, every §D close silently degrading to
    the timeout net (a 2-minute close with the artifact discarded) while
    both suites stay green. Same metadata-bag vehicle and posture as
    ``end_interaction_vote``.
    """
    go_value = _go_const(_SYNTHESIS_CLAIM_GO, "synthesisReplyMetadataKey")
    py_src = _DISPATCH_CONTEXT_PY.read_text(encoding="utf-8")
    stamp = f'claim["{go_value}"] = True'
    if stamp not in py_src:
        _parse_miss(
            f"the reply-echo stamp `{stamp}` "
            f"(Go synthesisReplyMetadataKey = {go_value!r})",
            _DISPATCH_CONTEXT_PY,
        )
    # The derivation is structural (DispatchContext.for_event, strict
    # ``is True``) — the same one-home rule as the interaction-id claim: a
    # per-call-site payload re-read could drift outside this pin.
    if '.get("synthesis_turn") is True' not in py_src:
        _parse_miss(
            'the strict `…get("synthesis_turn") is True` derivation '
            "(DispatchContext.for_event)",
            _DISPATCH_CONTEXT_PY,
        )


def test_bounded_trigger_stamp_site_uses_named_predicate() -> None:
    """The Go wire-stamp site that gates the OQ #6 metering key MUST route
    through the NAMED ``isBoundedCloseTrigger`` predicate, and the predicate
    MUST enumerate exactly the two pinned trigger consts (PR #718 review).
    ``test_close_trigger_values_agree`` (the parent drift file) pins the
    const VALUES equal to Python's ``WIRE_BOUNDED_CLOSE_TRIGGERS`` — but it
    parses only the const declarations, so the stamp site itself was the one
    Go enumeration point a third bounded cause could silently miss: the new
    cause would close interactions without ever riding the wire, receivers
    keeping the legacy label and every summary of that cause skipping its
    lease, all suites green.
    """
    close_src = _CLOSE_NOTIFICATION_GO.read_text(encoding="utf-8")
    if "if isBoundedCloseTrigger(trigger) {" not in close_src:
        _parse_miss(
            "the named-predicate stamp gate `if isBoundedCloseTrigger(trigger) {`",
            _CLOSE_NOTIFICATION_GO,
        )
    bounded_src = _BOUNDED_CLOSE_GO.read_text(encoding="utf-8")
    if (
        "return trigger == structuralTrigger || trigger == costTrigger"
        not in bounded_src
    ):
        _parse_miss(
            "the `isBoundedCloseTrigger` body enumerating exactly "
            "`structuralTrigger || costTrigger`",
            _BOUNDED_CLOSE_GO,
        )
