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
from typing import NoReturn

import pytest

_TASK_PROTO = Path("proto/task.proto")
_GRPC_DISPATCHER_GO = Path("internal/channels/grpc_dispatcher.go")
_SYNTHESIS_CLOSE_GO = Path("internal/channels/synthesis_close.go")
_CHANNEL_WIRE_METADATA_PY = Path("agents/channel_wire_metadata.py")
_CLOSE_NOTIFICATION_PY = Path("agents/persona_runtime/close_notification.py")
_PROMPT_ASSEMBLY_PY = Path("agents/persona_runtime/prompt_assembly.py")
_RESPONSE_GATE_PY = Path("agents/response_gate.py")


def _parse_miss(what: str, where: Path) -> NoReturn:
    """Fail with an actionable message on a parse miss (vs a silent
    ``None``-vs-value ``AssertionError``) — the sibling files' contract:
    a refactor that hides the declaration must land as a deliberate
    update to this test's parse rules."""
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
    go_value = _go_const(_SYNTHESIS_CLOSE_GO, "synthesisReplyMetadataKey")
    py_src = _CHANNEL_WIRE_METADATA_PY.read_text(encoding="utf-8")
    stamp = f'claim["{go_value}"] = True'
    if stamp not in py_src:
        _parse_miss(
            f"the reply-echo stamp `{stamp}` "
            f"(Go synthesisReplyMetadataKey = {go_value!r})",
            _CHANNEL_WIRE_METADATA_PY,
        )
    # The derivation is structural (DispatchContext.for_event, strict
    # ``is True``) — the same one-home rule as the interaction-id claim: a
    # per-call-site payload re-read could drift outside this pin.
    if '.get("synthesis_turn") is True' not in py_src:
        _parse_miss(
            'the strict `…get("synthesis_turn") is True` derivation '
            "(DispatchContext.for_event)",
            _CHANNEL_WIRE_METADATA_PY,
        )
