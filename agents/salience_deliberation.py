"""RFC 0051 Phase 1a (v0.3.10) — the structured silence verdict grammar.

The leased Tier-B salience bid (:mod:`agents.salience_bid`) historically emits a
scalar ``speak:``/``score:`` verdict gated against a per-member ``threshold``
(RFC 0030 Tier B, v0.3.8). RFC 0051 generalizes it: before publishing, a
persona privately decides *whether* a turn is worth a post and (PR 3) *what* the
post should add. This module owns the **structured verdict** half of that — the
``{ should_post, reason_code, reason_note }`` grammar the bid emits under
``reasoning.mode: bid|plan`` ([RFC 0051 §C](../docs/rfcs/0051-reasoning-before-posting.md)).

It is split out of ``salience_bid.py`` for the same file-size-cap reason the
addressing soft-bias lives in ``salience_addressing.py``: the structured grammar
adds parser branches, a reason-code vocabulary, and the parse-failure safety-net
wiring that would push the bid core past the 500-line review cap. The split also
keeps the value type (:class:`agents.salience_bid.SalienceDecision`) in one
place: this module returns a **plain tuple**, never a ``SalienceDecision``, so
there is no import cycle back into ``salience_bid``.

**Supersedes the score gate, not a pure add** (RFC 0051 OQ 7). Under reasoning
the bid emits **no** ``score``: ``should_post`` *is* the silence decision, and
the per-member ``threshold`` is inert (it governs ``mode: off`` only). That is
the whole point — a numeric threshold can neither articulate "I'd only be
agreeing" nor rescue a low-scored turn that would add real substance.

**Dark until Phase 3.** The structured path is reachable only when a caller
passes ``mode="bid"``/``"plan"`` to :func:`agents.salience_bid.evaluate_salience`;
the action-loop seam does not, so deploying Phases 1–2 is behaviourally inert in
production until [PR 4](../docs/rfcs/0051-pr-plan.md) wires the ``reasoning``
config knob and [PR 6](../docs/rfcs/0051-pr-plan.md) flips the governed default.
"""

from __future__ import annotations

import logging
import re
from typing import Final

from .observability._metrics_salience import deliberation_parse_failure_attrs
from .observability.metrics import try_get_instruments

logger = logging.getLogger(__name__)

__all__ = [
    "MODE_BID",
    "MODE_OFF",
    "MODE_PLAN",
    "REASON_ADDS_SUBSTANCE",
    "REASON_ALREADY_ANSWERED",
    "REASON_NOTHING_TO_ADD",
    "REASON_ONLY_AGREEING",
    "REASON_PARSE_FAILURE",
    "is_known_mode",
    "is_structured",
    "max_output_tokens_for",
    "parse_verdict",
    "system_snippet",
    "user_snippet",
    "warn_if_unknown_mode",
]

# ``reasoning.mode`` values (RFC 0051 §G). ``off`` is byte-for-byte the scalar
# Tier-B bid; ``bid`` adds the structured silence verdict; ``plan`` adds the
# plan-threaded compose (PR 3). ``bid`` and ``plan`` share this verdict grammar
# — they differ only in the compose stage downstream, so both are "structured".
MODE_OFF: Final[str] = "off"
MODE_BID: Final[str] = "bid"
MODE_PLAN: Final[str] = "plan"
_STRUCTURED_MODES: Final[frozenset[str]] = frozenset({MODE_BID, MODE_PLAN})
_KNOWN_MODES: Final[frozenset[str]] = frozenset({MODE_OFF, MODE_BID, MODE_PLAN})

# The runtime-prompt snippets the bid loads, keyed by mode. Named here, next to
# the grammar regexes that parse what they produce, so the prompt form and its
# parser cannot silently drift apart. ``off`` keeps the scalar speak/score form.
_SCALAR_SYSTEM_SNIPPET: Final[str] = "salience-bid-system"
_SCALAR_USER_SNIPPET: Final[str] = "salience-bid-user"
REASONING_SYSTEM_SNIPPET: Final[str] = "salience-bid-reasoning-system"
REASONING_USER_SNIPPET: Final[str] = "salience-bid-reasoning-user"


def user_snippet(mode: str) -> str:
    """The user-prompt snippet name for ``mode`` (structured vs. scalar)."""
    return REASONING_USER_SNIPPET if is_structured(mode) else _SCALAR_USER_SNIPPET


def system_snippet(mode: str) -> str:
    """The system-prompt snippet name for ``mode`` (structured vs. scalar)."""
    return REASONING_SYSTEM_SNIPPET if is_structured(mode) else _SCALAR_SYSTEM_SNIPPET


# ── Output-token budget, scaled by mode ──────────────────────────────────────
#
# The scalar bid is a yes/no + score and fits in 64 tokens; if a verbose model
# truncates before emitting the verdict the parse fails *closed* to silence, so
# a tight budget is safe. The structured verdict is larger (three labelled
# lines), and ``plan`` (PR 3) additionally carries a ``CompositionPlan`` — so
# the cap scales up: a modest bump for ``bid``, a larger ceiling for ``plan``.
# Truncation still fails closed, but a too-tight cap would manufacture parse
# failures, so the cap must comfortably fit each rung's output. NOTE: until PR 3
# wires the ``CompositionPlan``, ``plan`` shares the ``bid`` prompt+parser, so
# its larger cap is forward *headroom* — ``max_tokens`` is a ceiling, not a
# floor, and today's bid-shaped output stops well short of it (no extra cost).
_SCALAR_MAX_OUTPUT_TOKENS: Final[int] = 64
_BID_MAX_OUTPUT_TOKENS: Final[int] = 128
_PLAN_MAX_OUTPUT_TOKENS: Final[int] = 320


def max_output_tokens_for(mode: str) -> int:
    """The bid's ``max_tokens`` for ``mode`` (RFC 0051 §C/§F).

    ``off`` keeps the scalar 64 (byte-for-byte the existing wire call); ``bid``
    and ``plan`` scale up to fit the structured verdict (and, under ``plan``,
    the eventual ``CompositionPlan``). An unknown mode is treated as ``off``."""
    if mode == MODE_PLAN:
        return _PLAN_MAX_OUTPUT_TOKENS
    if mode == MODE_BID:
        return _BID_MAX_OUTPUT_TOKENS
    return _SCALAR_MAX_OUTPUT_TOKENS


def is_structured(mode: str) -> bool:
    """``True`` for the reasoning rungs (``bid``/``plan``); ``False`` for ``off``
    and any unknown value (which falls back to the scalar gate, fail-safe)."""
    return mode in _STRUCTURED_MODES


def is_known_mode(mode: str) -> bool:
    """``True`` for a recognised ``reasoning.mode`` (``off``/``bid``/``plan``).

    An unknown value still resolves to the scalar gate fail-safe (via
    :func:`is_structured` / :func:`max_output_tokens_for`); this predicate exists
    so the caller can *notice* the typo — see :func:`warn_if_unknown_mode`."""
    return mode in _KNOWN_MODES


def warn_if_unknown_mode(mode: str, *, agent_id: str) -> None:
    """Log once when an unrecognised ``mode`` reaches the bid.

    The bid then degrades to the scalar score gate (``is_structured`` is
    ``False`` for any unknown value), so the fallback is *fail-safe* — but
    without this a typo'd ``reasoning.mode`` would silently disable the
    structured verdict with no signal until the Phase-3 config ``validate``
    (PR 4) rejects an unbacked value outright.

    The caller invokes this *before* resolving the model alias: the unknown-mode
    signal is independent of resolution success, so a separately-unresolvable
    model must not swallow the diagnostic by returning first."""
    if mode not in _KNOWN_MODES:
        logger.warning(
            "Tier B salience bid: unrecognised reasoning mode %r for agent %s; "
            "using the scalar score gate", mode, agent_id,
        )


# ── Reason-code vocabulary ───────────────────────────────────────────────────
#
# ``reason_code`` *is* the existing ``SalienceDecision.reason`` low-cardinality
# label, value-set extended with the semantic cases and pruned of the score-only
# ``below_threshold`` ([RFC 0051 §C](../docs/rfcs/0051-reasoning-before-posting.md)).
# It is what the metric and (PR 2) the ``agent.deliberated`` audit both read, so
# it MUST stay a small closed set — an off-enum value from the model collapses
# to the mode-appropriate default rather than reaching the metric verbatim and
# blowing up its cardinality. The free-text justification lives in ``reason_note``
# (debug-egress only), never here.
REASON_ADDS_SUBSTANCE: Final[str] = "adds_substance"  # the sole speak-side code
REASON_ALREADY_ANSWERED: Final[str] = "already_answered"
REASON_ONLY_AGREEING: Final[str] = "only_agreeing"
REASON_NOTHING_TO_ADD: Final[str] = "nothing_to_add"
# Reused from the scalar bid's fail-closed labels — a structured parse error
# resolves to silence under the SAME ``parse_failure`` label the seam already
# classifies, so its existing handling is unchanged.
REASON_PARSE_FAILURE: Final[str] = "parse_failure"

_SILENCE_CODES: Final[frozenset[str]] = frozenset(
    {REASON_ALREADY_ANSWERED, REASON_ONLY_AGREEING, REASON_NOTHING_TO_ADD},
)

# Grammar. Forgiving of surrounding prose (the model may editorialize) but a
# missing ``should_post:`` is a parse failure → silence (fail-closed). The
# trailing ``\b`` mirrors the scalar ``_SCORE_RE``'s ``(?!\d)`` guard — it keeps
# closed the *one* direction the parser could fail *toward* speech: a token that
# merely starts with ``yes`` (``should_post: yesterday``) must not partial-match
# into a speak verdict; it falls through to ``parse_failure`` → silence instead.
# (A ``no``-prefixed token like ``nope`` likewise stops matching, but its
# direction is already silence, so the guard only ever *removes* a false speak.)
# The ``reason_note`` capture is single-line and bounded so a runaway clause
# cannot bloat the debug log.
_SHOULD_POST_RE: Final[re.Pattern[str]] = re.compile(
    r"should_post\s*[:=]\s*(?P<v>yes|no)\b", re.IGNORECASE,
)
_REASON_CODE_RE: Final[re.Pattern[str]] = re.compile(
    r"reason_code\s*[:=]\s*(?P<v>[a-z][a-z_]*)", re.IGNORECASE,
)
_REASON_NOTE_RE: Final[re.Pattern[str]] = re.compile(
    r"reason_note\s*[:=]\s*(?P<v>\S.*)", re.IGNORECASE,
)
_REASON_NOTE_MAX_CHARS: Final[int] = 240


def _parse_should_post(text: str) -> bool | None:
    match = _SHOULD_POST_RE.search(text)
    if match is None:
        return None
    return match.group("v").lower() == "yes"


def _normalize_reason_code(text: str, *, should_post: bool) -> str:
    """Map the model's ``reason_code`` onto the closed vocabulary. A speak
    verdict defaults to ``adds_substance`` (the sole speak-side code); a silence
    verdict to ``nothing_to_add``. An off-enum value collapses to that default
    rather than reaching the metric verbatim."""
    match = _REASON_CODE_RE.search(text)
    raw = match.group("v").lower() if match else ""
    if should_post:
        return REASON_ADDS_SUBSTANCE
    return raw if raw in _SILENCE_CODES else REASON_NOTHING_TO_ADD


def _parse_reason_note(text: str) -> str | None:
    match = _REASON_NOTE_RE.search(text)
    if match is None:
        return None
    note = match.group("v").strip()
    if not note:
        return None
    # A model with nothing to justify may echo the user snippet's literal
    # ``<one short clause on why — optional>`` placeholder verbatim. That is the
    # snippet's only ``<…>`` field, so an angle-bracket-wrapped capture is the
    # placeholder, not a justification — drop it to ``None`` rather than leak
    # template noise into the operator-debug egress (RFC 0051 §E).
    if note.startswith("<") and note.endswith(">"):
        return None
    return note[:_REASON_NOTE_MAX_CHARS]


def parse_verdict(text: str | None, *, mode: str) -> tuple[bool, str, str | None]:
    """Parse a structured deliberation response into ``(should_post, reason_code,
    reason_note)``.

    Fail-closed to silence: a missing/unparseable ``should_post:`` returns
    ``(False, "parse_failure", None)`` **and** increments the first-class
    ``deliberation.parse_failures`` counter — the mandatory, never-gated safety
    net that makes a silent parser break alertable, distinct from genuine
    no-pile-on dampening on ``channel.messages.gated``. The two are
    **additive, not a re-route**: once the seam is wired (PR 2) a reasoning
    parse failure still rides ``channel.messages.gated{reason=parse_failure}``
    too — this counter adds a *second*, never-gated signal, so the two must not
    be summed as if disjoint. ``mode`` is carried only as the counter's
    low-cardinality attribute (the caller has already chosen the structured
    path)."""
    should_post = _parse_should_post(text or "")
    if should_post is None:
        inst = try_get_instruments()
        if inst is not None:
            inst.deliberation_parse_failures.add(
                1, attributes=deliberation_parse_failure_attrs(mode=mode),
            )
        return (False, REASON_PARSE_FAILURE, None)
    reason_code = _normalize_reason_code(text or "", should_post=should_post)
    reason_note = _parse_reason_note(text or "")
    return (should_post, reason_code, reason_note)
