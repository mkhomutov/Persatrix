"""Shared OTel test helpers.

Single implementations of the two shapes metric tests across the repo
had been re-deriving inline: the OTel-counter assertion walk
(:func:`counter_total`) and the in-memory meter bootstrap
(:func:`build_meter`).  Lives at the top of ``tests/`` so unit and
integration suites can both import it via ``conftest.py``'s
``sys.path`` insertion.

History: PR 298 (RFC 0020 PR 6 slice 4) was about to land a third
copy of ``_counter_total`` in
``tests/integration/_persona_parity_helpers.py``.  Slice-4 review N5
flagged the duplication; this module collapses that copy plus the two
pre-existing ones in ``test_interaction_tracker`` and
``test_summarize_close_helpers`` into a single source of truth.

PR 347 (RFC 0026 PR 5d) review N1 hoisted ``build_meter`` here for the
same reason — the ``init_metrics(reader=InMemoryMetricReader())``
bootstrap was duplicated verbatim across the facts-tier metric
suites.  Other test files still carry a local ``_build_meter``; they
collapse onto this one as they are next touched.
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_meter", "counter_total"]


def build_meter() -> tuple[Any, Any]:
    """Bootstrap OTel metrics against a fresh in-memory reader.

    Returns ``(reader, metrics_mod)``.  Counters are dead until
    ``init_metrics`` has run — ``try_get_instruments`` returns
    ``None`` until then — so a metric test calls this in setup and
    reads the result back through :func:`counter_total`.

    Teardown is the caller's: wrap the test body in ``try / finally:
    await metrics_mod.shutdown()`` so the global provider is nulled
    and metrics state does not leak into later tests.
    """
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    from agents.observability import metrics as metrics_mod  # noqa: PLC0415

    reader = InMemoryMetricReader()
    metrics_mod.init_metrics(reader=reader)
    return reader, metrics_mod


def counter_total(reader: Any, name: str) -> int:
    """Sum every data point of an OTel counter exported through ``reader``.

    Walks the SDK's ``MetricsData → ResourceMetrics → ScopeMetrics →
    Metric → DataPoint`` shape and returns ``0`` for unrecorded
    counters or readers that have not yet seen any data.
    """
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
