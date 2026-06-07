"""RFC 0030 Tier B (v0.3.8) salience-bid metric registration.

Split out of :mod:`agents.observability.metrics` so the parent module stays
under the project's 500-line review cap (see ``scripts/checks/file_size.py``),
mirroring the :mod:`._metrics_persona_tick` / :mod:`._metrics_wakes` splits.
The registered counter is assigned to the parent :class:`_Instruments`
instance, so call sites reach it via ``inst.channel_messages_salience_skipped``
with no rename.

``channel.messages.salience_skipped`` counts open-floor channel messages where
the Tier B salience bid was **skipped (not run)** — currently only the TB6
``channel_too_large`` case (the channel exceeded ``salience_max_channel_members``
and fell back to ``addressed``-only). It is deliberately distinct from
``channel.messages.gated`` (where ``policy=low_salience`` marks a bid that
*ran* — or *attempted* to run and failed closed — and resolved to "stay
silent"): a skip means Tier B is off for that oversized channel, a gate means
the bid resolved to silence — two different operational signals.

The low-salience ``gated`` fire additionally carries a ``reason`` attribute
(see :func:`salience_gated_attrs`) so a *fail-closed* branch (``lease_denied`` /
``llm_error`` / ``model_unresolvable`` / ``parse_failure``) is distinguishable
on a dashboard from genuine no-pile-on dampening (``below_threshold`` /
``declined``). Without it a ``fast``-model outage or wallet back-pressure
would be invisible — indistinguishable from the feature working as intended.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..response_gate import POLICY_LOW_SALIENCE

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

    from .metrics import _Instruments


def register(inst: _Instruments, meter: Meter) -> None:
    """Register ``channel.messages.salience_skipped`` on ``inst``."""
    inst.channel_messages_salience_skipped = meter.create_counter(
        name="channel.messages.salience_skipped",
        unit="{message}",
        description=(
            "Open-floor channel messages where the RFC 0030 Tier B salience "
            "bid was skipped (not run). Attribute: reason (channel_too_large)."
        ),
    )


def salience_skip_attrs(*, reason: str) -> dict[str, str]:
    """Attribute set for ``channel.messages.salience_skipped`` (RFC 0030 Tier
    B). ``channel_id`` is omitted — the skip is a coarse, low-cardinality
    signal; the cardinality rationale matches
    :func:`agents.observability.metrics.gate_attrs`."""
    return {"reason": reason}


def salience_gated_attrs(*, channel_id: str, reason: str) -> dict[str, str]:
    """Attribute set for the *Tier B* ``channel.messages.gated`` fire (RFC
    0030).

    The base gate fires with ``{channel_id, policy}`` only
    (:func:`agents.observability.metrics.gate_attrs`, RFC 0011 §D). The Tier B
    bias-to-silence suppression rides the same counter with
    ``policy=low_salience`` but adds the bid ``reason`` so a fail-closed branch
    (``lease_denied`` / ``llm_error`` / …) is distinguishable from genuine
    dampening (``below_threshold`` / ``declined``). The added dimension is
    bounded (a fixed, small ``reason`` vocabulary), and it only applies to the
    ``low_salience`` rows — the RFC 0011 gate rows keep their exact
    ``{channel_id, policy}`` shape."""
    return {
        "channel_id": channel_id,
        "policy": POLICY_LOW_SALIENCE,
        "reason": reason,
    }
