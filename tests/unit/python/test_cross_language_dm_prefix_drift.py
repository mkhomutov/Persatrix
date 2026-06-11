"""Cross-language drift pin for the ``dm:`` channel-id prefix.

The DM prefix is the channel-kind vocabulary several boundary modules
classify on, each with a local pin and a comment explaining why it does
not import a shared constant (``response_gate`` ↔ ``channel_reply``
would cycle; ``end_vote_action`` would couple the executor side to the
persona-runtime package for one literal). That is the repo's established
posture for wire-vocabulary literals — the ``@everyone`` sentinel is
pinned the same way in three places — and the posture's other half is a
lock-step drift guard (``test_channel_validation.py``'s
``test_sentinel_agrees_across_modules``). The vote producer (RFC 0030
producer plan PR 2) added a third ``_DM_CHANNEL_PREFIX`` pin for its DM
vote gate, and the close-propagation follow-up (PR 607) a fourth for
the agent-local vote-close mirror (``interaction_boundary.py`` — a gate
that must agree with ``end_vote_action.py``'s by construction, or a DM
vote the executor drops still closes the voter's local record). That is
the copy count where silent divergence stops being hypothetical: a
one-sided change leaves that side's suite green while a DM stops
classifying as a DM there — e.g. the vote gate lets a flagged vote into
a DM and Go's ``processEndVote`` counts it toward a quorum.

Python copies are imported and compared directly (stronger than a text
pin). The Go side has no named constant — [scopeForDM]'s id *builder*
(``internal/channels/channels.go``) is what mints every DM channel id —
so it is pinned as text, the sibling drift files' posture.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NoReturn

import pytest

_CHANNELS_GO = Path("internal/channels/channels.go")

# The canonical value, asserted explicitly (not just mutual equality):
# DM channel ids are persisted (message store, interaction scopes), so
# even a coordinated rename of every pin is a wire/storage migration,
# not a refactor — it must land as a deliberate update here.
_DM_PREFIX_CANONICAL = "dm:"


def _parse_miss(what: str, where: Path) -> NoReturn:
    """Fail with an actionable message on a parse miss (the
    ``test_cross_language_interaction_wire_drift`` posture): a refactor
    that hides the declaration must land as a deliberate update to this
    test's parse rules."""
    pytest.fail(
        f"could not find {what} in {where}. If it was renamed or "
        f"restructured, update the parse rule in this test to match the "
        f"new shape — the cross-language drift pin is part of the "
        f"contract.",
    )


def test_dm_prefix_agrees_across_python_modules() -> None:
    """Every Python pin of the DM channel-id prefix MUST be equal — the
    classifying gates (response gate, DM-reply fallback, DM vote gate),
    the wire validator's prefix↔type table, and the memory-scope
    builder all read or mint the same vocabulary."""
    from agents.channel_validation import _CHANNEL_TYPE_PREFIXES
    from agents.end_vote_action import _DM_CHANNEL_PREFIX as VOTE_GATE_PREFIX
    from agents.memory.scopes import _DM_PREFIX as SCOPE_PREFIX
    from agents.persona_runtime.channel_reply import (
        _DM_CHANNEL_PREFIX as REPLY_FALLBACK_PREFIX,
    )
    from agents.persona_runtime.interaction_boundary import (
        _DM_CHANNEL_PREFIX as VOTE_CLOSE_PREFIX,
    )
    from agents.response_gate import _DM_CHANNEL_PREFIX as GATE_PREFIX

    assert (
        GATE_PREFIX
        == REPLY_FALLBACK_PREFIX
        == VOTE_GATE_PREFIX
        == VOTE_CLOSE_PREFIX
        == _CHANNEL_TYPE_PREFIXES["dm"]
        == SCOPE_PREFIX
        == _DM_PREFIX_CANONICAL
    )


def test_dm_prefix_agrees_with_go_id_builder() -> None:
    """Go's DM id builder — the producer that mints every DM channel id
    the Python pins classify — MUST mint the same prefix."""
    src = _CHANNELS_GO.read_text(encoding="utf-8")
    m = re.search(r'return\s+"([^"]+)"\s*\+\s*a\s*\+\s*":"\s*\+\s*b', src)
    if m is None:
        _parse_miss('the DM id builder `return "dm:" + a + ":" + b`', _CHANNELS_GO)
    assert m.group(1) == _DM_PREFIX_CANONICAL
