"""RFC 0044 Phase 1 — the closed assertion-vocabulary engine.

This module is the deterministic core of the golden-trace eval harness: the
matchers that turn an observed run (:class:`EvalRun`) into per-assertion
pass/fail results. It is intentionally *pure* — it imports nothing from the
eval-set loader, the persona runtime, or any LLM client — so the grammar can be
unit-tested in isolation and reused by the replay runner (a later PR) without a
dependency cycle.

The vocabulary is **closed** (RFC 0044 §B): adding a new match operator is an
RFC amendment, not a routine change. Events are treated as opaque
``{"type": ...}`` mappings so this engine does not depend on the (not-yet-landed)
RFC 0041 typed-event taxonomy — it asserts over whatever event-type strings a
run produces.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MatchOp(Enum):
    """The closed set of assertion operators (RFC 0044 §B).

    Members are constructed from their string value (``MatchOp("contains")``),
    which is how the loader maps a recipe's ``match:`` key onto an operator.
    """

    # Content operators — applied to transcript / assistant text.
    EXACT = "exact"
    CONTAINS = "contains"
    MUST_REFERENCE = "must_reference"
    MUST_NOT_REFERENCE = "must_not_reference"
    REGEX = "regex"
    # Numeric operators — applied to (terminal) state values.
    GT = "gt"
    LT = "lt"
    GTE = "gte"
    LTE = "lte"
    EQ = "eq"


#: Operators that read stochastic text. ``EXACT`` is a member syntactically, but
#: RFC 0044 §D forbids it on ``assistant:`` / transcript content — the loader
#: enforces that; only state values may use ``exact``.
CONTENT_OPS: frozenset[MatchOp] = frozenset(
    {
        MatchOp.EXACT,
        MatchOp.CONTAINS,
        MatchOp.MUST_REFERENCE,
        MatchOp.MUST_NOT_REFERENCE,
        MatchOp.REGEX,
    }
)
#: Operators that compare numbers (or exact-equality) against a state value.
NUMERIC_OPS: frozenset[MatchOp] = frozenset(
    {MatchOp.GT, MatchOp.LT, MatchOp.GTE, MatchOp.LTE, MatchOp.EQ}
)


@dataclass(frozen=True)
class AssertionResult:
    """One assertion's outcome. ``name`` is a stable dotted id for reporting."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class EvalRun:
    """The observed outcome of running an eval-set recipe.

    Produced by the replay runner (a later PR) and consumed by
    :func:`evaluators.eval_set.evaluate`. Kept deliberately small: the ordered
    assistant outputs, the terminal state map, and the flat event stream.

    ``shadow_traces`` (RFC 0049 PR 2) carries the per-turn L2 cross-room
    shadow records the driver captured off the runtime's shadow log —
    opaque dicts here so the pure assertion core stays runtime-free. No
    assertion vocabulary consumes them; the runner threads them into the
    report artifact for the PR 4 shadow→live measurement gate.
    """

    turn_outputs: list[str] = field(default_factory=list)
    terminal_state: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    shadow_traces: list[dict[str, Any]] = field(default_factory=list)

    @property
    def transcript(self) -> str:
        """The final transcript = assistant outputs joined newline-wise.

        Assertions in ``final_transcript`` assert on what the *persona* said,
        not on the seeded user prompts, so only assistant turns contribute.
        """
        return "\n".join(self.turn_outputs)


# ─── Content matcher ────────────────────────────────────────────────────────


def match_content(
    op: MatchOp,
    text: str,
    *,
    value: str | None = None,
    values: list[str] | None = None,
) -> tuple[bool, str]:
    """Evaluate a content operator against ``text``.

    Returns ``(passed, detail)`` where ``detail`` is empty on success and names
    the offending needle(s) on failure. Never raises on ordinary input — a
    caller-side mistake (wrong operator for the payload) surfaces as a
    descriptive failure, not an exception.
    """
    if op is MatchOp.CONTAINS:
        needle = value or ""
        ok = needle in text
        return ok, "" if ok else f"missing substring: {needle!r}"

    if op is MatchOp.MUST_REFERENCE:
        missing = [v for v in (values or []) if v not in text]
        return (not missing), "" if not missing else f"missing references: {missing}"

    if op is MatchOp.MUST_NOT_REFERENCE:
        present = [v for v in (values or []) if v in text]
        return (not present), "" if not present else f"forbidden references present: {present}"

    if op is MatchOp.REGEX:
        pattern = value or ""
        try:
            ok = re.search(pattern, text) is not None
        except re.error as exc:
            # The loader compiles regex operands at load time, so a bad pattern
            # is normally caught earlier — but honour the "never raises" contract
            # for direct callers too.
            return False, f"invalid regex {pattern!r}: {exc}"
        return ok, "" if ok else f"regex did not match: {pattern!r}"

    if op is MatchOp.EXACT:
        ok = text == value
        return ok, "" if ok else f"expected exactly {value!r}, got {text!r}"

    return False, f"{op.value!r} is not a content operator"


# ─── Numeric matcher ────────────────────────────────────────────────────────


def match_numeric(op: MatchOp, actual: Any, expected: Any) -> tuple[bool, str]:
    """Evaluate a numeric operator. All operators coerce to ``float`` where they
    can and fail gracefully on a missing / non-numeric operand — an ``actual``
    that is a state key never written, or an ``expected`` the recipe author
    fat-fingered (``gt: high``)."""
    # ``bool`` is a subclass of ``int``, so an unguarded numeric op would let a
    # boolean spuriously satisfy a count/threshold (``True == 1``,
    # ``float(True) > 0.5``). A boolean is not a number here — fail it on either
    # side (use ``match: exact`` for boolean state values). Guarding ``expected``
    # too keeps the rule symmetric: ``eq: true`` must not match a numeric state
    # value of ``1`` just because ``float(True) == 1.0``.
    if isinstance(actual, bool):
        return False, f"value not numeric (boolean): got {actual!r}"
    if isinstance(expected, bool):
        return False, (
            f"operand not numeric (boolean): expected {expected!r} — use `exact` for booleans"
        )

    if op is MatchOp.EQ:
        # Numeric equality with the same float coercion the ordering operators
        # use, so a state value serialized as ``"2"`` still satisfies ``eq: 2``
        # (the ordering ops already coerce — ``eq`` was the odd one out). Fall
        # back to plain equality when either operand is non-numeric, so ``eq``
        # still works on the occasional string state value where it reads more
        # naturally than ``exact``.
        try:
            ok = float(actual) == float(expected)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            ok = actual == expected
        return ok, "" if ok else f"expected == {expected!r}, got {actual!r}"

    try:
        a = float(actual)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, f"value not numeric: got {actual!r}"
    try:
        e = float(expected)
    except (TypeError, ValueError):
        # Name the *operand* (not the observed value) so a fat-fingered recipe
        # threshold — ``gt: high`` — points the author at their own mistake.
        return False, f"operand not numeric: expected {expected!r}"

    if op is MatchOp.GT:
        ok = a > e
    elif op is MatchOp.LT:
        ok = a < e
    elif op is MatchOp.GTE:
        ok = a >= e
    elif op is MatchOp.LTE:
        ok = a <= e
    else:  # pragma: no cover - guarded by the caller / schema enum
        return False, f"{op.value!r} is not a numeric operator"
    return ok, "" if ok else f"expected {op.value} {expected!r}, got {actual!r}"


# ─── Exact (state) matcher ──────────────────────────────────────────────────


def match_exact(actual: Any, expected: Any) -> tuple[bool, str]:
    """Evaluate the ``exact`` state operator: strict equality with a bool↔number
    firewall.

    ``bool`` is an ``int`` subclass, so a bare ``==`` treats ``1 == True`` and
    ``0 == False`` as equal — which would let a state value silently change type
    (persona writes ``int 1`` where a recipe asserts ``exact: true``, or vice
    versa) without failing the eval. That is the same conflation
    :func:`match_numeric` guards against, and this is the operator that guard
    steers booleans toward, so ``exact`` must firewall it too: a boolean is never
    exactly-equal to a non-boolean. Every other pair uses ordinary equality.
    """
    if isinstance(actual, bool) != isinstance(expected, bool):
        ok = False
    else:
        ok = actual == expected
    return ok, "" if ok else f"expected {expected!r}, got {actual!r}"


# ─── Event matchers ─────────────────────────────────────────────────────────


def match_event_count(
    expected: dict[str, int], events: list[dict[str, Any]]
) -> list[AssertionResult]:
    """Assert an exact per-type event count over the flat event stream."""
    counts = Counter(e.get("type") for e in events)
    results: list[AssertionResult] = []
    for etype, want in expected.items():
        got = counts.get(etype, 0)
        results.append(
            AssertionResult(
                name=f"event_count.{etype}",
                passed=got == want,
                detail="" if got == want else f"expected {want}, got {got}",
            )
        )
    return results


def match_event_sequence(expected: list[str], events: list[dict[str, Any]]) -> AssertionResult:
    """Assert ``expected`` appears as a *contiguous* run of event types in the
    stream (RFC 0044 §B "event stream slice")."""
    stream = [e.get("type") for e in events]
    ok = _contains_contiguous(expected, stream)
    return AssertionResult(
        name="event_sequence",
        passed=ok,
        detail="" if ok else f"sequence {expected} not found (contiguous) in {stream}",
    )


def _contains_contiguous(sub: list[str], seq: list[Any]) -> bool:
    if not sub:
        return True
    n = len(sub)
    for i in range(len(seq) - n + 1):
        if seq[i : i + n] == sub:
            return True
    return False
