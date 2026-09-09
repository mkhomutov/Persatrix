"""RFC 0049 / ISSUE-0132 — capturing the runtime's shadow traces.

The shadow passes report what they *would* have done through structured
log records rather than return values, so the RFC 0044 harness reads them
the way an operator does: attach a handler, run the recipe, partition the
merged stream on each payload's ``tier`` key
(``evaluators/shadow_measurement.py``).

Split out of ``persona_driver`` (v0.3.16) when that module reached its
size-cap split point: the capture is a self-contained seam with its own
tests, and the driver only uses it as a context manager. Re-exported
there for the callers that import it from the driver.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["capture_shadow_traces"]


class _ShadowTraceHandler(logging.Handler):
    """Collects the structured payload off each shadow log record.

    ``traces`` may be shared across handlers (RFC 0049 PR 3: one handler
    per shadow logger, one merged stream) — records append in emission
    order, so a run's L2 (facts) and L1 (episodes) traces interleave
    chronologically; consumers partition on each payload's ``tier`` key.
    """

    def __init__(self, attr: str, traces: list[dict[str, Any]]) -> None:
        super().__init__(level=logging.INFO)
        self._attr = attr
        self.traces = traces

    def emit(self, record: logging.LogRecord) -> None:
        payload = getattr(record, self._attr, None)
        if isinstance(payload, dict):
            self.traces.append(payload)


@contextmanager
def capture_shadow_traces() -> Iterator[list[dict[str, Any]]]:
    """Capture the runtime's shadow traces for the block's duration.

    Attaches a collecting handler directly to each of the runtime's
    shadow loggers — ``agents.persona_runtime.facts_shadow`` (L2, RFC
    0049 PR 2), ``agents.persona_runtime.episodes_shadow`` (L1, PR 3)
    and ``agents.persona_runtime.audience_shadow`` (the ISSUE-0132
    audience verdicts, v0.3.16 A2) — and,
    because a handler only sees records the logger's effective level
    admits, temporarily lowers each to ``DEBUG`` when the ambient config
    would filter the trace. ``DEBUG`` rather than ``INFO`` because the
    audience trace attaches its per-entry ``candidates`` array only at
    that level (production pays the constant-size record instead), and
    the seeds' evidence is exactly that array. Both are restored on exit,
    so the harness
    never perturbs the process's logging outside the run. Shadow traces
    are the measurement input for the RFC 0049 PR 4 shadow→live
    promotion gate; the runner threads the captured (merged, ``tier``-
    keyed) list into the report artifact.

    The runtime imports are deferred (module convention: ``agents``
    loads only on the driver paths, never from ``import evaluators``).
    """
    from agents.persona_runtime import (  # noqa: PLC0415
        audience_shadow,
        episodes_shadow,
        facts_shadow,
    )

    traces: list[dict[str, Any]] = []
    attached: list[tuple[logging.Logger, logging.Handler, int]] = []
    try:
        for mod in (facts_shadow, episodes_shadow, audience_shadow):
            shadow_logger = logging.getLogger(mod.SHADOW_LOGGER_NAME)
            handler = _ShadowTraceHandler(mod.SHADOW_TRACE_ATTR, traces)
            prev_level = shadow_logger.level
            shadow_logger.addHandler(handler)
            if shadow_logger.getEffectiveLevel() > logging.DEBUG:
                shadow_logger.setLevel(logging.DEBUG)
            attached.append((shadow_logger, handler, prev_level))
        yield traces
    finally:
        for attached_logger, attached_handler, level in attached:
            attached_logger.removeHandler(attached_handler)
            attached_logger.setLevel(level)
