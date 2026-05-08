"""Shared OTel test helpers.

One implementation of the OTel-counter assertion shape every metric
test in the repo had been re-deriving inline.  Lives at the top of
``tests/`` so unit and integration suites can both import it via
``conftest.py``'s ``sys.path`` insertion.

History: PR 298 (RFC 0020 PR 6 slice 4) was about to land a third
copy of ``_counter_total`` in
``tests/integration/_persona_parity_helpers.py``.  Slice-4 review N5
flagged the duplication; this module collapses that copy plus the two
pre-existing ones in ``test_interaction_tracker`` and
``test_summarize_close_helpers`` into a single source of truth.
"""

from __future__ import annotations

from typing import Any

__all__ = ["counter_total"]


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
