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

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..response_gate import POLICY_LOW_SALIENCE

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram, Meter

    from .metrics import _Instruments


# ── RFC 0051 PR 6 (v0.3.10) go-live deliberation telemetry ───────────────────
#
# The observability that makes an active ``reasoning`` default safe to run: the
# deliberation rate, the silence rate charted by ``reason_code``/``mode``, the
# deliberation-latency histogram (the pass is a serial ``fast`` call *before*
# compose — it reuses the ``agent.llm.duration`` instrument *shape*), and a
# budget-starvation counter (a deliberation starved to silence by a low
# ``interaction_budget_tokens`` — operationally distinct from a semantic "nothing
# to add"). All kept distinct from the Tier-B ``channel.messages.gated`` rows.
#
# These instruments live in module state HERE, not on :class:`_Instruments`,
# because ``metrics.py`` is at the 500-line review cap and cannot gain the class
# annotations the ``inst.X`` pattern needs (PR plan §File-size constraints —
# "all new deliberation/reasoning instruments land here ... metrics.py must not
# gain net lines"). :func:`register` re-creates them on every ``_Instruments``
# construction (every ``init_metrics``), so they always track the live meter; the
# parse-failure counter above remains on ``inst`` as the never-gated safety net.

# The bid ``reason_code`` for a budget/lease starvation (a denied lease OR the
# wallet ``RESOURCE_EXHAUSTED`` cap — see ``agents/salience_bid.py``).
# Operationally distinct from a semantic-silence code, so it gets its own counter.
_BUDGET_STARVED_REASON: str = "lease_denied"


@dataclass
class _DeliberationInstruments:
    """The module-owned RFC 0051 PR 6 deliberation instruments."""

    total: Counter
    suppressed: Counter
    duration: Histogram
    budget_starved: Counter


_deliberation: _DeliberationInstruments | None = None


def record_deliberation(
    *, mode: str, reason_code: str, spoke: bool, duration_ms: float,
) -> None:
    """Record the go-live telemetry for one structured-rung deliberation.

    Always emits the rate (``deliberation.total``) + the latency
    (``deliberation.duration``); on a *silence* verdict additionally emits the
    suppress row (``deliberation.suppressed``, charted by ``reason_code`` AND
    ``mode``) and, when the silence was a budget/lease starvation, the
    ``deliberation.budget_starved`` counter. A no-op until :func:`register` has
    run (metrics unconfigured), so a call site never has to guard. ``reason_code``
    is the closed-vocabulary bid label and ``mode`` is the closed rung set, so the
    cardinality stays bounded — both safe as metric dimensions.

    **Best-effort, like the sibling ``agent.deliberated`` audit emit.** This runs
    on the active-by-default ``bid`` path *after* the deliberation has already
    happened, so a metric-export hiccup must never propagate and undo the turn —
    the same "the decision already happened, don't block on the egress" contract
    (RFC 0051 §Security). The OTel SDK already swallows recording errors, but the
    suppress makes the no-block guarantee explicit and matches the audit path."""
    di = _deliberation
    if di is None:
        return
    with contextlib.suppress(Exception):
        di.total.add(1, attributes={"mode": mode})
        di.duration.record(duration_ms, attributes={"mode": mode})
        if not spoke:
            di.suppressed.add(1, attributes={"reason_code": reason_code, "mode": mode})
            if reason_code == _BUDGET_STARVED_REASON:
                di.budget_starved.add(1, attributes={"mode": mode})


def register(inst: _Instruments, meter: Meter) -> None:
    """Register the Tier-B salience counters on ``inst`` plus the module-owned
    RFC 0051 deliberation instruments (see :data:`_deliberation`)."""
    inst.channel_messages_salience_skipped = meter.create_counter(
        name="channel.messages.salience_skipped",
        unit="{message}",
        description=(
            "Open-floor channel messages where the RFC 0030 Tier B salience "
            "bid was skipped (not run). Attribute: reason (channel_too_large)."
        ),
    )
    # RFC 0051 Phase 1a — the deliberation parse-failure safety net. A
    # structured ``should_post`` verdict that fails to parse falls *closed* to
    # silence; without a first-class counter that fail-closed error is buried in
    # the ``channel.messages.gated{reason=parse_failure}`` suppression totals and
    # reads as intended no-pile-on dampening. This counter is the **mandatory,
    # never-gated** signal that makes a silent parser break alertable. Kept
    # distinct from ``channel.messages.gated`` on purpose (two operational
    # signals: a broken parser vs. the feature working as designed). It is
    # **additive, not a re-route**: once the seam threads reasoning (PR 2) a
    # parse failure still also fires ``channel.messages.gated{reason=parse_failure}``,
    # so the two are not disjoint — do not sum them.
    inst.deliberation_parse_failures = meter.create_counter(
        name="deliberation.parse_failures",
        unit="{failure}",
        description=(
            "RFC 0051 structured deliberation verdicts that failed to parse and "
            "fell closed to silence. Attribute: mode (bid|plan; off marks a "
            "non-structured-mode misuse of the public parser). Distinct from "
            "channel.messages.gated — a broken parser, not no-pile-on dampening."
        ),
    )
    # RFC 0051 PR 6 go-live — the module-owned deliberation instruments (see the
    # block comment above for why they are not on ``inst``).
    global _deliberation
    _deliberation = _DeliberationInstruments(
        total=meter.create_counter(
            name="deliberation.total",
            unit="{deliberation}",
            description=(
                "RFC 0051 structured deliberations run on an open-floor admit. "
                "Attribute: mode (bid|plan). The deliberation RATE; the silence "
                "fraction is deliberation.suppressed / this total (both carry "
                "mode, so the ratio holds per rung as well as in aggregate)."
            ),
        ),
        suppressed=meter.create_counter(
            name="deliberation.suppressed",
            unit="{deliberation}",
            description=(
                "RFC 0051 deliberations that resolved to silence, charted by cause "
                "and rung. Attributes: reason_code (the closed bid vocabulary — "
                "only_agreeing|already_answered|nothing_to_add|lease_denied|…) and "
                "mode (bid|plan), so the silence fraction (suppressed/total) is "
                "computable per rung. Distinct from channel.messages.gated; and a "
                "TB6 oversized-channel skip is NOT a deliberation, so it rides "
                "channel.messages.salience_skipped, not this counter."
            ),
        ),
        duration=meter.create_histogram(
            name="deliberation.duration",
            unit="ms",
            description=(
                "Wall-clock duration of the RFC 0051 deliberation pass — a serial "
                "fast-model call before compose, measured around the bid only (the "
                "agent.deliberated audit emit is excluded). Reuses the "
                "agent.llm.duration instrument shape. Attribute: mode (bid|plan). "
                "Charts the latency the flip adds; the structured rung also costs "
                "modestly more per bid than the off scalar gate (larger prompt + "
                "higher max_tokens), captured by existing wallet accounting."
            ),
        ),
        budget_starved=meter.create_counter(
            name="deliberation.budget_starved",
            unit="{deliberation}",
            description=(
                "RFC 0051 deliberations starved to silence by a denied lease / "
                "exhausted interaction_budget_tokens (reason_code=lease_denied) — "
                "operationally distinct from a semantic 'nothing to add'. "
                "Attribute: mode (bid|plan)."
            ),
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


def deliberation_parse_failure_attrs(*, mode: str) -> dict[str, str]:
    """Attribute set for ``deliberation.parse_failures`` (RFC 0051 Phase 1a).
    ``mode`` (``bid``/``plan``) is the only dimension — bounded and small —
    so an operator can see *which* reasoning rung is breaking; the rationale
    matches :func:`salience_skip_attrs`. The caller
    (:func:`agents.salience_deliberation.parse_verdict`) clamps ``mode`` to the
    bounded set before passing it, so a rogue value never reaches the label."""
    return {"mode": mode}
