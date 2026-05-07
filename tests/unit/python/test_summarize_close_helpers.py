"""Unit tests for ``agents.persona_runtime.summarize_close`` helpers.

Pins the PR 6 review follow-ups against the close-path helpers:

* :class:`TestMaybeRunJanitorCooldown` — review #29: two ``on_tick``
  calls within :data:`JANITOR_INTERVAL_SEC` run the cleanup once, not
  twice (the cooldown is exercised directly rather than via the
  persona event loop).
* :class:`TestJanitorFailedCounter` — review #24: a transient cleanup
  failure increments ``agent.interactions.janitor.failed`` so
  operators can SLO-alert on persistent janitor outages instead of
  silently accumulating stuck rows.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.persona_runtime.summarize_close import (
    JANITOR_INTERVAL_SEC,
    maybe_run_janitor,
)


def _build_meter():
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from agents.observability import metrics as metrics_mod

    reader = InMemoryMetricReader()
    metrics_mod.init_metrics(reader=reader)
    return reader, metrics_mod


def _counter_total(reader, name: str) -> int:
    data = reader.get_metrics_data()
    if data is None:
        return 0
    total = 0
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name == name:
                    for point in metric.data.data_points:
                        total += point.value
    return total


@pytest.mark.asyncio
class TestMaybeRunJanitorCooldown:
    """PR 6 review #29 — cooldown semantics under :data:`JANITOR_INTERVAL_SEC`.

    Two calls inside the cooldown window must invoke the cleanup
    callable exactly once.  Pins the contract so a future refactor that
    drops the monotonic guard surfaces immediately.
    """

    async def test_two_calls_within_interval_runs_cleanup_once(self):
        calls = 0

        async def _cleanup() -> int:
            nonlocal calls
            calls += 1
            return 0

        last = await maybe_run_janitor(
            _cleanup, last_monotonic=None,
            now_monotonic=1000.0, interval_sec=JANITOR_INTERVAL_SEC,
            agent_id="a",
        )
        assert calls == 1

        # Second call inside the cooldown window — must NOT run.
        last = await maybe_run_janitor(
            _cleanup, last_monotonic=last,
            now_monotonic=1000.0 + JANITOR_INTERVAL_SEC / 2,
            interval_sec=JANITOR_INTERVAL_SEC, agent_id="a",
        )
        assert calls == 1, "cleanup must not re-run inside the cooldown window"

        # Third call past the cooldown window — must run.
        await maybe_run_janitor(
            _cleanup, last_monotonic=last,
            now_monotonic=1000.0 + JANITOR_INTERVAL_SEC + 1.0,
            interval_sec=JANITOR_INTERVAL_SEC, agent_id="a",
        )
        assert calls == 2


@pytest.mark.asyncio
class TestJanitorFailedCounter:
    """PR 6 review #24 — failed sweeps increment a dedicated counter.

    Without this, a persistent DB outage stalls the sweep silently for
    :data:`JANITOR_INTERVAL_SEC` per failure (5 min default) and stuck
    ``[summary pending]`` rows accumulate without any operator signal.
    """

    async def test_cleanup_exception_increments_failed_counter(self):
        reader, metrics_mod = _build_meter()
        try:
            async def _boom() -> int:
                raise RuntimeError("simulated DB hiccup")

            # The helper must swallow the failure (best-effort contract).
            new_last = await maybe_run_janitor(
                _boom, last_monotonic=None,
                now_monotonic=1000.0, interval_sec=JANITOR_INTERVAL_SEC,
                agent_id="janitor-test-agent",
            )
            assert new_last == 1000.0, (
                "cooldown must advance even on failure so the next call "
                "does not hammer a struggling DB"
            )
            assert _counter_total(
                reader, "agent.interactions.janitor.failed",
            ) == 1
        finally:
            await metrics_mod.shutdown()

    async def test_successful_sweep_does_not_tick_failed_counter(self):
        reader, metrics_mod = _build_meter()
        try:
            async def _ok() -> int:
                return 0

            await maybe_run_janitor(
                _ok, last_monotonic=None,
                now_monotonic=1000.0, interval_sec=JANITOR_INTERVAL_SEC,
                agent_id="janitor-ok-agent",
            )
            assert _counter_total(
                reader, "agent.interactions.janitor.failed",
            ) == 0
        finally:
            await metrics_mod.shutdown()


# Keep ruff happy — ``asyncio`` is imported for the @pytest.mark.asyncio
# decorator's coroutine wrapping; rely on ``noqa`` only if ruff's
# ``unused-import`` rule complains in a future revision.
_ = asyncio
