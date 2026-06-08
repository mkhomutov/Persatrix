"""RFC 0030 Tier B (v0.3.8) — the leased ``fast``-model salience bid.

Tier A (v0.3.7, :mod:`agents.response_gate`) is the *free, deterministic*
eligibility filter: it sheds ``@``-directed-elsewhere traffic, ``observer``
members, and the self-sender, and admits an open-floor ``participant`` with
``reason="policy_always"``. Tier B is the *cheap, dynamic* layer that runs
**only** on that open-floor remainder and decides who actually has something
to add — the no-pile-on win
(``docs/rfcs/0030-amendment-relevance-gated-response.md``).

The bid is a single ``fast``-model call asking the persona "do you have
something worth adding to this thread that has not already been said?",
reading the v0.3.7 in-round transcript (RFC 0034 Phase 2 group working
memory) so "someone already made my point" resolves to silence. It is
**downstream of the pure gate** — Tier A stays a pure function; this stage
issues a leased LLM call and so lives in the action-loop caller, not inside
``evaluate_response_gate`` (see the gate's ``is_open_floor_admit`` helper).

Load-bearing invariants (amendment OQs / master-plan §Open-question status):

* **TB2 — bias-to-silence.** An unset (``None``) ``threshold`` requires a
  *decisively* high score (:data:`_DECISIVE_SCORE`). A parse failure, a
  lease denial, an unresolvable ``fast`` alias, or ``score < threshold`` all
  resolve to ``speak=False``. Conservative by construction; calibration is a
  post-soak concern (amendment OQ #3).
* **TB3 — every bid is leased + attributable.** The call carries the
  resolving ``agent_id`` and a wallet ``cause`` *derived from the inbound
  event* (defaulting to ``CAUSE_CHANNEL_MESSAGE``) so the bid bills the same
  cause as the event's quality turn; the RFC 0023 wallet bounds + attributes
  it, and a denied lease fails *closed* (RFC 0023 §F).
* **TB6 — channel-size cap.** Above ``salience_max_channel_members`` the caller
  skips the bid and falls back to ``addressed``-only so bid fan-out stays
  small on large channels (amendment OQ #4). The pure
  :func:`skip_bid_for_channel_size` predicate lives here for testability;
  the caller owns the fallback.

**Activation note (PR 2a):** this module is the bid *core*. The per-member
``threshold`` and the ``channel_size`` it consumes are carried across the
store/wire boundary in PR 2b (the SQLite ``memberships.threshold`` migration
+ the ``ChannelMessageEvent`` proto field). Until then the action-loop seam
(:func:`agents.persona_runtime.salience_gate.run_salience_gate`) is dormant — it
fires only when the inbound event is flagged Tier-B-governed — so PR 2a is
additive and the v0.3.7 response behaviour is unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import grpc
import grpc.aio

from .generated import wallet_pb2 as walletpb
from .model_aliases import resolve as resolve_model
from .salience_addressing import NLAddressing, detect_nl_addressing
from .wallet_client import BudgetExceededError

if TYPE_CHECKING:
    from .llm_client import LLMClient

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS",
    "NLAddressing",
    "SalienceDecision",
    "detect_nl_addressing",
    "evaluate_salience",
    "skip_bid_for_channel_size",
]

# The ``fast`` role alias (RFC 0033) the bid always runs on, regardless of
# the persona's quality model. Resolved per-call so an operator's provider
# choice is honoured; an unconfigured/unknown alias fails *closed* (silence).
_BID_MODEL_ALIAS: Final[str] = "fast"

# A short, structured output budget — the bid is a yes/no + score, not prose.
# If a verbose model truncates before emitting ``score:`` the parse fails and
# the bid stays silent — the fail-closed direction (TB2), so the tight budget
# is safe.
_BID_MAX_OUTPUT_TOKENS: Final[int] = 64
# Low temperature: the bid is a judgement, not a creative turn.
_BID_TEMPERATURE: Final[float] = 0.0

# TB2: the implicit bar an *unset* (``None``) threshold must clear. An unset
# threshold biases to silence, so only a decisively-high score speaks.
_DECISIVE_SCORE: Final[float] = 0.8

# PR 3 — natural-language addressing (TB4 / amendment OQ #2). A free-text
# invitation of a named person ("let's hear from Iron Fox") biases the bid's
# *bar* toward or away from the bidding persona — it is **never** a hard
# pre-filter (structured ``@``-mentions remain the only deterministic
# directed-elsewhere drop, owned by Tier A). Being invited by name lowers the
# bar (lean toward speaking); seeing someone *else* invited raises it (defer
# unless decisively novel). The shift is symmetric and deliberately modest so
# a non-named persona with a genuinely strong contribution still clears.
_ADDRESSED_SELF_BONUS: Final[float] = 0.2
_ADDRESSED_OTHER_PENALTY: Final[float] = 0.2
# Decimal places the shifted bar is rounded to so float drift (e.g.
# ``0.4 + 0.2 == 0.6000000000000001``) cannot turn the inclusive floor into an
# epsilon-exclusive one.
_BAR_PRECISION: Final[int] = 6

# TB6: default channel-member cap above which the bid is skipped (the channel
# falls back to ``addressed``-only). A non-positive value disables the cap.
DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS: Final[int] = 20

# Bid output grammar. The bid is asked to answer on two lines:
#   speak: yes|no
#   score: 0.0-1.0
# The score governs the threshold gate. ``speak:`` is a one-way veto *toward
# silence* (TB2): an explicit ``speak: no`` stays silent even when the score
# clears the bar, but ``speak: yes`` never overrides a below-bar score. A
# missing ``speak:`` line falls through to the score alone. Parsing is
# forgiving of surrounding prose, but a missing score is a parse failure
# (→ silence, TB2).
#
# The trailing ``(?!\d)`` guards the *one* direction the parser could fail
# *toward* speech: without it, a model answering on a 0-10 or 0-100 scale
# (``score: 10`` / ``score: 100``) would partial-match the leading ``1``,
# clamp to a clearing ``1.0``, and wrongly admit. The lookahead rejects a
# would-be score immediately followed by another digit so it falls through
# to ``parse_failure`` → silence (TB2). It deliberately forbids only a
# trailing *digit*, not a trailing ``.`` — a sentence-final ``score: 0.5.``
# still reads as ``0.5`` — and an in-range ``1.5`` still matches and clamps.
_SCORE_RE: Final[re.Pattern[str]] = re.compile(
    r"score\s*[:=]\s*(?P<score>[01](?:\.\d+)?|0?\.\d+)(?!\d)",
    re.IGNORECASE,
)
_SPEAK_RE: Final[re.Pattern[str]] = re.compile(
    r"speak\s*[:=]\s*(?P<speak>yes|no)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SalienceDecision:
    """Outcome of :func:`evaluate_salience`.

    Attributes:
        speak: ``True`` when the persona should proceed to the (expensive)
            quality turn; ``False`` to stay silent (the no-pile-on path).
        score: The parsed salience score in ``[0, 1]``, or ``None`` when the
            bid could not be scored (parse failure / lease denial / error).
        reason: Low-cardinality branch label. For every *suppressing* verdict
            the action-loop seam emits it as the ``reason`` attribute on the
            ``channel.messages.gated`` counter (alongside
            ``policy=low_salience``) so a fail-closed branch is distinguishable
            on a dashboard from genuine no-pile-on dampening — not only in a
            DEBUG log. Values: ``salient`` (a speak verdict — never gated) /
            ``declined`` (explicit ``speak: no`` veto) / ``below_threshold`` /
            ``parse_failure`` / ``lease_denied`` (a denied/unreachable lease
            **or** the wallet's ``RESOURCE_EXHAUSTED`` active-lease cap) /
            ``llm_error`` (any other provider/gRPC failure) /
            ``model_unresolvable``.
    """

    speak: bool
    score: float | None
    reason: str


def skip_bid_for_channel_size(
    *, channel_size: int | None, max_members: int,
) -> bool:
    """Return ``True`` when the channel is too large to bid on (TB6).

    Above ``max_members`` the caller skips the bid and falls back to
    ``addressed``-only. A non-positive ``max_members`` disables the cap; an
    unknown/zero ``channel_size`` cannot trigger it (no information is not a
    reason to suppress).
    """
    if max_members <= 0:
        return False
    if not channel_size or channel_size <= 0:
        return False
    return channel_size > max_members


def _addressing_note(addressing: NLAddressing) -> str:
    """A short bid-prompt nudge reflecting the NL-addressing signal (PR 3).

    The note is advisory only — it never instructs the model to stay silent
    outright (that would re-introduce a hard NL filter); it leans the
    judgement, and the deterministic bar shift in :func:`evaluate_salience`
    carries the load that does not depend on the model honouring prose."""
    if addressing.self_named:
        return (
            "\n\nNote: you appear to be invited by name to weigh in — if you "
            "have anything useful, lean toward speaking."
        )
    if addressing.other_named:
        return (
            "\n\nNote: someone else appears to be invited by name — defer "
            "unless you have something genuinely new they would miss."
        )
    return ""


def _build_bid_messages(
    *,
    content: str,
    transcript: list[dict[str, Any]],
    addressing: NLAddressing,
) -> list[dict[str, Any]]:
    """Compact the in-round transcript + the inbound message into the bid
    prompt. The transcript is the RFC 0034 group working memory the caller
    already reconstructed for the turn — reused so the bid pays no extra
    history round-trip."""
    lines: list[str] = []
    for msg in transcript:
        role = msg.get("role", "")
        text = str(msg.get("content", "")).strip()
        if not text:
            continue
        speaker = "Me" if role == "assistant" else "Thread"
        lines.append(f"{speaker}: {text}")
    transcript_block = "\n".join(lines) if lines else "(no prior turns this round)"
    user = (
        "Conversation so far this round:\n"
        f"{transcript_block}\n\n"
        f"New message:\n{content}\n\n"
        "Decide whether you have something genuinely new and relevant to add "
        "that has not already been said. Bias toward staying silent."
        f"{_addressing_note(addressing)}\n\n"
        "Answer on exactly two lines:\n"
        "speak: yes|no\n"
        "score: <a number from 0.0 to 1.0 for how much you have to add>"
    )
    return [{"role": "user", "content": user}]


def _bar_for(threshold: float | None, addressing: NLAddressing) -> float:
    """The effective score bar for this bid (TB2 + PR 3).

    Starts from the configured ``threshold`` (or :data:`_DECISIVE_SCORE` when
    unset — bias-to-silence), then applies the NL-addressing shift: invited-by-
    name lowers the bar, someone-else-invited raises it. ``self`` wins when
    both fire.

    The someone-else-invited *penalty* is a **bias, never a hard filter** (TB4
    / amendment OQ #2): it is capped so a decisive contribution still clears
    even when someone else was invited. Without the cap, the unset-threshold
    path (base bar :data:`_DECISIVE_SCORE` = 0.8) plus the 0.2 penalty would
    clamp the bar to 1.0 — a de-facto hard drop where only a literal perfect
    score speaks. The ceiling is the decisive score, or the operator's own bar
    when they deliberately set one higher (we never lift a turn further out of
    reach than the configured threshold already places it).

    Rounded to :data:`_BAR_PRECISION` so the shift stays an *inclusive* floor:
    a float-naive ``0.4 + 0.2`` lands at 0.6000000000000001 and would silence a
    score of exactly 0.6 by an epsilon. Clamped to ``[0, 1]`` so the shift can
    never invert the gate."""
    base = _DECISIVE_SCORE if threshold is None else threshold
    bar = base
    if addressing.self_named:
        bar -= _ADDRESSED_SELF_BONUS
    elif addressing.other_named:
        bar = min(bar + _ADDRESSED_OTHER_PENALTY, max(_DECISIVE_SCORE, base))
    return round(max(0.0, min(1.0, bar)), _BAR_PRECISION)


def _bid_system_prompt(*, persona_name: str, persona_role: str) -> str:
    return (
        f"You are {persona_name} ({persona_role}) in a group chat. You are "
        "deciding ONLY whether to speak — not what to say. Prefer silence "
        "unless you would add something the thread does not already have. "
        "Reply with the two-line speak/score form and nothing else."
    )


def _parse_score(text: str | None) -> float | None:
    """Extract the salience score from the bid output, or ``None`` if absent."""
    if not text:
        return None
    match = _SCORE_RE.search(text)
    if match is None:
        return None
    try:
        score = float(match.group("score"))
    except ValueError:  # pragma: no cover - regex guarantees a float
        return None
    # Clamp defensively; the grammar already bounds 0-1 but a model can drift.
    return max(0.0, min(1.0, score))


def _parse_speak(text: str | None) -> bool | None:
    """Extract the ``speak: yes|no`` verdict, or ``None`` when the line is
    absent (the score then governs alone)."""
    if not text:
        return None
    match = _SPEAK_RE.search(text)
    if match is None:
        return None
    return match.group("speak").lower() == "yes"


async def evaluate_salience(
    *,
    llm_client: LLMClient,
    content: str,
    transcript: list[dict[str, Any]],
    agent_id: str,
    persona_name: str,
    persona_role: str,
    threshold: float | None,
    cause: walletpb.Cause.ValueType = walletpb.CAUSE_CHANNEL_MESSAGE,
) -> SalienceDecision:
    """Run the Tier B salience bid for one open-floor admit.

    Returns a :class:`SalienceDecision`. Every non-``salient`` path resolves
    to ``speak=False`` (bias-to-silence, TB2). The call is leased on the
    ``fast`` alias (TB3); a lease denial, the wallet active-lease cap
    (``RESOURCE_EXHAUSTED``), or any provider error all fail closed.

    ``cause`` is the RFC 0023 wallet cause the lease is billed under. The
    action-loop seam derives it from the inbound event
    (:func:`agents.persona_runtime.wallet_cause.cause_for_event`) so the bid
    bills the *same* cause as the quality turn for that event (e.g.
    ``CAUSE_CHAT`` for a chat-shaped message); it defaults to
    ``CAUSE_CHANNEL_MESSAGE`` for the common channel-message path and for
    direct callers (TB3).
    """
    # Resolve the `fast` alias per-call (RFC 0033). An unconfigured/unknown
    # alias raises SystemExit (a BaseException) by design — swallow it here
    # and degrade to silence rather than crash the hot path (the same
    # discipline summarize_close uses for the `summarizer` alias).
    try:
        resolved = resolve_model(_BID_MODEL_ALIAS)
    except SystemExit as exc:
        logger.warning(
            "Tier B salience bid: model alias %r unresolvable for agent %s: "
            "%s; staying silent",
            _BID_MODEL_ALIAS, agent_id, exc,
        )
        return SalienceDecision(speak=False, score=None, reason="model_unresolvable")

    # PR 3 — free-text addressing signal. Computed before the call so it both
    # nudges the prompt and shifts the deterministic score bar below.
    addressing = detect_nl_addressing(content=content, persona_name=persona_name)

    try:
        response = await llm_client.create_message(
            model=resolved.model,
            model_alias=resolved.alias,
            messages=_build_bid_messages(
                content=content, transcript=transcript, addressing=addressing,
            ),
            system=_bid_system_prompt(
                persona_name=persona_name, persona_role=persona_role,
            ),
            tools=[],
            max_tokens=_BID_MAX_OUTPUT_TOKENS,
            temperature=_BID_TEMPERATURE,
            cause=cause,
            agent_id=agent_id,
        )
    except BudgetExceededError:
        # TB3 / RFC 0023 §F: a denied (or unreachable) lease fails closed.
        logger.debug(
            "Tier B salience bid: lease denied for agent %s; staying silent",
            agent_id,
        )
        return SalienceDecision(speak=False, score=None, reason="lease_denied")
    except grpc.aio.AioRpcError as exc:
        # The wallet's per-agent active-lease cap is re-raised *unwrapped* by
        # ``WalletClient._acquire`` after its retry budget (ISSUE-0066) — it
        # never becomes a ``BudgetExceededError``. Like
        # :func:`agents.persona_runtime.llm_call_errors.handle_llm_call_exception`,
        # treat ``RESOURCE_EXHAUSTED`` as wallet back-pressure (a lease that
        # could not be acquired) and label it ``lease_denied``; any other
        # gRPC code is a real provider/server problem and degrades to
        # ``llm_error`` so the two signals stay distinct. Both fail closed.
        if exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
            logger.debug(
                "Tier B salience bid: lease back-pressure (RESOURCE_EXHAUSTED) "
                "for agent %s; staying silent",
                agent_id,
            )
            return SalienceDecision(speak=False, score=None, reason="lease_denied")
        logger.warning(
            "Tier B salience bid: gRPC error (%s) for agent %s; staying silent",
            exc.code(), agent_id,
        )
        return SalienceDecision(speak=False, score=None, reason="llm_error")
    except Exception as exc:  # noqa: BLE001 - any provider error → silence
        logger.warning(
            "Tier B salience bid: provider error for agent %s: %s; staying silent",
            agent_id, exc,
        )
        return SalienceDecision(speak=False, score=None, reason="llm_error")

    score = _parse_score(response.text)
    if score is None:
        logger.debug(
            "Tier B salience bid: unparseable output for agent %s; staying silent",
            agent_id,
        )
        return SalienceDecision(speak=False, score=None, reason="parse_failure")

    # TB2 — bias-to-silence. An unset threshold demands a decisive score; an
    # explicit threshold is an inclusive floor. PR 3 then shifts the bar for
    # NL addressing (invited-by-name lowers it; someone-else-invited raises it)
    # — a bias, never a hard drop, so a decisive score still clears.
    bar = _bar_for(threshold, addressing)
    if score < bar:
        return SalienceDecision(speak=False, score=score, reason="below_threshold")

    # The score would speak — but ``speak: no`` is a one-way veto toward
    # silence (TB2): honour an explicit decline even when the score clears the
    # bar. (A below-bar score is already handled above, so the veto only ever
    # *removes* a would-be turn; it can never add one.)
    if _parse_speak(response.text) is False:
        logger.debug(
            "Tier B salience bid: explicit speak=no veto for agent %s "
            "(score=%s cleared bar=%s); staying silent",
            agent_id, score, bar,
        )
        return SalienceDecision(speak=False, score=score, reason="declined")

    return SalienceDecision(speak=True, score=score, reason="salient")
