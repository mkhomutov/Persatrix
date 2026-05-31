"""Config-sourced event-timeout coercion for the persona runtime.

Extracted from ``persona_runtime/__init__.py`` (ISSUE-0053) to keep that
package-root file under the 500-line code cap, mirroring the existing
``conversation_window`` / ``summarize_close`` extraction precedent.
``__init__`` re-exports :func:`_coerce_event_timeout` so every existing
importer (``agents/persona.py`` and the test suite) is unaffected.

The single helper coerces a config value to ``float`` with an optional
floor, used by ``_LLMPersonaAgent`` for ``event_timeout`` and the
``interaction_idle_timeout_sec`` aggregation guard.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _coerce_event_timeout(
    raw_value: object,
    default: float,
    agent_id: str,
    *,
    min_value: float | None = None,
    setting_name: str = "event_timeout",
) -> float:
    """Coerce a config-sourced timeout to ``float`` with optional floor.

    Returns *default* (and warns) when coercion fails or — when
    ``min_value`` is given — when the coerced value is ``<= min_value``.
    PR-3 review #19 folded the prior caller-side ``<= 0`` re-check for
    ``interaction_idle_timeout_sec`` into this helper.  PR #60 review
    extracted the original try/float guard from on_event / on_tick.
    """
    try:
        value = float(raw_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning(
            "Agent %s: invalid %s %r, using default %.0fs",
            agent_id, setting_name, raw_value, default,
        )
        return default
    if min_value is not None and value <= min_value:
        logger.warning(
            "Agent %s: %s=%r is not greater than %r; using default %.0fs",
            agent_id, setting_name, value, min_value, default,
        )
        return default
    return value
