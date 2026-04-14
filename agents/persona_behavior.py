"""
Orchestr8 Persona Behavioral Dimensions.

Structured behavior dimension descriptions and rendering for LLM prompts.
Extracted from ``persona.py`` for modularity — no logic changes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["DIMENSION_DESCRIPTIONS", "render_behavior"]


DIMENSION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "directness": {
        "indirect": (
            "Diplomatic and tactful. Softens criticism, asks questions"
            " instead of stating objections directly."
        ),
        "balanced": (
            "Balances directness with tact. States positions clearly"
            " but frames feedback constructively."
        ),
        "direct": (
            "Says exactly what they think."
            " Doesn't sugarcoat feedback or hedge opinions."
        ),
    },
    "detail_focus": {
        "big-picture": (
            "Focuses on high-level patterns and architecture."
            " Skips minutiae to keep discussions strategic."
        ),
        "balanced": (
            "Addresses both high-level concerns and"
            " specific details as needed."
        ),
        "detail-focused": (
            "Thorough and meticulous. Flags edge cases,"
            " checks specifics, prefers exhaustive analysis."
        ),
    },
    "formality": {
        "casual": (
            "Informal and approachable. Uses humor,"
            " contractions, and conversational language."
        ),
        "professional": (
            "Clear and structured. Uses professional"
            " language without being stiff."
        ),
        "formal": (
            "Precise and formal. Uses structured reports,"
            " proper titles, and measured language."
        ),
    },
    "risk_tolerance": {
        "cautious": (
            "Wants thorough analysis before decisions."
            " Asks for more data. Flags risks others might overlook."
        ),
        "moderate": (
            "Balances speed with diligence."
            " Comfortable with reasonable assumptions."
        ),
        "bold": (
            "Willing to make calls with incomplete information"
            " and course-correct. Bias toward action."
        ),
    },
    "expressiveness": {
        "reserved": (
            "Keeps emotions out of professional communication."
            " Focuses on facts and logic."
        ),
        "moderate": (
            "Acknowledges emotions when relevant"
            " but keeps focus on substance."
        ),
        "expressive": (
            "Openly shares reactions and feelings. Communication"
            " is warm, enthusiastic, or frustrated as the"
            " situation warrants."
        ),
    },
}

# Default middle value for each dimension when not specified.
_DIMENSION_DEFAULTS: dict[str, str] = {
    "directness": "balanced",
    "detail_focus": "balanced",
    "formality": "professional",
    "risk_tolerance": "moderate",
    "expressiveness": "moderate",
}

# Invariant: every key in _DIMENSION_DEFAULTS must also appear in
# DIMENSION_DESCRIPTIONS (so render_behavior() can look up descriptions),
# and vice versa (so defaults are available for every documented dimension).
# Caught at import time: if a future dimension is added to one dict and not
# the other, the mismatch surfaces immediately rather than silently producing
# incomplete behavioral prompts.
# Uses if/raise instead of assert because assert is stripped by python -O,
# which would silently disable this guard in optimized production deployments.
# (PR review: no guard against _DIMENSION_DEFAULTS/DIMENSION_DESCRIPTIONS key drift.)
# (PR #64 review F-64-DR-04: assert stripped in optimized mode — use if/raise.)
if set(_DIMENSION_DEFAULTS) != set(DIMENSION_DESCRIPTIONS):
    raise RuntimeError(
        f"_DIMENSION_DEFAULTS keys {set(_DIMENSION_DEFAULTS)} do not match "
        f"DIMENSION_DESCRIPTIONS keys {set(DIMENSION_DESCRIPTIONS)}"
    )


def render_behavior(behavior: dict[str, str]) -> str:
    """Convert structured behavior dimensions into natural language for LLM prompt.

    Applies defaults for omitted dimensions so the persona always has
    a complete behavioral profile.  Unknown dimension keys from ``behavior``
    are logged as warnings to aid config debugging.
    """
    merged = {**_DIMENSION_DEFAULTS, **behavior}
    lines: list[str] = []
    for dimension, value in merged.items():
        if dimension not in DIMENSION_DESCRIPTIONS:
            logger.warning(
                "Unknown behavior dimension %r (value=%r) — ignored",
                dimension,
                value,
            )
            continue
        desc = DIMENSION_DESCRIPTIONS[dimension].get(value)
        if desc:
            lines.append(f"- {desc}")
        else:
            # Known dimension but unknown value — log a warning so config
            # typos (e.g. "super-direct" instead of "direct") are visible
            # to operators.  The dimension still gets no line in the output,
            # but unlike the unknown-dimension case, the dimension key is
            # valid so the issue is the value.
            # (PR #64 review F-64-DR-05: unknown dimension values silently
            # produce no output — no warning logged.)
            logger.warning(
                "Unknown value %r for behavior dimension %r — "
                "no description produced (valid values: %s)",
                value,
                dimension,
                ", ".join(DIMENSION_DESCRIPTIONS[dimension]),
            )
    return "\n".join(lines)
