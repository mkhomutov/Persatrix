"""RFC 0030 Tier B (v0.3.8) — the action-loop seam for the salience bid.

The pure bid lives in :mod:`agents.tier_b_salience`. This module is the
*runtime orchestration* around it: it reads the bid inputs off the inbound
``CHANNEL_MESSAGE`` payload, enforces the TB6 channel-size cap, runs the bid,
emits the suppression metrics, and ingests a suppressed message into memory.
It is carved out of :mod:`agents.persona_runtime.action_loop` so that file
stays under the 500-line review cap (the same separation that pulled the
ingest sanitizer into ``channel_ingest.py`` and the LLM-error dispatch into
``llm_call_errors.py``).

The seam is invoked **only** on the open-floor admit
(:func:`agents.response_gate.is_open_floor_admit`) — the action loop checks
that before calling :func:`run_tier_b_gate`, so a directed ``@``-mention, a
DM, an ``observer``, and the self-sender never reach the bid (TB1). Of that
remainder, the bid runs only when the inbound event is **Tier-B-governed**
(the channel-level ``tier_b_active`` flag).

**Activation note (PR 2a):** the bid inputs (``tier_b_active``, per-member
``threshold``, ``channel_size``) are carried across the store/wire boundary
in **PR 2b** (the ``memberships.threshold`` SQLite migration + the
``ChannelMessageEvent`` proto fields). Until then ``tier_b_active`` is never
set, so :func:`run_tier_b_gate` short-circuits to "not applicable" and the
v0.3.7 response behaviour is unchanged — PR 2a is additive, mirroring the
inertness of Tier B PR 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..observability._metrics_tier_b import tier_b_skip_attrs
from ..observability.metrics import gate_attrs, try_get_instruments
from ..response_gate import POLICY_LOW_SALIENCE, is_open_floor_admit
from ..tier_b_salience import (
    DEFAULT_TIER_B_MAX_CHANNEL_MEMBERS,
    evaluate_salience,
    skip_bid_for_channel_size,
)

if TYPE_CHECKING:
    from ..persona_types import AgentEvent
    from ..response_gate import GateDecision

logger = logging.getLogger(__name__)

__all__ = ["TierBOutcome", "run_tier_b_gate"]

# Bid inputs carried on the inbound ``CHANNEL_MESSAGE`` payload alongside
# ``respond_policy`` / ``mentions``. Populated by the Go dispatcher in PR 2b.
_TIER_B_ACTIVE_KEY: str = "tier_b_active"
_TIER_B_THRESHOLD_KEY: str = "threshold"
_TIER_B_CHANNEL_SIZE_KEY: str = "channel_size"
_TIER_B_MAX_MEMBERS_KEY: str = "tier_b_max_channel_members"


@dataclass(frozen=True, slots=True)
class TierBOutcome:
    """What the action loop should do after the Tier B seam.

    Attributes:
        silence: ``True`` → suppress the turn (the no-pile-on path). The
            seam has already ingested the message into memory and emitted
            the suppression metric; the caller just returns ``DO_NOTHING``.
        user_message: When ``silence`` is ``False``, the formatted current
            message the seam already computed — handed back so the action
            loop reuses it instead of re-formatting.
        seed: When ``silence`` is ``False``, the conversation-window seed
            the seam already built for the bid — handed back so the action
            loop reuses it instead of re-fetching channel history.
    """

    silence: bool
    user_message: str | None = None
    seed: list[dict[str, Any]] | None = None


def _governed(event: AgentEvent) -> bool:
    return bool((event.payload or {}).get(_TIER_B_ACTIVE_KEY))


def _threshold(event: AgentEvent) -> float | None:
    """The member's salience ``threshold`` (``None`` → unset → bias-to-
    silence, TB2). A non-numeric value degrades to ``None``."""
    raw = (event.payload or {}).get(_TIER_B_THRESHOLD_KEY)
    if isinstance(raw, bool):  # bool is an int subclass — never a threshold
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _channel_size(event: AgentEvent) -> int | None:
    raw = (event.payload or {}).get(_TIER_B_CHANNEL_SIZE_KEY)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _max_members(event: AgentEvent) -> int:
    raw = (event.payload or {}).get(_TIER_B_MAX_MEMBERS_KEY)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_TIER_B_MAX_CHANNEL_MEMBERS
    return raw


async def run_tier_b_gate(
    agent: Any, event: AgentEvent, decision: GateDecision,
) -> TierBOutcome | None:
    """Run the Tier B salience bid for one admitted event, if applicable.

    Returns:
        ``None`` when the bid does not apply (not an open-floor admit, or the
        channel is not Tier-B-governed) — the caller proceeds with the normal
        turn. A :class:`TierBOutcome` otherwise: ``silence=True`` to suppress,
        or ``silence=False`` with the reusable ``user_message`` + ``seed``.

    ``agent`` is the :class:`_LLMPersonaAgent` (passed rather than bound as a
    method to keep ``action_loop.py`` thin); the seam uses its
    ``_format_event`` / ``_build_seed_messages`` / ``_store_event_episode``
    methods and its ``_llm_client`` / identity attributes.
    """
    if not (is_open_floor_admit(decision) and _governed(event)):
        return None

    # Formatted once here and handed back on the speak path so the action
    # loop does not re-format / re-fetch.
    user_message = agent._format_event(event)

    # TB6 — oversized channel: skip the bid entirely and fall back to
    # ``addressed``-only. An un-addressed open-floor participant therefore
    # stays silent on a channel above the cap (it was admitted only by the
    # open-floor branch, which Tier B now declines to honour at scale).
    if skip_bid_for_channel_size(
        channel_size=_channel_size(event), max_members=_max_members(event),
    ):
        inst = try_get_instruments()
        if inst is not None:
            inst.channel_messages_tier_b_skipped.add(
                1, attributes=tier_b_skip_attrs(reason="channel_too_large"),
            )
        await agent._store_event_episode(event, [])
        return TierBOutcome(silence=True)

    seed = await agent._build_seed_messages(event, user_message)
    salience = await evaluate_salience(
        llm_client=agent._llm_client,
        content=(event.payload or {}).get("content", ""),
        # The seed's last element is the current message — the bid receives
        # it via ``content``, so the transcript is everything *before* it.
        transcript=seed[:-1],
        agent_id=agent.agent_id,
        persona_name=agent.name,
        persona_role=agent.role,
        threshold=_threshold(event),
    )
    if not salience.speak:
        # No-pile-on: suppress the turn before paying for memory recall or
        # the quality LLM call. Still ingest (decide whether to respond, not
        # whether to remember — the gate-suppress discipline).
        inst = try_get_instruments()
        if inst is not None:
            inst.channel_messages_gated.add(
                1,
                attributes=gate_attrs(
                    channel_id=event.channel_id or "",
                    policy=POLICY_LOW_SALIENCE,
                ),
            )
        logger.debug(
            "Agent %s: Tier B salience bid suppressed turn (reason=%s, score=%s)",
            agent.agent_id, salience.reason, salience.score,
        )
        await agent._store_event_episode(event, [])
        return TierBOutcome(silence=True)

    return TierBOutcome(silence=False, user_message=user_message, seed=seed)
