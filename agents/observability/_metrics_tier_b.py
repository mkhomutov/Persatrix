"""RFC 0030 Tier B (v0.3.8) salience-bid metric registration.

Split out of :mod:`agents.observability.metrics` so the parent module stays
under the project's 500-line review cap (see ``scripts/checks/file_size.py``),
mirroring the :mod:`._metrics_persona_tick` / :mod:`._metrics_wakes` splits.
The registered counter is assigned to the parent :class:`_Instruments`
instance, so call sites reach it via ``inst.channel_messages_tier_b_skipped``
with no rename.

``channel.messages.tier_b_skipped`` counts open-floor channel messages where
the Tier B salience bid was **skipped (not run)** — currently only the TB6
``channel_too_large`` case (the channel exceeded ``tier_b_max_channel_members``
and fell back to ``addressed``-only). It is deliberately distinct from
``channel.messages.gated`` (where ``policy=low_salience`` marks a bid that
*ran* and returned "stay silent"): a skip means Tier B is off for that
oversized channel, a gate means Tier B ran and the persona had nothing to
add — two different operational signals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

    from .metrics import _Instruments


def register(inst: _Instruments, meter: Meter) -> None:
    """Register ``channel.messages.tier_b_skipped`` on ``inst``."""
    inst.channel_messages_tier_b_skipped = meter.create_counter(
        name="channel.messages.tier_b_skipped",
        unit="{message}",
        description=(
            "Open-floor channel messages where the RFC 0030 Tier B salience "
            "bid was skipped (not run). Attribute: reason (channel_too_large)."
        ),
    )


def tier_b_skip_attrs(*, reason: str) -> dict[str, str]:
    """Attribute set for ``channel.messages.tier_b_skipped`` (RFC 0030 Tier
    B). ``channel_id`` is omitted — the skip is a coarse, low-cardinality
    signal; the cardinality rationale matches
    :func:`agents.observability.metrics.gate_attrs`."""
    return {"reason": reason}
