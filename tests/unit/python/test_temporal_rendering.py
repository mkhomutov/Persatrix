"""
Unit tests for :mod:`agents.temporal.rendering` (RFC 0021 P1 PR 1).

The rendering helpers are pure functions: ``(then, now, tz) -> str`` for
``format_relative``, ``seconds -> str`` for ``format_duration``, and
``hour -> str`` for ``format_part_of_day``.  They are the substrate the
prompt-assembly layer (PR 2) calls to render a now-anchor and recency
prefixes; the LLM never does date arithmetic itself.

Boundary coverage is intentionally exhaustive: each bucket transition
in RFC 0021 §D is exercised at ``boundary - 1`` and ``boundary``, so an
off-by-one in the implementation surfaces deterministically.

Reference instant for cross-test math:

* ``NOW = 1_714_132_800`` (epoch seconds) is ``2024-04-26T12:00:00+00:00``,
  a Friday — chosen so weekday-relative renders (added later in PR 2)
  remain unambiguous.
"""

from __future__ import annotations

import pytest

from agents.temporal.rendering import (
    format_cadence,
    format_duration,
    format_now_anchor,
    format_part_of_day,
    format_relative,
)

# Friday 2024-04-26T12:00:00+00:00.  Rendering is duration-driven in
# PR 1 (calendar-aware "yesterday"/"last <weekday>" alternatives are a
# PR 2 follow-up), so the absolute instant only matters for the
# timezone-smoke test below.
NOW = 1_714_132_800.0


# ─── format_relative — past tense ─────────────────────────────


class TestFormatRelativePast:
    @pytest.mark.parametrize("delta_sec", [0, 1, 30, 59])
    def test_under_a_minute_renders_just_now(self, delta_sec: int) -> None:
        assert format_relative(NOW - delta_sec, NOW) == "just now"

    @pytest.mark.parametrize(
        "delta_sec,expected",
        [
            (60, "1 min ago"),
            (61, "1 min ago"),
            (120, "2 min ago"),
            (3599, "59 min ago"),
        ],
    )
    def test_minute_bucket(self, delta_sec: int, expected: str) -> None:
        assert format_relative(NOW - delta_sec, NOW) == expected

    @pytest.mark.parametrize(
        "delta_sec,expected",
        [
            (3600, "1 hour ago"),
            (3601, "1 hour ago"),
            (7200, "2 hours ago"),
            (86399, "23 hours ago"),
        ],
    )
    def test_hour_bucket(self, delta_sec: int, expected: str) -> None:
        # The singular ``1 hour`` form is intentional — ``1 hours`` is
        # ungrammatical and would surface in every operator-visible
        # render of an episode that closed 1–2 hours ago.
        assert format_relative(NOW - delta_sec, NOW) == expected

    @pytest.mark.parametrize("delta_sec", [86400, 172_799])
    def test_one_day_window_renders_yesterday(self, delta_sec: int) -> None:
        # 24h – 48h - 1s window — the day boundary is duration-based in
        # PR 1; calendar-aware "yesterday" lands in PR 2.
        assert format_relative(NOW - delta_sec, NOW) == "yesterday"

    @pytest.mark.parametrize(
        "delta_days,expected",
        [
            (2, "2 days ago"),
            (3, "3 days ago"),
            (6, "6 days ago"),
        ],
    )
    def test_days_bucket(self, delta_days: int, expected: str) -> None:
        assert format_relative(NOW - delta_days * 86_400, NOW) == expected

    @pytest.mark.parametrize("delta_days", [7, 8, 13])
    def test_one_to_two_weeks_renders_last_week(self, delta_days: int) -> None:
        assert format_relative(NOW - delta_days * 86_400, NOW) == "last week"

    @pytest.mark.parametrize(
        "delta_days,expected",
        [
            (14, "2 weeks ago"),
            (20, "2 weeks ago"),
            (21, "3 weeks ago"),
            (60, "8 weeks ago"),
        ],
    )
    def test_weeks_bucket(self, delta_days: int, expected: str) -> None:
        assert format_relative(NOW - delta_days * 86_400, NOW) == expected

    @pytest.mark.parametrize(
        "delta_days,expected",
        [
            (61, "2 months ago"),
            (90, "3 months ago"),
            (180, "6 months ago"),
            (364, "12 months ago"),
        ],
    )
    def test_months_bucket(self, delta_days: int, expected: str) -> None:
        # Months use a 30-day approximation per RFC 0021 §D — the
        # rendering is operator-facing recency, not calendar arithmetic.
        assert format_relative(NOW - delta_days * 86_400, NOW) == expected

    @pytest.mark.parametrize("delta_days", [365, 400, 730])
    def test_over_a_year_ago(self, delta_days: int) -> None:
        assert format_relative(NOW - delta_days * 86_400, NOW) == "over a year ago"


# ─── format_relative — future tense ───────────────────────────


class TestFormatRelativeFuture:
    @pytest.mark.parametrize("delta_sec", [1, 30, 59])
    def test_under_a_minute_renders_any_moment(self, delta_sec: int) -> None:
        # ``delta == 0`` is intentionally exercised in the past-tense
        # suite ("just now"); past tense wins on the boundary so a
        # commitment that just landed and an episode that just closed
        # render the same way.
        assert format_relative(NOW + delta_sec, NOW) == "any moment"

    @pytest.mark.parametrize(
        "delta_sec,expected",
        [
            (60, "in 1 min"),
            (120, "in 2 min"),
            (3599, "in 59 min"),
        ],
    )
    def test_minute_bucket(self, delta_sec: int, expected: str) -> None:
        assert format_relative(NOW + delta_sec, NOW) == expected

    @pytest.mark.parametrize(
        "delta_sec,expected",
        [
            (3600, "in 1 hour"),
            (7200, "in 2 hours"),
            (86399, "in 23 hours"),
        ],
    )
    def test_hour_bucket(self, delta_sec: int, expected: str) -> None:
        assert format_relative(NOW + delta_sec, NOW) == expected

    @pytest.mark.parametrize("delta_sec", [86400, 172_799])
    def test_one_day_window_renders_tomorrow(self, delta_sec: int) -> None:
        assert format_relative(NOW + delta_sec, NOW) == "tomorrow"

    @pytest.mark.parametrize(
        "delta_days,expected",
        [
            (2, "in 2 days"),
            (3, "in 3 days"),
            (6, "in 6 days"),
        ],
    )
    def test_days_bucket(self, delta_days: int, expected: str) -> None:
        assert format_relative(NOW + delta_days * 86_400, NOW) == expected

    @pytest.mark.parametrize("delta_days", [7, 8, 13])
    def test_one_to_two_weeks_renders_next_week(self, delta_days: int) -> None:
        assert format_relative(NOW + delta_days * 86_400, NOW) == "next week"

    @pytest.mark.parametrize(
        "delta_days,expected",
        [
            (14, "in 2 weeks"),
            (21, "in 3 weeks"),
            (60, "in 8 weeks"),
        ],
    )
    def test_weeks_bucket(self, delta_days: int, expected: str) -> None:
        assert format_relative(NOW + delta_days * 86_400, NOW) == expected

    @pytest.mark.parametrize(
        "delta_days,expected",
        [
            (61, "in 2 months"),
            (180, "in 6 months"),
            (364, "in 12 months"),
        ],
    )
    def test_months_bucket(self, delta_days: int, expected: str) -> None:
        assert format_relative(NOW + delta_days * 86_400, NOW) == expected

    @pytest.mark.parametrize("delta_days", [365, 400, 730])
    def test_over_a_year_out(self, delta_days: int) -> None:
        assert format_relative(NOW + delta_days * 86_400, NOW) == "over a year out"


# ─── format_relative — timezone smoke ─────────────────────────


class TestFormatRelativeTimezone:
    def test_utc_stored_timestamp_renders_under_non_utc_tz(self) -> None:
        # The bucket choice is duration-based in PR 1, so the timezone
        # argument does not change the rendered string for a 3-hour
        # delta.  This test pins that contract — a future change that
        # makes rendering tz-sensitive must update both this test and
        # the docstring at the same time so the contract drift is loud.
        assert (
            format_relative(NOW - 3 * 3600, NOW, tz="America/Los_Angeles")
            == "3 hours ago"
        )

    def test_invalid_timezone_raises(self) -> None:
        with pytest.raises(Exception):
            format_relative(NOW - 60, NOW, tz="Not/A_Real_Zone")


# ─── format_duration ──────────────────────────────────────────


class TestFormatDuration:
    @pytest.mark.parametrize("seconds", [0, 1, 59])
    def test_sub_minute_renders_less_than_a_minute(self, seconds: int) -> None:
        # Episode-duration prefix on single-turn or near-instant
        # interactions; the operator-visible string must not lie about
        # duration in the 0–59s range.
        assert format_duration(seconds) == "less than a minute"

    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (60, "over 1 min"),
            (90, "over 1 min"),
            (2820, "over 47 min"),  # the RFC §D example
            (3599, "over 59 min"),
        ],
    )
    def test_minute_bucket(self, seconds: int, expected: str) -> None:
        assert format_duration(seconds) == expected

    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (3600, "over 1 hour"),
            (7200, "over 2 hours"),
            (86399, "over 23 hours"),
        ],
    )
    def test_hour_bucket(self, seconds: int, expected: str) -> None:
        assert format_duration(seconds) == expected

    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (86400, "over 1 day"),
            (172_800, "over 2 days"),
            (864_000, "over 10 days"),
        ],
    )
    def test_day_bucket(self, seconds: int, expected: str) -> None:
        assert format_duration(seconds) == expected

    def test_negative_duration_rejected(self) -> None:
        # ``format_duration`` is fed ``closed_at - started_at``.  A
        # negative result indicates a clock-skew or row-corruption bug
        # upstream; render the bug at the seam rather than emit a
        # misleading "less than a minute" prefix.
        with pytest.raises(ValueError):
            format_duration(-1)


# ─── format_part_of_day ───────────────────────────────────────


class TestFormatPartOfDay:
    @pytest.mark.parametrize("hour", [0, 1, 2, 3, 4])
    def test_early_morning(self, hour: int) -> None:
        assert format_part_of_day(hour) == "early morning"

    @pytest.mark.parametrize("hour", [5, 6, 7])
    def test_morning(self, hour: int) -> None:
        assert format_part_of_day(hour) == "morning"

    @pytest.mark.parametrize("hour", [8, 9, 10, 11])
    def test_late_morning(self, hour: int) -> None:
        assert format_part_of_day(hour) == "late morning"

    @pytest.mark.parametrize("hour", [12, 13, 14, 15, 16])
    def test_afternoon(self, hour: int) -> None:
        assert format_part_of_day(hour) == "afternoon"

    @pytest.mark.parametrize("hour", [17, 18, 19, 20])
    def test_evening(self, hour: int) -> None:
        assert format_part_of_day(hour) == "evening"

    @pytest.mark.parametrize("hour", [21, 22, 23])
    def test_night(self, hour: int) -> None:
        assert format_part_of_day(hour) == "night"

    @pytest.mark.parametrize("hour", [-1, 24, 25, 100])
    def test_out_of_range_hour_raises(self, hour: int) -> None:
        # ``format_part_of_day`` is fed ``datetime.hour`` (always 0–23
        # by construction).  An out-of-range hour signals an upstream
        # bug — surface it loudly rather than silently bucket it.
        with pytest.raises(ValueError):
            format_part_of_day(hour)


# ─── format_now_anchor (PR 2) ─────────────────────────────────


class TestFormatNowAnchor:
    """RFC 0021 §C: the now-anchor line injected into every system prompt."""

    def test_utc_friday_afternoon(self) -> None:
        # 2024-04-26T12:00:00Z — Friday at noon.  Validates ISO-8601
        # absolute, weekday name, and part-of-day word in the same render.
        assert format_now_anchor(NOW, "UTC") == (
            "Current time: 2024-04-26T12:00:00+00:00 (Friday afternoon)."
        )

    def test_persona_local_timezone_shifts_offset_and_part_of_day(self) -> None:
        # Same epoch in PT during DST renders as 05:00 — "morning".
        assert format_now_anchor(NOW, "America/Los_Angeles") == (
            "Current time: 2024-04-26T05:00:00-07:00 (Friday morning)."
        )

    def test_default_timezone_is_utc(self) -> None:
        assert format_now_anchor(NOW) == format_now_anchor(NOW, "UTC")


# ─── format_cadence (PR 2) ────────────────────────────────────


WEEK = 7 * 86_400
MONTH = 30 * 86_400


class TestFormatCadence:
    """RFC 0021 §E: coarse cadence bucket for relationship summaries."""

    def test_below_threshold_returns_none(self) -> None:
        # interaction_count <= 5 — not enough signal to bucket.
        assert format_cadence(5, 0.0, WEEK, WEEK) is None

    def test_missing_first_or_last_returns_none(self) -> None:
        assert format_cadence(10, None, WEEK, WEEK) is None
        assert format_cadence(10, 0.0, None, WEEK) is None

    def test_zero_lifetime_returns_none(self) -> None:
        # All interactions stamped at the same instant — no rate to compute.
        assert format_cadence(10, 100.0, 100.0, 100.0) is None

    def test_frequent_bucket(self) -> None:
        # 10 interactions across 30 days — 3 days/interaction → frequent.
        assert format_cadence(10, 0.0, MONTH, MONTH) == "frequent"

    def test_regular_bucket(self) -> None:
        # 10 interactions across 200 days — 20 days/interaction → regular.
        assert format_cadence(10, 0.0, 200 * 86_400, 200 * 86_400) == "regular"

    def test_sparse_bucket(self) -> None:
        # 10 interactions across 800 days — 80 days/interaction → sparse.
        assert format_cadence(10, 0.0, 800 * 86_400, 800 * 86_400) == "sparse"

    def test_quiet_relationship_drifts_toward_sparse(self) -> None:
        # 10 interactions in the first 100 days, then silence for two
        # years.  Bucketing against ``now`` rather than ``last`` keeps
        # the cadence honest — historical "frequent" decays to sparse
        # once the relationship goes cold.
        first = 0.0
        last = 100 * 86_400
        now = last + 700 * 86_400
        assert format_cadence(10, first, last, now) == "sparse"
