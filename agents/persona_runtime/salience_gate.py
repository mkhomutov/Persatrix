"""RFC 0030 Tier B (v0.3.8) — the action-loop seam for the salience bid.

The pure bid lives in :mod:`agents.salience_bid`. This module is the
*runtime orchestration* around it: it reads the bid inputs off the inbound
``CHANNEL_MESSAGE`` payload, enforces the TB6 channel-size cap, runs the bid,
emits the suppression metrics, and ingests a suppressed message into memory.
It is carved out of :mod:`agents.persona_runtime.action_loop` so that file
stays under the 500-line review cap (the same separation that pulled the
ingest sanitizer into ``channel_ingest.py`` and the LLM-error dispatch into
``llm_call_errors.py``).

The seam is invoked **only** on the open-floor admit
(:func:`agents.response_gate.is_open_floor_admit`) — the action loop checks
that before calling :func:`run_salience_gate`, so a directed ``@``-mention, a
DM, an ``observer``, and the self-sender never reach the bid (TB1). Of that
remainder, the bid runs only when the inbound event is **Tier-B-governed**
(the channel-level ``salience_gated`` flag).

**Activation (PR 2b, landed):** the bid inputs (``salience_gated``, per-member
``threshold``, ``channel_size``) now cross the store/wire boundary end-to-end —
the ``memberships.threshold``/``salience_gated`` SQLite columns persist them and
the ``ChannelMessageEvent`` proto fields deliver them here (populated by the Go
dispatcher in ``internal/channels/grpc_dispatcher.go``; lifted onto the event
payload in ``agents/server_servicers.py``). :func:`run_salience_gate` therefore
runs the bid for real on a Tier-B-governed open-floor admit. It still
short-circuits to "not applicable" when the event is **not** governed
(``salience_gated`` unset) — a bare legacy ``always`` member, which keeps
replying unconditionally — so the feature stays additive over the v0.3.7
response behaviour.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final

from ..observability._metrics_salience import (
    record_deliberation,
    salience_gated_attrs,
    salience_skip_attrs,
)
from ..observability.metrics import try_get_instruments
from ..response_gate import is_open_floor_admit
from ..salience_bid import (
    DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS,
    evaluate_salience,
    skip_bid_for_channel_size,
)
from ..salience_deliberation import MODE_OFF, MODE_PLAN, is_known_mode, is_structured
from .deliberation_plan import CompositionPlan, parse_plan
from .wallet_cause import cause_for_event, lease_interaction_id_for_event

if TYPE_CHECKING:
    from ..persona_types import AgentEvent
    from ..response_gate import GateDecision

logger = logging.getLogger(__name__)

__all__ = ["SalienceOutcome", "run_salience_gate"]

# RFC 0051 PR 2 — the deliberation audit event name (RFC 0009 §G shape). The
# canonical registry + severity classification live Go-side in
# ``internal/security/audit_event.go`` (``AuditAgentDeliberated``,
# telemetry-class); the decision is made *here* in the Python runtime, which
# has no Go audit RPC, so it emits the record on this structured-log egress.
_AUDIT_EVENT_DELIBERATED: Final[str] = "agent.deliberated"


def _emit_deliberated_audit(
    *,
    agent_id: str,
    channel_id: str,
    should_post: bool,
    reason_code: str,
    transcript_turns: int,
) -> None:
    """Emit the RFC 0051 ``agent.deliberated`` audit record (post-commit).

    Carries the *decision* (``should_post``), the closed-set ``reason_code``,
    and low-cardinality *counts* — and **never** the verbatim ``reason_note`` or
    the ``CompositionPlan`` (RFC 0051 §E privacy wall / §Security). The wall is
    structural: the payload is bool/enum/int by construction, so there is nothing
    private to leak and no free-text redaction is needed — the seam never reads
    ``reason_note`` onto it.

    Best-effort, post-commit: the deliberation has already happened, so a
    structured-log hiccup must never undo or block the turn. This applies the
    RFC 0009 §G "the decision already happened — don't drop the record" rule on
    the Python emit path rather than the Go ``context.WithoutCancel`` API
    (RFC 0051 §Security)."""
    with contextlib.suppress(Exception):
        logger.info(
            _AUDIT_EVENT_DELIBERATED,
            extra={
                "audit": True,
                "agent_id": agent_id,
                "channel_id": channel_id,
                "should_post": should_post,
                "reason_code": reason_code,
                "transcript_turns": transcript_turns,
            },
        )

# Bid inputs carried on the inbound ``CHANNEL_MESSAGE`` payload alongside
# ``respond_policy`` / ``mentions``. Populated by the Go dispatcher and lifted
# onto the payload in ``server_servicers.py`` (RFC 0030 Tier B, PR 2b).
_SALIENCE_GATED_KEY: str = "salience_gated"
_SALIENCE_THRESHOLD_KEY: str = "threshold"
_SALIENCE_CHANNEL_SIZE_KEY: str = "channel_size"
_SALIENCE_MAX_MEMBERS_KEY: str = "salience_max_channel_members"
# RFC 0051 PR 6 go-live — the channel's resolved reasoning rung, lifted off the
# wire by ``channel_wire_metadata.channel_event_payload``. Absent / empty (a
# pre-v0.3.10 producer) or any unrecognised value resolves to ``off``, the scalar
# score gate (byte-for-byte v0.3.8), so the wiring is additive.
_REASONING_MODE_KEY: str = "reasoning_mode"


@dataclass(frozen=True, slots=True)
class SalienceOutcome:
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
        plan: When ``silence`` is ``False`` *and* ``mode: plan`` produced a
            parseable plan, the private :class:`CompositionPlan` the action loop
            threads into the Tier-C compose (RFC 0051 §C). ``None`` on every
            other rung (``off``/``bid``) and when the plan failed to parse —
            compose then proceeds unplanned, never blocked. The plan rides here,
            **not** on the pure bid's ``SalienceDecision`` (RFC 0051 §C
            "why two types"); it is never an ``AgentAction`` / never persisted
            (the §E wall, pinned by ``test_deliberation_no_leak.py``).
    """

    silence: bool
    user_message: str | None = None
    seed: list[dict[str, Any]] | None = None
    plan: CompositionPlan | None = None


def _governed(event: AgentEvent) -> bool:
    return bool((event.payload or {}).get(_SALIENCE_GATED_KEY))


def _threshold(event: AgentEvent) -> float | None:
    """The member's salience ``threshold`` (``None`` → unset → bias-to-
    silence, TB2). A non-numeric *or out-of-range* value degrades to
    ``None``: a threshold is a score floor in ``[0, 1]``, so a stray value
    (e.g. a future wire bug) becomes "unset" rather than permanently muting
    (>1) or admitting everything (<0).

    Boundary note — ``0.0`` is a *valid, deliberate* floor, not unset: it
    means "speak on any parseable score" (the bid still runs and can still
    fail closed on parse/lease errors, but no score is too low to clear the
    bar). It is the opposite extreme from ``None`` (which demands a decisive
    ``_DECISIVE_SCORE``). The asymmetry is intentional — an operator setting
    ``0.0`` is opting a member out of the no-pile-on dampening (e.g. a
    facilitator), whereas an *absent* threshold must stay conservative — but
    it is a sharp edge: ``0.0`` and "field omitted" are worlds apart."""
    raw = (event.payload or {}).get(_SALIENCE_THRESHOLD_KEY)
    if isinstance(raw, bool):  # bool is an int subclass — never a threshold
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if 0.0 <= value <= 1.0 else None
    return None


def _channel_size(event: AgentEvent) -> int | None:
    raw = (event.payload or {}).get(_SALIENCE_CHANNEL_SIZE_KEY)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _max_members(event: AgentEvent) -> int:
    raw = (event.payload or {}).get(_SALIENCE_MAX_MEMBERS_KEY)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS
    return raw


def _reasoning_mode(event: AgentEvent) -> str:
    """The channel's resolved reasoning rung off the wire payload (RFC 0051 PR 6).

    Resolves to ``off`` for an absent / empty / non-string value (a pre-v0.3.10
    producer) and for any unrecognised string — so a malformed or future rung
    fails *safe* to the scalar score gate rather than reaching the structured
    path. A recognised ``bid``/``plan`` (or an explicit ``off``) is returned
    verbatim; the downstream bid clamps the budget/grammar off it."""
    raw = (event.payload or {}).get(_REASONING_MODE_KEY)
    if isinstance(raw, str) and is_known_mode(raw):
        return raw
    return MODE_OFF


async def run_salience_gate(
    agent: Any, event: AgentEvent, decision: GateDecision,
    *, mode: str | None = None,
) -> SalienceOutcome | None:
    """Run the Tier B salience bid for one admitted event, if applicable.

    Returns:
        ``None`` when the bid does not apply (not an open-floor admit, or the
        channel is not Tier-B-governed) — the caller proceeds with the normal
        turn. A :class:`SalienceOutcome` otherwise: ``silence=True`` to suppress,
        or ``silence=False`` with the reusable ``user_message`` + ``seed``.

    ``agent`` is the :class:`_LLMPersonaAgent` (passed rather than bound as a
    method to keep ``action_loop.py`` thin); the seam uses its
    ``_format_event`` / ``_build_seed_messages`` / ``_store_event_episode``
    methods and its ``_llm_client`` / identity attributes.

    ``mode`` (RFC 0051 §C/§G) selects the bid's verdict grammar. **Live as of PR
    6**: ``None`` (the ``action_loop`` call site, which passes nothing) resolves
    the rung off the wire payload (:func:`_reasoning_mode`) — the channel's
    router-resolved ``reasoning.mode``, ``off`` for a pre-v0.3.10 / ungoverned
    channel and ``bid`` for a governed one post the go-live default flip. An
    explicit ``mode`` argument overrides the payload (test injection). On the
    structured rungs (``bid``/``plan``) the seam additionally emits the
    ``agent.deliberated`` audit (decision + ``reason_code`` + counts, never the
    ``reason_note`` or plan, RFC 0051 §E). The TB6 channel-too-large *skip* is not
    a deliberation, so it emits no audit even under reasoning.
    """
    if not (is_open_floor_admit(decision) and _governed(event)):
        return None

    # RFC 0051 PR 6 — resolve the rung off the wire when the caller did not pin
    # one (the production ``action_loop`` path). An explicit ``mode`` (tests)
    # wins. Resolved only after the open-floor + governed guard so an ungoverned
    # or non-admit event short-circuits without touching the payload.
    if mode is None:
        mode = _reasoning_mode(event)

    # TB6 — oversized channel: skip the bid entirely and fall back to
    # ``addressed``-only. An un-addressed open-floor participant therefore
    # stays silent on a channel above the cap (it was admitted only by the
    # open-floor branch, which Tier B now declines to honour at scale). This
    # cap check is a pure, cheap predicate and runs *before* ``_format_event``
    # so an oversized governed channel pays nothing but the check + the ingest
    # (the formatted message would only be discarded on this path).
    if skip_bid_for_channel_size(
        channel_size=_channel_size(event), max_members=_max_members(event),
    ):
        inst = try_get_instruments()
        if inst is not None:
            inst.channel_messages_salience_skipped.add(
                1, attributes=salience_skip_attrs(reason="channel_too_large"),
            )
        await agent._store_event_episode(event, [])
        return SalienceOutcome(silence=True)

    # Formatted once here and handed back on the speak path so the action
    # loop does not re-format / re-fetch.
    user_message = agent._format_event(event)

    # Cost note: reaching here (an open-floor admit of a governed channel)
    # always pays a conversation-window fetch (``_build_seed_messages``, one
    # history round-trip) **and** the leased ``fast`` bid below — the per-
    # message price of Tier B. The "no-pile-on / idle-cost-zero" win is
    # specifically about *not* paying the expensive half (memory recall + the
    # quality LLM turn) when the bid stays silent; it is not zero-cost. The
    # cheap-bid-vs-full-turn trade is the whole point, but a busy governed
    # channel does see one extra fetch + bid per open-floor message.
    seed = await agent._build_seed_messages(event, user_message)
    # RFC 0051 PR 3 — collect the bid's raw verdict text *only* on the ``plan``
    # rung, so the seam can parse the private CompositionPlan from it on the
    # speak path. ``None`` on ``off``/``bid`` means the bid surfaces nothing.
    deliberation_text: list[str] | None = [] if mode == MODE_PLAN else None
    # RFC 0051 PR 6 — time the deliberation pass (a serial ``fast`` call before
    # compose) so the latency histogram can chart the added cost of the flip.
    _delib_start = perf_counter()
    salience = await evaluate_salience(
        llm_client=agent._llm_client,
        content=(event.payload or {}).get("content", ""),
        # The seed's last element is the current message — the bid receives
        # it via ``content``, so the transcript is everything *before* it.
        #
        # Known limitation (the dedup is window-bounded): ``seed`` is the RFC
        # 0034 conversation window *after* admission/truncation (``max_turns``
        # etc.), so the bid's "has this already been said?" judgement only
        # sees turns inside that window. If the turn that already made the
        # persona's point scrolled out of the window, the bias-to-silence
        # dedup cannot see it and pile-on can recur on long threads. Accepted
        # for the bid core (PR 2a); widening the bid's history independently
        # of the quality window is a calibration concern (amendment OQ #3).
        transcript=seed[:-1],
        agent_id=agent.agent_id,
        persona_name=agent.name,
        persona_role=agent.role,
        threshold=_threshold(event),
        # Bill the bid under the *same* wallet cause the quality turn would use
        # for this event (e.g. CAUSE_CHAT for a chat-shaped message), rather
        # than a hardcoded constant. The ISSUE-0064 sub-agent override that
        # ``lease_attribution_for_event`` layers on top is a TASK_ASSIGNED-only
        # concern and cannot reach an open-floor channel admit, so deriving the
        # cause alone (and keeping ``agent_id`` as the resolving persona) is
        # sufficient and matches the quality turn for every reachable case.
        cause=cause_for_event(event),
        # RFC 0030 producer plan PR 2: same interaction attribution as the
        # quality turn — see evaluate_salience's interaction_id contract.
        interaction_id=lease_interaction_id_for_event(event),
        # RFC 0051 PR 2 — dark by default (action_loop passes nothing): ``off``
        # is the scalar score gate; ``bid``/``plan`` is the structured verdict.
        mode=mode,
        # RFC 0051 PR 3 — under ``plan`` the bid hands its raw verdict text back
        # here so the seam can parse the CompositionPlan (keeps the pure bid's
        # SalienceDecision un-widened, RFC 0051 §C). ``None`` off the ``plan`` rung.
        deliberation_out=deliberation_text,
    )

    # RFC 0051 PR 2 — record the deliberation the instant the verdict is in,
    # *before* the suppression metric / memory ingest below, so the audit
    # survives a downstream failure (the decision already happened, §Security).
    # Only the structured rungs deliberate; ``off`` is the scalar gate and stays
    # byte-for-byte v0.3.8 (no new egress — the dark-ship contract).
    if is_structured(mode):
        _emit_deliberated_audit(
            agent_id=agent.agent_id,
            channel_id=event.channel_id or "",
            should_post=salience.speak,
            reason_code=salience.reason,
            # ``transcript`` the bid scored is everything *before* the current
            # message (the seed's last element), per the call above.
            transcript_turns=max(0, len(seed) - 1),
        )
        # RFC 0051 PR 6 go-live telemetry: the deliberation rate + latency, and on
        # a silence verdict the suppress-by-reason_code + budget-starvation rows.
        # Distinct from the ``channel.messages.gated`` suppression counter below.
        record_deliberation(
            mode=mode,
            reason_code=salience.reason,
            spoke=salience.speak,
            duration_ms=(perf_counter() - _delib_start) * 1000.0,
        )

    if not salience.speak:
        # No-pile-on: suppress the turn before paying for memory recall or
        # the quality LLM call. Still ingest (decide whether to respond, not
        # whether to remember — the gate-suppress discipline).
        inst = try_get_instruments()
        if inst is not None:
            # Ride the existing ``gated`` counter with ``policy=low_salience``,
            # but carry the bid ``reason`` so a fail-closed branch
            # (lease_denied / llm_error / …) is distinguishable on a dashboard
            # from genuine dampening (below_threshold / declined) — otherwise a
            # ``fast``-model outage or wallet back-pressure looks exactly like
            # the no-pile-on feature working.
            inst.channel_messages_gated.add(
                1,
                attributes=salience_gated_attrs(
                    channel_id=event.channel_id or "",
                    reason=salience.reason,
                ),
            )
        logger.debug(
            "Agent %s: Tier B salience bid suppressed turn (reason=%s, score=%s)",
            agent.agent_id, salience.reason, salience.score,
        )
        await agent._store_event_episode(event, [])
        return SalienceOutcome(silence=True)

    # RFC 0051 PR 3 — on the ``plan`` rung, parse the private CompositionPlan
    # from the bid's verdict text and carry it back for the Tier-C compose.
    # Fail-closed to *no plan* (not to silence): an unparseable plan composes
    # unplanned rather than blocking a post the gate already admitted (§Phase 2).
    plan = parse_plan(deliberation_text[0]) if deliberation_text else None
    return SalienceOutcome(
        silence=False, user_message=user_message, seed=seed, plan=plan,
    )
