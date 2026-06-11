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
    """Go's ``interactionIDMetadataKey`` and the Python lease-threading
    read MUST be equal."""
    go_value = _go_const(_INTERACTION_ID_GO, "interactionIDMetadataKey")
    py_src = _WALLET_CAUSE_PY.read_text(encoding="utf-8")
    if f'"{go_value}"' not in py_src:
        _parse_miss(
            f"a read of the {go_value!r} metadata key "
            f"(Go interactionIDMetadataKey)",
            _WALLET_CAUSE_PY,
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


# ─── Producer plan OQ 5: the retired-close cause pair ────────────────

_INTERACTION_RESOLVER_GO = Path("internal/channels/interaction_resolver.go")
_CHANNEL_WIRE_METADATA_PY = Path("agents/channel_wire_metadata.py")
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


def test_close_trigger_values_agree() -> None:
    """The trigger vocabulary the resolver stamps (Go ``idleTrigger`` /
    ``endVotesTrigger``) MUST equal the Python seed-point allowlist and the
    boundary seam's idle literal — a drifted value degrades every close
    cause to the legacy label without a failing test on either side."""
    idle = _go_const(_INTERACTION_RESOLVER_GO, "idleTrigger")
    end_votes = _go_const(_END_VOTE_GO, "endVotesTrigger")

    seed_src = _CHANNEL_WIRE_METADATA_PY.read_text(encoding="utf-8")
    for name, value in (
        ("_WIRE_CLOSE_TRIGGER_IDLE", idle),
        ("_WIRE_CLOSE_TRIGGER_END_VOTES", end_votes),
    ):
        pin = f'{name} = "{value}"'
        if pin not in seed_src:
            _parse_miss(f"the trigger literal `{pin}`", _CHANNEL_WIRE_METADATA_PY)

    boundary_pin = f'_WIRE_CLOSE_TRIGGER_IDLE = "{idle}"'
    if boundary_pin not in _INTERACTION_BOUNDARY_PY.read_text(encoding="utf-8"):
        _parse_miss(
            f"the trigger literal `{boundary_pin}`", _INTERACTION_BOUNDARY_PY,
        )
