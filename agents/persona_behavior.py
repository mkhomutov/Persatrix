"""
Persatrix Persona Behavioral Dimensions.

Structured behavior dimension descriptions and rendering for LLM prompts.
Extracted from ``persona.py`` for modularity — no logic changes.

The ``DIMENSION_DESCRIPTIONS`` content is sourced from
``prompts/runtime/persona/sections/behavior-dimensions.yaml`` via
``agents.prompt_loader.load_dimension_descriptions``.  The Python
module retains the ``_DIMENSION_DEFAULTS`` table and the import-time
consistency check because those encode invariants (which dimensions
the persona system supports, and what the middle value of each is),
not content.
"""

from __future__ import annotations

import logging

from .prompt_loader import load_dimension_descriptions

logger = logging.getLogger(__name__)

__all__ = ["DIMENSION_DESCRIPTIONS", "render_behavior"]


# Loaded once at import time from
# ``prompts/runtime/persona/sections/behavior-dimensions.yaml``.  The
# loader caches the parse, so re-reading via ``load_dimension_descriptions``
# from other call sites is cheap.  Re-exposed as a module-level name to
# preserve the public API previously offered by this module.
DIMENSION_DESCRIPTIONS: dict[str, dict[str, str]] = load_dimension_descriptions()

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
# Caught at import time: if a future dimension is added to one source and
# not the other, the mismatch surfaces immediately rather than silently
# producing incomplete behavioral prompts.  Now that DIMENSION_DESCRIPTIONS
# lives in YAML, this also catches a YAML edit that adds/removes a
# dimension without the matching code update.
# Uses if/raise instead of assert because assert is stripped by python -O,
# which would silently disable this guard in optimized production deployments.
# (PR review: no guard against _DIMENSION_DEFAULTS/DIMENSION_DESCRIPTIONS key drift.)
# (PR #64 review F-64-DR-04: assert stripped in optimized mode — use if/raise.)
if set(_DIMENSION_DEFAULTS) != set(DIMENSION_DESCRIPTIONS):
    raise RuntimeError(
        f"_DIMENSION_DEFAULTS keys {set(_DIMENSION_DEFAULTS)} do not match "
        f"DIMENSION_DESCRIPTIONS keys {set(DIMENSION_DESCRIPTIONS)}"
    )

# Invariant: every default value must be a valid value for its dimension.
# Catches a YAML edit that renames the middle value of a dimension (e.g.
# "balanced" → "neutral") without the matching ``_DIMENSION_DEFAULTS``
# update — otherwise ``render_behavior({})`` would silently produce no
# line for that dimension.
for _dim, _default_value in _DIMENSION_DEFAULTS.items():
    if _default_value not in DIMENSION_DESCRIPTIONS[_dim]:
        raise RuntimeError(
            f"Default value {_default_value!r} for dimension {_dim!r} is not "
            f"a known value (valid: "
            f"{sorted(DIMENSION_DESCRIPTIONS[_dim])})"
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
