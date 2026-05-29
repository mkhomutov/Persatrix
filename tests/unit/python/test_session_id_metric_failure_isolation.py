"""
PR #337 deep review finding **M1** — span must not be marked ERROR
when ``sessions.writes`` raises *after* a successful ``db.commit()``.

The PR 4 follow-up landed a per-session ``sessions.writes`` counter in
both ``EpisodicMemory.store_episode`` and ``record_interaction``.  In
``record_interaction`` (``agents/memory/relationship_mutations.py:267``)
the increment is *outside* any ``try/except``.  In ``store_episode``
the increment was originally *inside* the outer ``try:`` wrapping
validation + persistence; that block's ``except`` calls
``span.record_exception`` + ``span.set_status(StatusCode.ERROR)`` and
re-raises.  But even moving the metric "outside the try" is not
enough on the persona-runtime span path: ``start_as_current_span``
defaults to ``record_exception=True`` and ``set_status_on_exception=True``,
so an exception escaping the ``with`` block still marks the span ERROR
on context exit.

The OTEL SDK contract for ``Counter.add()`` is best-effort and is
*not* required to be exception-free.  If it ever raises, the failure
mode under the *old* layout is:

1. ``await db.commit()`` succeeds — the row is persisted.
2. ``inst.sessions_writes.add(...)`` raises.
3. The exception either (a) is caught by the outer ``except`` and
   marks the span ERROR explicitly, OR (b) escapes the ``with``
   block and the span context manager marks it ERROR on exit.
4. ``raise`` propagates to the caller.

The caller sees an exception **and** the trace says the write failed,
yet the row is in the DB.  An operator triaging "failed remembers" via
Jaeger sees ghost failures.

The fix wraps the metric call site in ``contextlib.suppress(Exception)``
so a metric-backend failure on the post-commit branch is fully
isolated: the caller sees the write succeed, the span stays clean.
This test pins that invariant.

RFC 0031 PR plan PR 4 — review-feedback M1 (asymmetry with
``record_interaction`` was the surface symptom; the real fix is the
suppress wrapper at the metric site).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from agents.memory import _salience as _sal_mod
from agents.memory.episodic import EpisodicMemory
from agents.observability.spans import EPISODIC_REMEMBER_SPAN

# ISSUE-0081 PR 3: the post-commit ``sessions.writes`` emission +
# ``contextlib.suppress`` wrapper were extracted from ``episodic.py`` into
# the shared ``agents.memory._salience.emit_session_write`` shim, so the
# failure-injection patch targets ``_salience.try_get_instruments`` (the
# new lookup site).  The end-to-end invariant under test is unchanged:
# a metric-backend failure after ``db.commit()`` must not propagate to the
# caller or mark the remember span ERROR.


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """In-memory span exporter wired into the active tracer provider.

    Same pattern as ``agents/tests/test_observability_spans.py`` — kept
    local to this file so the test does not couple to ``agents/tests``'
    fixture discovery.
    """
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exp = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exp)
    provider.add_span_processor(processor)
    yield exp
    processor.shutdown()


class _RaisingCounter:
    """Stand-in for ``_Instruments.sessions_writes`` whose ``add`` always
    raises.  Mirrors the OTEL ``Counter`` surface needed by the call
    site at ``episodic.py``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, Any]]] = []

    def add(self, value: int, attributes: dict[str, Any] | None = None) -> None:
        # Record the call so the test can assert it was attempted even
        # after the regression fix — the call site must still try to
        # emit the metric; only the failure mode changes.
        self.calls.append((value, dict(attributes or {})))
        raise RuntimeError("simulated OTEL backend failure (test fixture)")


class _RaisingInstruments:
    """Minimal duck-typed stand-in for the ``_Instruments`` object
    returned by ``try_get_instruments()``.  Only the attribute the
    metric site touches (``sessions_writes``) is implemented.
    """

    def __init__(self) -> None:
        self.sessions_writes = _RaisingCounter()


def _find_remember_span(exporter: InMemorySpanExporter):  # noqa: ANN202
    matches = [
        s for s in exporter.get_finished_spans()
        if s.name == EPISODIC_REMEMBER_SPAN
    ]
    assert matches, (
        f"expected an {EPISODIC_REMEMBER_SPAN} span; got: "
        f"{[s.name for s in exporter.get_finished_spans()]!r}"
    )
    return matches[-1]


async def test_metric_failure_after_commit_does_not_mark_span_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exporter: InMemorySpanExporter,
) -> None:
    """M1 regression: ``sessions.writes.add`` raising AFTER ``db.commit()``
    must leave the remember span un-flagged AND must not surface to the
    caller as a write failure — the ``contextlib.suppress(Exception)``
    wrapper at the metric call site is the load-bearing piece.
    """
    raising = _RaisingInstruments()
    monkeypatch.setattr(_sal_mod, "try_get_instruments", lambda: raising)

    mem = EpisodicMemory(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    await mem.initialize()
    try:
        # No ``pytest.raises``: the suppress() wrapper must isolate the
        # metric-backend failure entirely.  The row is already persisted
        # at this point — making the caller pay for an OTEL hiccup
        # would be wrong.  Returns the episode_id on success.
        episode_id = await mem.store_episode("a row that did commit", {})
        assert episode_id, (
            "store_episode must return a non-empty episode id even when "
            "the metric backend raised (commit already succeeded)"
        )
        # The row really is in the DB — recall it to prove M1's premise
        # (commit-before-metric ordering) holds.
        results = await mem.recall("row that did commit")
        assert results, (
            "the row was not persisted, contradicting M1's assumption "
            "that db.commit() runs before the metric increment"
        )
    finally:
        await mem.close()

    # The metric site must have been reached — guard against any
    # future refactor that accidentally moves the metric BEFORE commit
    # (which would mask this test's intent).
    assert raising.sessions_writes.calls, (
        "the metric site was not reached at all; the test can no longer "
        "distinguish 'metric failure isolated from span' from 'metric "
        "site removed entirely'"
    )

    # The row was committed before the metric raise, so the span must
    # record success.  Pre-fix this assertion fails because the outer
    # ``except`` calls ``span.set_status(StatusCode.ERROR, ...)`` (and
    # because ``start_as_current_span`` defaults to
    # ``set_status_on_exception=True``, even moving the metric outside
    # the try without ``suppress`` would still mark the span ERROR
    # when the exception escapes the ``with`` block).
    remember_spans = [
        s for s in exporter.get_finished_spans()
        if s.name == EPISODIC_REMEMBER_SPAN
    ]
    assert remember_spans, "the remember span did not finish"
    # Only assert on the most recent remember span — recall() also runs
    # under a span but it's a different name so it doesn't show up here.
    span = remember_spans[-1]
    assert span.status.status_code.name != "ERROR", (
        "M1: a successful db.commit followed by an OTEL metric failure "
        "must NOT mark the agent.memory.episodic.remember span as ERROR "
        "(the row IS persisted; operators triaging failed writes via "
        "traces must not see a ghost failure)."
    )


async def test_metric_failure_does_not_record_exception_on_span(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exporter: InMemorySpanExporter,
) -> None:
    """Companion to the status check: ``span.record_exception`` is the
    other half of the ERROR path; the suppress wrapper must skip it
    too.  A future refactor that left ``record_exception`` reachable
    would still leak a spurious exception event into the trace.
    """
    monkeypatch.setattr(
        _sal_mod, "try_get_instruments", lambda: _RaisingInstruments(),
    )

    mem = EpisodicMemory(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    await mem.initialize()
    try:
        # Same contract as above: no exception expected from the call.
        await mem.store_episode("another row that did commit", {})
    finally:
        await mem.close()

    remember_spans = [
        s for s in exporter.get_finished_spans()
        if s.name == EPISODIC_REMEMBER_SPAN
    ]
    assert remember_spans, "the remember span did not finish"
    span = remember_spans[-1]
    event_names = [e.name for e in span.events]
    assert "exception" not in event_names, (
        "M1: a post-commit metric failure must not appear as a span "
        f"``exception`` event; events were: {event_names!r}"
    )


async def test_validation_error_still_marks_span_error(
    tmp_path: Path,
    exporter: InMemorySpanExporter,
) -> None:
    """Guard against over-correction: M1 must NOT remove the existing
    span-ERROR contract for validation / persistence failures *before*
    commit.  The PR #167 review Must-Fix #2 regression test in
    ``agents/tests/test_observability_spans.py`` exercises empty-summary
    via ``ValueError``; we re-pin it here so a future M1 refactor that
    drops the try/except wholesale also fails this file.
    """
    mem = EpisodicMemory(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    await mem.initialize()
    try:
        with pytest.raises(ValueError):
            await mem.store_episode("", {})
    finally:
        await mem.close()

    span = _find_remember_span(exporter)
    assert span.status.status_code.name == "ERROR", (
        "M1 fix must not erase the pre-commit ERROR-status contract — "
        "validation failures (empty summary) must still mark the span "
        "as ERROR per PR #167 Must-Fix #2."
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
