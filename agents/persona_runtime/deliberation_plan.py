"""RFC 0051 Phase 2 (v0.3.10) — the private ``CompositionPlan``.

PR 1–2 made a persona privately decide *whether* to post (the structured
silence verdict in :mod:`agents.salience_deliberation`). Phase 2 adds *what* the
post should accomplish: when ``should_post`` is true under ``reasoning.mode:
plan`` the bid additionally emits a small plan — intent, the substance to land,
whom it addresses, what not to restate — which is threaded into the Tier-C
compose as a **private** system-prompt section
([RFC 0051 §C](../../docs/rfcs/0051-reasoning-before-posting.md)).

This module owns that artifact in isolation: the :class:`CompositionPlan` value
type, a regex-tolerant :func:`parse_plan`, and a pure :func:`render_plan_section`
renderer. It is a **separate type and a separate module** from the gate verdict
on purpose ([RFC 0051 §C "why two types"](../../docs/rfcs/0051-reasoning-before-posting.md)):

* The plan is irrelevant to the *silence* decision — it is transported *through*
  the gate to compose, never read by it. Folding it onto
  :class:`agents.salience_bid.SalienceDecision` would couple the gate's return
  type to compose-stage data, so the plan rides back on the seam's
  ``SalienceOutcome`` instead and ``SalienceDecision`` stays un-widened.
* Keeping it agent-/``action_loop``-free makes it unit-testable on its own, and
  makes it the single point the no-leak test
  (``tests/integration/test_deliberation_no_leak.py``) points at to pin the
  §E privacy wall — the plan is a distinct value type, never an ``AgentAction``,
  so "this is private" is a *structural* property, not a convention.

**Fail-closed to "no plan", not to silence.** :func:`parse_plan` returns
``None`` for an unparseable response, and the caller composes *unplanned* rather
than blocking the post — the bias is the **opposite** of the gate's
bias-to-silence: by the time a plan is parsed the gate has already decided the
persona *should* post (RFC 0051 §Phase 2).

**Dark until Phase 3.** The plan path is reached only under ``mode: plan``,
which the action-loop seam does not pass until PR 4 wires the ``reasoning``
config knob and PR 6 flips the governed default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "CompositionPlan",
    "parse_plan",
    "render_plan_section",
]

# ≤3 — the substance to land, not an essay smuggled into the compose prompt
# ([RFC 0051 §C](../../docs/rfcs/0051-reasoning-before-posting.md)). A model that
# lists more is trimmed to the first three rather than rejected.
_MAX_KEY_POINTS: Final[int] = 3

# The default audience when the plan names none — an open-floor post addresses
# the whole channel, matching the gate's open-floor admit ([RFC 0051 §B/§C]).
_DEFAULT_ADDRESSED_TO: Final[str] = "channel"

# List fields are a single line of ``;``-separated clauses. ``;`` is the one
# delimiter the plan prompt asks for; splitting on it (and dropping blanks) keeps
# parsing forgiving of a trailing ``;`` or a doubled ``;;``.
_LIST_DELIMITER: Final[str] = ";"


@dataclass(frozen=True, slots=True)
class CompositionPlan:
    """The private per-turn plan a persona composes under (RFC 0051 §C).

    Frozen because it is *transported* — produced by the deliberation, carried on
    the seam's ``SalienceOutcome``, and rendered into the compose prompt — never
    mutated en route, and never an ``AgentAction`` (the §E privacy wall).

    Attributes:
        intent: What this contribution is *for* — the one-clause anchor. A plan
            with no parseable intent is not produced (:func:`parse_plan` returns
            ``None``).
        key_points: ≤3 points, the substance to land. May be empty (intent alone
            is a valid plan).
        addressed_to: A participant id, or ``"channel"`` for the whole room (the
            default when the plan names no one).
        avoid_restating: What peers already said that this post should not repeat.
            May be empty.
    """

    intent: str
    key_points: tuple[str, ...]
    addressed_to: str
    avoid_restating: tuple[str, ...]


# Grammar. Mirrors the verdict parser's tolerance (:mod:`agents.salience_deliberation`):
# forgiving of surrounding prose (the should_post/reason_code lines ride the same
# response), ``[:=]``-tolerant, case-insensitive, and **single-line**. The
# whitespace around the separator is horizontal-only (``[^\S\r\n]`` = space/tab,
# not newline) so a present-but-blank ``intent:`` line cannot let the value
# regex cross the newline and capture the *next* field's line; ``\S.*`` then
# anchors on a non-space char so a blank value is no value at all.
def _field_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"{name}[^\S\r\n]*[:=][^\S\r\n]*(?P<v>\S.*)", re.IGNORECASE)


_INTENT_RE: Final[re.Pattern[str]] = _field_re("intent")
_KEY_POINTS_RE: Final[re.Pattern[str]] = _field_re("key_points")
_ADDRESSED_TO_RE: Final[re.Pattern[str]] = _field_re("addressed_to")
_AVOID_RESTATING_RE: Final[re.Pattern[str]] = _field_re("avoid_restating")


def _scalar(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    value = match.group("v").strip()
    return value or None


def _list(pattern: re.Pattern[str], text: str, *, cap: int | None = None) -> tuple[str, ...]:
    raw = _scalar(pattern, text)
    if raw is None:
        return ()
    items = [part.strip() for part in raw.split(_LIST_DELIMITER)]
    items = [part for part in items if part]
    if cap is not None:
        items = items[:cap]
    return tuple(items)


def parse_plan(text: str | None) -> CompositionPlan | None:
    """Parse a ``CompositionPlan`` from the bid's ``mode: plan`` response.

    Returns ``None`` (fail-closed to "no plan") when there is no parseable
    ``intent`` — the load-bearing anchor — so the caller composes *unplanned*
    rather than blocking the post. The other fields are best-effort around the
    intent: ``key_points`` is capped at :data:`_MAX_KEY_POINTS`, ``addressed_to``
    defaults to :data:`_DEFAULT_ADDRESSED_TO`, and ``avoid_restating`` may be
    empty. Tolerant of the should_post/reason_code verdict lines that ride the
    same response.
    """
    if not text:
        return None
    intent = _scalar(_INTENT_RE, text)
    if intent is None:
        return None
    return CompositionPlan(
        intent=intent,
        key_points=_list(_KEY_POINTS_RE, text, cap=_MAX_KEY_POINTS),
        addressed_to=_scalar(_ADDRESSED_TO_RE, text) or _DEFAULT_ADDRESSED_TO,
        avoid_restating=_list(_AVOID_RESTATING_RE, text),
    )


# The private section wrapper. The opening line states the §E contract in-prompt
# (the persona must not reveal the plan); the structural half of the wall — the
# plan is never an ``AgentAction`` and never persisted — is what the no-leak test
# pins. Optional list fields render only when non-empty so an intent-only plan
# never emits a dangling "Key points:" that could read as "say nothing".
_SECTION_OPEN: Final[str] = "<deliberation_plan>"
_SECTION_CLOSE: Final[str] = "</deliberation_plan>"
_SECTION_PREAMBLE: Final[str] = (
    "This is your PRIVATE plan for the message you are about to post. It is "
    "yours alone — never reveal, quote, or mention it; no other participant can "
    "see it. Use it to shape the post, then write the post itself."
)


def render_plan_section(plan: CompositionPlan) -> str:
    """Render ``plan`` as a private system-prompt section for the Tier-C compose.

    Pure — no agent/``action_loop`` coupling; the action loop appends the result
    alongside the RFC 0034 working-memory sections via a one-line call. The text
    is the persona's *own* trusted reasoning, so it is a normal system-prompt
    section, **not** the RFC 0009 ``<external_data>`` quarantine envelope (which
    is for untrusted tool/bridge output) (RFC 0051 §E).
    """
    lines = [_SECTION_OPEN, _SECTION_PREAMBLE, f"Intent: {plan.intent}"]
    if plan.key_points:
        lines.append("Key points to land: " + "; ".join(plan.key_points))
    lines.append(f"Addressed to: {plan.addressed_to}")
    if plan.avoid_restating:
        lines.append("Already said — do not restate: " + "; ".join(plan.avoid_restating))
    lines.append(_SECTION_CLOSE)
    return "\n".join(lines)
