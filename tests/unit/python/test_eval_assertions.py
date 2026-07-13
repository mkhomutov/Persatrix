"""Unit tests for the RFC 0044 Phase 1 assertion-vocabulary engine.

These exercise the pure matchers in ``evaluators.assertions`` — the closed
assertion grammar from RFC 0044 §B — in isolation from the eval-set loader and
the (not-yet-built) replay runner. Every matcher is checked on both a passing
and a failing input, and the numeric matchers are checked on their boundary so
``gte``/``lte`` are distinguished from ``gt``/``lt``.
"""

from evaluators.assertions import (
    AssertionResult,
    EvalRun,
    MatchOp,
    match_content,
    match_event_count,
    match_event_sequence,
    match_numeric,
)

# ─── Content matchers (RFC 0044 §B, §D) ─────────────────────────────────────


def test_contains_hit_and_miss() -> None:
    assert match_content(MatchOp.CONTAINS, "hello Alice", value="Alice")[0] is True
    ok, detail = match_content(MatchOp.CONTAINS, "hello Bob", value="Alice")
    assert ok is False
    assert "Alice" in detail  # detail names the missing needle


def test_must_reference_requires_all_values() -> None:
    text = "Alice works on the data-platform team"
    assert match_content(
        MatchOp.MUST_REFERENCE, text, values=["Alice", "data-platform"]
    )[0] is True
    ok, detail = match_content(
        MatchOp.MUST_REFERENCE, text, values=["Alice", "Kubernetes"]
    )
    assert ok is False
    assert "Kubernetes" in detail  # only the missing one is reported
    assert "Alice" not in detail


def test_must_not_reference_flags_forbidden() -> None:
    assert match_content(
        MatchOp.MUST_NOT_REFERENCE, "a clean answer", values=["[error]", "I don't recall"]
    )[0] is True
    ok, detail = match_content(
        MatchOp.MUST_NOT_REFERENCE, "sorry, [error] happened", values=["[error]"]
    )
    assert ok is False
    assert "[error]" in detail


def test_regex_match_and_nomatch() -> None:
    assert match_content(
        MatchOp.REGEX, "I do not recall that", value=r"^I (don't|do not) recall"
    )[0] is True
    assert match_content(
        MatchOp.REGEX, "Sure, it was Tuesday", value=r"^I (don't|do not) recall"
    )[0] is False


def test_exact_is_equality() -> None:
    assert match_content(MatchOp.EXACT, "wallet_denied", value="wallet_denied")[0] is True
    assert match_content(MatchOp.EXACT, "wallet denied", value="wallet_denied")[0] is False


# ─── Numeric matchers (RFC 0044 §B) ─────────────────────────────────────────


def test_numeric_gt_lt_strict() -> None:
    assert match_numeric(MatchOp.GT, 0.5, 0.0)[0] is True
    assert match_numeric(MatchOp.GT, 0.0, 0.0)[0] is False  # strict
    assert match_numeric(MatchOp.LT, -1.0, 0.0)[0] is True
    assert match_numeric(MatchOp.LT, 0.0, 0.0)[0] is False  # strict


def test_numeric_gte_lte_boundary() -> None:
    assert match_numeric(MatchOp.GTE, 0.0, 0.0)[0] is True
    assert match_numeric(MatchOp.LTE, 0.0, 0.0)[0] is True
    assert match_numeric(MatchOp.GTE, -0.1, 0.0)[0] is False
    assert match_numeric(MatchOp.LTE, 0.1, 0.0)[0] is False


def test_numeric_eq() -> None:
    assert match_numeric(MatchOp.EQ, 2, 2)[0] is True
    assert match_numeric(MatchOp.EQ, 2, 3)[0] is False


def test_numeric_eq_coerces_like_the_ordering_ops() -> None:
    # `eq` shares the ordering ops' float coercion: a state value serialized as
    # a numeric string must satisfy `eq: 2` (parity with `gte`, which coerces).
    assert match_numeric(MatchOp.EQ, "2", 2)[0] is True
    assert match_numeric(MatchOp.EQ, "2.0", 2)[0] is True
    assert match_numeric(MatchOp.EQ, "3", 2)[0] is False
    # …but a non-numeric operand falls back to plain equality, so `eq` still
    # works on a string state value that reads more naturally than `exact`.
    assert match_numeric(MatchOp.EQ, "active", "active")[0] is True
    assert match_numeric(MatchOp.EQ, "active", "idle")[0] is False


def test_numeric_non_numeric_actual_fails_gracefully() -> None:
    ok, detail = match_numeric(MatchOp.GT, None, 0.0)
    assert ok is False
    assert detail  # explains the value was missing / non-numeric
    ok2, _ = match_numeric(MatchOp.GT, "not-a-number", 0.0)
    assert ok2 is False


def test_numeric_boolean_actual_is_not_numeric() -> None:
    # bool is an int subclass; a boolean state flag must not satisfy a numeric op
    # (True == 1, float(True) > 0.5). Use `exact` for boolean state instead.
    assert match_numeric(MatchOp.EQ, True, 1)[0] is False
    assert match_numeric(MatchOp.GT, True, 0.5)[0] is False
    ok, detail = match_numeric(MatchOp.GTE, False, 0)
    assert ok is False
    assert "bool" in detail.lower()


def test_regex_invalid_pattern_fails_without_raising() -> None:
    # match_content promises never to raise — a bad regex is a graceful failure.
    ok, detail = match_content(MatchOp.REGEX, "anything", value="([unclosed")
    assert ok is False
    assert "regex" in detail.lower()


# ─── Event matchers (RFC 0044 §B) ───────────────────────────────────────────


def test_event_count_exact_per_type() -> None:
    events = [{"type": "ModelOutput"}, {"type": "ToolCall"}, {"type": "ToolCall"}]
    results = match_event_count({"ToolCall": 2, "Error": 0}, events)
    assert {r.name: r.passed for r in results} == {
        "event_count.ToolCall": True,
        "event_count.Error": True,
    }


def test_event_count_mismatch_reports_actual() -> None:
    events = [{"type": "Error"}, {"type": "Error"}]
    (result,) = match_event_count({"Error": 0}, events)
    assert result.passed is False
    assert "2" in result.detail  # observed count surfaced


def test_event_sequence_contiguous_present() -> None:
    events = [
        {"type": "ModelOutput"},
        {"type": "ToolCall"},
        {"type": "ToolResult"},
        {"type": "ModelOutput"},
    ]
    assert match_event_sequence(
        ["ToolCall", "ToolResult", "ModelOutput"], events
    ).passed is True


def test_event_sequence_absent_or_out_of_order() -> None:
    events = [{"type": "ModelOutput"}, {"type": "ToolResult"}, {"type": "ToolCall"}]
    # order reversed → not a contiguous run
    assert match_event_sequence(["ToolCall", "ToolResult"], events).passed is False
    # missing entirely
    assert match_event_sequence(["Error"], events).passed is False


# ─── Result / run plumbing ──────────────────────────────────────────────────


def test_assertion_result_is_frozen_value() -> None:
    r = AssertionResult(name="x", passed=True)
    assert r.detail == ""  # default
    assert r.passed is True


def test_evalrun_transcript_joins_assistant_outputs() -> None:
    run = EvalRun(turn_outputs=["Hi Alice", "You work on data-platform"])
    assert "Alice" in run.transcript
    assert "data-platform" in run.transcript
