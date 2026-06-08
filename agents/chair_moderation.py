"""RFC 0030 Layer 5 (v0.4.0) — the inert ``chair``-moderation seam.

The ``chair`` disposition ships in **v0.3.8** as a low-threshold *facilitator*
only (RFC 0030 Tier B): on the wire a ``chair`` is just a ``participant`` whose
salience ``threshold`` is the low ``DefaultChairThreshold``
(:mod:`internal/channels/config.go`), so it clears the cheap relevance bid
(:func:`agents.salience_bid.evaluate_salience`) readily and keeps a discussion
moving. That active facilitator half needs **no code here** — it is entirely the
low threshold delivered over the ``ChannelMessageEvent.threshold`` wire field.

This module is the chair's *other* half — the **moderator** that reads the
transcript and decides whether the conversation should continue, wrap up, or
terminate. That is **Layer 5, deferred to v0.4.0** (master plan
§Open-question status; the governance RFC 0030 §"Layer 5",
``docs/rfcs/0030-multi-agent-conversation-governance.md``). v0.3.8 wires the
seam but leaves it **inert**, the same
reserved-field discipline v0.3.7 used for ``threshold``:

* :func:`evaluate_chair_moderation` is the v0.4.0 attach point, but its v0.3.8
  body is a hard-coded :data:`ModeratorAction.CONTINUE`. A v0.3.8 ``chair``
  therefore **cannot unilaterally close a conversation** (TB5). Convergence in
  v0.3.8 comes *only* from the deterministic governance Layers 1/2/4 (cost
  ceiling / reply budget / end-of-interaction vote), never from the chair.
* The seam is **not invoked** by any runtime path in v0.3.8 — the action loop
  and the salience gate never call it. It exists so the v0.4.0 moderator has a
  typed home, and so the "chair cannot close" invariant is pinned by a test
  (``tests/unit/python/test_chair_moderation.py``) rather than left implicit.

When Layer 5 lands, the CONTINUE-only body is replaced by the transcript-level
decision and the runtime begins calling the seam; the public signature does not
need to change.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "ModeratorAction",
    "ModeratorDecision",
    "evaluate_chair_moderation",
]


class ModeratorAction(enum.Enum):
    """The transcript-level decision the v0.4.0 Layer 5 moderator will make.

    Only :attr:`CONTINUE` is reachable in v0.3.8. :attr:`WRAP_UP` and
    :attr:`TERMINATE` are **reserved** for the v0.4.0 moderator and are never
    returned by the inert seam (TB5 — a v0.3.8 ``chair`` cannot close a
    conversation)."""

    #: Keep the conversation open — the only verdict the inert v0.3.8 seam emits.
    CONTINUE = "continue"
    #: (Reserved, v0.4.0) Signal the discussion should move toward closing.
    WRAP_UP = "wrap_up"
    #: (Reserved, v0.4.0) Close the interaction now.
    TERMINATE = "terminate"


@dataclass(frozen=True, slots=True)
class ModeratorDecision:
    """A moderator verdict + a low-cardinality reason label.

    ``reason`` is ``"layer5_inert"`` for every v0.3.8 decision (the seam is
    dormant); the v0.4.0 moderator will populate it with the branch that drove a
    real continue/wrap-up/terminate so the decision is observable."""

    action: ModeratorAction
    reason: str


# The single decision the inert seam ever returns in v0.3.8: keep going. Bound
# once as a frozen singleton so the inertness is obvious at the call site.
_INERT_DECISION: Final[ModeratorDecision] = ModeratorDecision(
    action=ModeratorAction.CONTINUE,
    reason="layer5_inert",
)


def evaluate_chair_moderation(
    *,
    transcript: list[dict[str, Any]] | None = None,
    interaction_id: str | None = None,
) -> ModeratorDecision:
    """Inert v0.3.8 Layer-5 moderation seam — always :attr:`ModeratorAction.CONTINUE`.

    In v0.3.8 this returns :data:`_INERT_DECISION` for *any* input: a ``chair``
    cannot wrap up or terminate a conversation (TB5). The parameters mirror the
    signal the v0.4.0 moderator will read (the in-round ``transcript`` and the
    ``interaction_id`` scope) so wiring Layer 5 is a body change, not a
    signature change; both are optional and ignored today.

    This function is deliberately **not called** by any runtime path in v0.3.8
    (see the module docstring) — it is the reserved attach point only.
    """
    return _INERT_DECISION
