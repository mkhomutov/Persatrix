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

import pytest

_END_VOTE_GO = Path("internal/channels/end_vote.go")
_INTERACTION_ID_GO = Path("internal/channels/interaction_id.go")
_END_VOTE_ACTION_PY = Path("agents/end_vote_action.py")
_WALLET_CAUSE_PY = Path("agents/persona_runtime/wallet_cause.py")
_ACTION_LOOP_PY = Path("agents/persona_runtime/action_loop.py")
_SALIENCE_GATE_PY = Path("agents/persona_runtime/salience_gate.py")


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


def test_lease_call_sites_thread_the_interaction() -> None:
    """The two channel-path leased calls — the Tier C quality turn
    (action_loop) and the Tier B salience bid (salience_gate's
    evaluate_salience call) — MUST pass ``interaction_id`` into their
    ``create_message``/bid invocations, or Layer 1 attribution silently
    degrades to untracked on the path it exists for."""
    for path in (_ACTION_LOOP_PY, _SALIENCE_GATE_PY):
        src = path.read_text(encoding="utf-8")
        if not re.search(r"interaction_id\s*=", src):
            _parse_miss("an `interaction_id=` lease-threading argument", path)
