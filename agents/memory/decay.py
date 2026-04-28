"""
Confidence decay for the procedural memory tier (RFC 0008 PR plan PR 5).

Implements the exponential-decay formula from RFC 0008 §G
(``docs/rfcs/0008-agent-memory-context-optimization.md``):

    c_t = c_0 * exp(-lambda * t)

where ``t`` is the elapsed time in days since the entry's last
validation event (``last_validated_at`` if set, else ``created_at``).
The shipped default ``lambda = 0.01 / day`` gives a half-life of
``ln(2) / 0.01 ≈ 69.3 days``.

Decay is computed at *read time* — there is no periodic rewrite pass.
The episodic-tier query path multiplies the stored ``c_0`` by the decay
factor for the current ``time.time()`` and applies the ``c_min`` floor
before returning rows.  That keeps the storage path append-only and
makes the formula trivial to retune (one config value, no migration).

This module is dependency-free (stdlib only) so the eviction loop and
the read-side facade can both import it without pulling SQLite in.
"""

from __future__ import annotations

import math

__all__ = [
    "DEFAULT_C_MIN",
    "DEFAULT_LAMBDA_PER_DAY",
    "DEFAULT_STALE_CONFIDENCE_ALERT_THRESHOLD",
    "SECONDS_PER_DAY",
    "compute_decayed_confidence",
]


# Number of seconds in one day.  Inlined here so the formula does not
# repeat the magic number; matches the value used elsewhere in the
# memory subsystem (``eviction.py`` ``ttl_low_importance_days * 86400``).
SECONDS_PER_DAY: float = 86400.0


# RFC 0008 §G defaults.  Operators override via ``config/agents.yaml``
# ``memory.procedural_memory`` (see ``schemas/agent.schema.json``); the
# constants here are the *shipped* defaults and the values the unit
# tests pin.  PR 6 may retune them based on the 30-day calibration
# review (Open Question 12).
DEFAULT_LAMBDA_PER_DAY: float = 0.01
DEFAULT_C_MIN: float = 0.1
DEFAULT_STALE_CONFIDENCE_ALERT_THRESHOLD: float = 0.3


def compute_decayed_confidence(
    c0: float,
    age_seconds: float,
    lambda_per_day: float = DEFAULT_LAMBDA_PER_DAY,
) -> float:
    """Return ``c_0 * exp(-lambda_per_day * age_days)``.

    Parameters
    ----------
    c0:
        Stored confidence at the last validation event, in ``[0.0, 1.0]``.
        Values outside the range are clamped (defence in depth — the
        episodic schema applies the same clamp at write time, but the
        formula is still well-defined under arbitrary inputs).
    age_seconds:
        Elapsed wall-clock seconds since the last validation event.
        Negative values (clock skew) are clamped to ``0`` so a future
        ``last_validated_at`` cannot inflate the returned confidence
        above ``c_0``.
    lambda_per_day:
        Decay constant in inverse days.  Must be ``>= 0`` — a negative
        constant would *grow* confidence over time.

    Returns
    -------
    float
        Decayed confidence in ``[0.0, 1.0]``.  The exponential is
        bounded by ``c_0`` from above and approaches ``0`` as
        ``age_seconds -> inf``.
    """
    if lambda_per_day < 0:
        raise ValueError(
            f"lambda_per_day must be non-negative, got {lambda_per_day}",
        )
    # Clamp c0 to [0, 1] — see docstring rationale.
    c0_clamped = max(0.0, min(1.0, c0))
    # Clamp age to [0, inf) — see docstring rationale.
    age = max(0.0, age_seconds)
    age_days = age / SECONDS_PER_DAY
    return c0_clamped * math.exp(-lambda_per_day * age_days)
