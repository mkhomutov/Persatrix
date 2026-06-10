"""Cross-language drift pins for the floor-capable directedness wire.

Mirrors the established per-topic drift-test pattern
(``test_cross_language_max_cascade_depth_drift.py``, PR #319;
``test_cross_language_salience_max_channel_members_drift.py``, PR #573;
``test_cross_language_respond_policy_drift.py``, PR #597 — its file
would be the natural home for these pins per the amendment's §C item 4,
but it sits at the 500-line review cap, so the pins live in this
sibling instead).

The RFC 0030 floor-capable-directedness amendment
(``docs/rfcs/0030-amendment-floor-capable-directedness.md``) routes the
Tier A suppression basis Go → wire → Python as the ``floor_mentions``
subset plus the ``floor_mentions_resolved`` producer-presence flag.
Three relationships are declared "must agree" across files that share
no code:

* the proto field names/tags (``proto/task.proto``) ↔ the payload lift
  keys (``agents/channel_wire_metadata.py``, the servicer's carve-out) ↔
  the gate's payload keys (``agents/response_gate.py``) — a one-sided
  rename keeps that side's
  suite green while the basis silently stops flowing, the gate falls
  back to the raw-mentions basis on every publish, and the trigger
  defect (one polite mention of the human silences the room) returns
  without a failing test;
* the floor-capable definition — normalized policy ≠ ``never``,
  excluding the sender — implemented only in Go
  (``internal/channels/floor_mentions.go``) but relied on by the Python
  gate, which consumes the subset it cannot recompute (it has no
  membership view);
* the basis switch: consumption keys on the flag (``is True``), never on
  the list's own presence or emptiness, which the wire cannot express
  (proto3 repeated fields have no presence).

Same posture as the sibling files: Python sources are pinned as text
alongside the Go source so the test runs anywhere the Python unit suite
runs — no Go toolchain dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_FLOOR_MENTIONS_GO = Path("internal/channels/floor_mentions.go")
_TASK_PROTO = Path("proto/task.proto")
_RESPONSE_GATE_PY = Path("agents/response_gate.py")
_CHANNEL_WIRE_METADATA_PY = Path("agents/channel_wire_metadata.py")


def _parse_miss(what: str, where: Path) -> None:
    """Fail with an actionable message on a parse miss (vs a silent
    ``None``-vs-value ``AssertionError``): a refactor that hides the
    declaration must land as a deliberate update to this test's parse
    rules — the cross-language drift pin is part of the contract.
    """
    pytest.fail(
        f"could not find {what} in {where}. If it was renamed or "
        f"restructured, update the parse rule in this test to match the "
        f"new shape — the cross-language drift pin is part of the "
        f"contract.",
    )


def test_floor_mentions_wire_field_names_and_tags_agree() -> None:
    """The proto field names/tags and both consumption sides MUST agree."""
    proto_src = _TASK_PROTO.read_text(encoding="utf-8")
    if not re.search(
        r"^\s*repeated string floor_mentions = 18;", proto_src, re.MULTILINE
    ):
        _parse_miss("`repeated string floor_mentions = 18;`", _TASK_PROTO)
    if not re.search(
        r"^\s*bool floor_mentions_resolved = 19;", proto_src, re.MULTILINE
    ):
        _parse_miss("`bool floor_mentions_resolved = 19;`", _TASK_PROTO)

    lift_src = _CHANNEL_WIRE_METADATA_PY.read_text(encoding="utf-8")
    for lift in (
        '"floor_mentions": list(request.floor_mentions)',
        '"floor_mentions_resolved": request.floor_mentions_resolved',
    ):
        if lift not in lift_src:
            _parse_miss(f"the payload lift `{lift}`", _CHANNEL_WIRE_METADATA_PY)

    gate_src = _RESPONSE_GATE_PY.read_text(encoding="utf-8")
    for key in ('"floor_mentions_resolved"', '"floor_mentions"'):
        if f"payload.get({key})" not in gate_src:
            _parse_miss(f"a `payload.get({key})` read", _RESPONSE_GATE_PY)


def test_floor_capable_definition_excludes_sender_and_normalizes() -> None:
    """The Go resolver's floor-capable membership test MUST be
    ``ParticipantID != sender && Normalize() != RespondNever`` (amendment
    §C item 4: "normalized policy ≠ never, excluding the sender").

    The definition exists in exactly one place (the gate cannot recompute
    it); this pin keeps a Go-side edit — dropping the Normalize (a
    hand-edited disposition row silently widens the basis) or the sender
    exclusion (a sole self-mention re-closes the floor) — from landing
    without a deliberate cross-language review.
    """
    src = _FLOOR_MENTIONS_GO.read_text(encoding="utf-8")
    membership_test = re.search(
        r"if m\.ParticipantID != senderID && "
        r"m\.RespondPolicy\.Normalize\(\) != RespondNever \{",
        src,
    )
    if membership_test is None:
        _parse_miss(
            "the floor-capable membership test "
            "(`ParticipantID != senderID && Normalize() != RespondNever`)",
            _FLOOR_MENTIONS_GO,
        )


def test_floor_mentions_basis_switch_is_the_flag() -> None:
    """The gate's basis switch MUST key on ``floor_mentions_resolved is
    True`` — never on the list's own presence or emptiness (which the wire
    cannot express; §C item 2) and never on truthiness (a spoofed truthy
    non-bool on the cleartext port must not widen admission).
    """
    gate_src = _RESPONSE_GATE_PY.read_text(encoding="utf-8")
    if 'payload.get("floor_mentions_resolved") is True' not in gate_src:
        _parse_miss(
            "the strict basis switch "
            '(`payload.get("floor_mentions_resolved") is True`)',
            _RESPONSE_GATE_PY,
        )
