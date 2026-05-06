"""
Pure rendering functions for the temporal layer (RFC 0021 §C/§D).

Three operator-visible string-rendering primitives:

* :func:`format_relative` — bucketed past/future tense ("3 min ago",
  "in 2 hours", "yesterday", "next week"…).  Used to prefix recalled
  episodes, relationship summaries, and forward-looking commitments.
* :func:`format_duration` — bucketed elapsed-time prefix ("over 47 min",
  "over 2 hours") for episode summaries that aggregate ≥2 turns.
* :func:`format_part_of_day` — coarse hour-of-day word ("morning",
  "afternoon"…) for the now-anchor block in the system prompt.

Hard rule: the LLM never does date arithmetic.  Every relative or
absolute string the model sees is pre-computed here.

Bucketing is duration-driven in PR 1.  Calendar-aware alternatives
("today, HH:MM", "last <weekday>", calendar-tomorrow) are an explicit
PR 2 follow-up — the RFC §D table lists them as "or" forms, and the
duration form is sufficient for the prompt-shape contract PR 2 lands.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

# ─── Bucket boundaries (seconds) ──────────────────────────────
#
# Pinned as named constants so a future calibration change touches one
# table rather than scattering ``86_400`` literals through the bucket
# branches.  The 30-day month approximation is documented at RFC 0021
# §D — months are a recency rendering, not a calendar-month count.

_SECONDS_PER_MIN = 60
_SECONDS_PER_HOUR = 3_600
_SECONDS_PER_DAY = 86_400
_SECONDS_PER_WEEK = 7 * _SECONDS_PER_DAY
_SECONDS_PER_MONTH = 30 * _SECONDS_PER_DAY  # RFC 0021 §D approximation
_SECONDS_PER_YEAR = 365 * _SECONDS_PER_DAY


def _resolve_tz(tz: str | ZoneInfo) -> ZoneInfo:
    if isinstance(tz, ZoneInfo):
        return tz
    return ZoneInfo(tz)


def _format_past(delta: float) -> str:
    if delta < _SECONDS_PER_MIN:
        return "just now"
    if delta < _SECONDS_PER_HOUR:
        minutes = int(delta // _SECONDS_PER_MIN)
        return f"{minutes} min ago"
    if delta < _SECONDS_PER_DAY:
        hours = int(delta // _SECONDS_PER_HOUR)
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    if delta < 2 * _SECONDS_PER_DAY:
        return "yesterday"
    if delta < _SECONDS_PER_WEEK:
        days = int(delta // _SECONDS_PER_DAY)
        return f"{days} days ago"
    if delta < 2 * _SECONDS_PER_WEEK:
        return "last week"
    # RFC 0021 §D: "14–60 days" inclusive on the 60-day boundary;
    # 61 days is the first month-bucket value.
    if delta < 61 * _SECONDS_PER_DAY:
        weeks = int(delta // _SECONDS_PER_WEEK)
        return f"{weeks} weeks ago"
    if delta < _SECONDS_PER_YEAR:
        months = int(delta // _SECONDS_PER_MONTH)
        return f"{months} months ago"
    return "over a year ago"


def _format_future(delta: float) -> str:
    if delta < _SECONDS_PER_MIN:
        return "any moment"
    if delta < _SECONDS_PER_HOUR:
        minutes = int(delta // _SECONDS_PER_MIN)
        return f"in {minutes} min"
    if delta < _SECONDS_PER_DAY:
        hours = int(delta // _SECONDS_PER_HOUR)
        return "in 1 hour" if hours == 1 else f"in {hours} hours"
    if delta < 2 * _SECONDS_PER_DAY:
        return "tomorrow"
    if delta < _SECONDS_PER_WEEK:
        days = int(delta // _SECONDS_PER_DAY)
        return f"in {days} days"
    if delta < 2 * _SECONDS_PER_WEEK:
        return "next week"
    # RFC 0021 §D — symmetric with past bucket's 60-day inclusive cap.
    if delta < 61 * _SECONDS_PER_DAY:
        weeks = int(delta // _SECONDS_PER_WEEK)
        return f"in {weeks} weeks"
    if delta < _SECONDS_PER_YEAR:
        months = int(delta // _SECONDS_PER_MONTH)
        return f"in {months} months"
    return "over a year out"


def format_relative(then: float, now: float, tz: str | ZoneInfo = "UTC") -> str:
    """Render ``then`` relative to ``now`` as a bucketed phrase.

    Past targets render past-tense ("3 min ago"); future targets
    render future-tense ("in 3 min").  See :mod:`agents.temporal.rendering`
    module docstring for the bucket table.

    The ``tz`` argument is validated even though the duration-driven
    PR 1 buckets do not consume it — passing an invalid zone here
    surfaces the misconfiguration before PR 2's calendar-aware
    rendering depends on a working zone.
    """
    _resolve_tz(tz)
    delta = now - then
    if delta >= 0:
        return _format_past(delta)
    return _format_future(-delta)


def format_duration(seconds: float) -> str:
    """Render a positive elapsed duration as an ``"over N <unit>"`` prefix.

    Used for episode-summary duration tags ("[over 47 min, with Bob]").
    Sub-minute durations render as ``"less than a minute"`` — the
    operator-visible string must not over-claim duration on near-
    instant interactions.
    """
    if seconds < 0:
        raise ValueError("format_duration requires a non-negative duration")
    if seconds < _SECONDS_PER_MIN:
        return "less than a minute"
    if seconds < _SECONDS_PER_HOUR:
        minutes = int(seconds // _SECONDS_PER_MIN)
        return f"over {minutes} min"
    if seconds < _SECONDS_PER_DAY:
        hours = int(seconds // _SECONDS_PER_HOUR)
        return "over 1 hour" if hours == 1 else f"over {hours} hours"
    days = int(seconds // _SECONDS_PER_DAY)
    return "over 1 day" if days == 1 else f"over {days} days"


_PART_OF_DAY_BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 5, "early morning"),
    (5, 8, "morning"),
    (8, 12, "late morning"),
    (12, 17, "afternoon"),
    (17, 21, "evening"),
    (21, 24, "night"),
)


def format_part_of_day(hour: int) -> str:
    """Coarse part-of-day word for a 0–23 hour-of-day input.

    Bands are pinned by RFC 0021 §C — text is bikeshed-fodder, but the
    mapping is stable so tests can assert on the rendered now-anchor
    line.
    """
    if hour < 0 or hour > 23:
        raise ValueError(f"format_part_of_day expects hour in [0, 23], got {hour}")
    for lo, hi, label in _PART_OF_DAY_BANDS:
        if lo <= hour < hi:
            return label
    # Unreachable — bands cover [0, 24).  Belt-and-braces so a future
    # band-table edit that introduces a gap surfaces immediately.
    raise ValueError(f"format_part_of_day band table has a gap at hour={hour}")


__all__ = [
    "format_duration",
    "format_part_of_day",
    "format_relative",
]
