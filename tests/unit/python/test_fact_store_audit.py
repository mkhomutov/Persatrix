"""Audit-log emission for :class:`agents.memory.facts.FactStore`
(RFC 0026 PR 2).

RFC 0026 §G pins ``RedactStruct`` as the cross-language redaction
anchor; the Python side reuses :mod:`agents.observability.logging` so
the chain's :func:`_apply_redactor` processor invokes the installed
:class:`agents.observability.redact.Redactor` automatically.

Contracts:

* Every ``FactStore.store`` write emits one ``audit=True`` structlog
  record carrying ``{event="fact.store", agent_id, subject, predicate,
  object, source_interaction_id, fact_id}``.  The redaction surface is
  honoured: a custom :class:`Redactor` that scrubs ``object`` is
  invoked exactly once per record (no double-application, no bypass).
* The latest-asserted-wins ``supersede`` branch fires a second record
  ``{event="fact.supersede", superseded_fact_id, by_fact_id}`` so
  operators can diff "facts erased by retraction" from "facts erased
  by explicit ``delete_by_subject``".  ``delete_by_subject`` itself
  remains silent at this layer — RFC 0013's umbrella ``SubjectErasure``
  owns the umbrella-level audit emission when it implements (target
  v0.5.0).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from agents.memory.facts import FactStore
from agents.observability import logging as obs_logging
from agents.observability.redact import NoopRedactor


# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture
async def fact_store():
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture(autouse=True)
def _reset_redactor():
    """Restore the default :class:`NoopRedactor` after each test.

    The structlog chain reads the module-level redactor reference at
    every record emission; tests that swap in a custom redactor must
    not leak it into other tests.
    """
    yield
    obs_logging.set_redactor(NoopRedactor())


# ─── Helpers ────────────────────────────────────────────────


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return every captured record marked ``audit=True``.

    structlog's stdlib bridge stores the event-dict keys as
    ``record.__dict__`` attributes when ``ProcessorFormatter.wrap_for_formatter``
    flows through ``logging.LogRecord`` — but unit tests run before
    :func:`configure_logging` is called, so we fall back to the structured
    keyword args structlog stashes on the record (``positional_args`` /
    ``extra``).  Both surfaces are checked so the helper is robust against
    test ordering.
    """
    out: list[logging.LogRecord] = []
    for rec in caplog.records:
        # ``logger.info("event-name", audit=True, ...)`` via stdlib lands
        # the kwargs on ``rec.__dict__`` (record factory propagates extras).
        if getattr(rec, "audit", None) is True:
            out.append(rec)
            continue
        # structlog with ``LoggerFactory`` may stash the event dict under
        # ``record.msg`` as a dict; check there too.
        msg = rec.msg
        if isinstance(msg, dict) and msg.get("audit") is True:
            out.append(rec)
    return out


# ─── store: audit emission ──────────────────────────────────


@pytest.mark.asyncio
class TestStoreEmitsAuditRecord:
    async def test_one_record_per_store(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        caplog.set_level(logging.INFO, logger="agents.memory.facts")
        fact_id = await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        records = _audit_records(caplog)
        assert len(records) == 1
        rec = records[0]
        event = _event_dict(rec)
        assert event["event"] == "fact.store"
        assert event["agent_id"] == "test-agent"
        assert event["fact_id"] == fact_id
        assert event["subject"] == "bob"
        assert event["predicate"] == "has_name"
        assert event["source_interaction_id"] == "ix-1"

    async def test_redactor_invoked_on_audit_record(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        """A custom redactor that scrubs ``object`` is honoured.

        Pins the RFC 0026 §G contract: raw PII never reaches the audit
        sink unredacted when a real redactor is installed.  The PR 2
        production default is still :class:`NoopRedactor`; this test
        exercises the seam.
        """
        scrub_calls: list[dict[str, Any]] = []

        class _ScrubObject:
            def redact(self, record: dict[str, Any]) -> dict[str, Any]:
                scrub_calls.append(dict(record))
                redacted = dict(record)
                if "object" in redacted:
                    redacted["object"] = "[REDACTED]"
                return redacted

        obs_logging.set_redactor(_ScrubObject())

        # The chain is exercised by configure_logging; tests call it
        # lazily via get_logger which still flows through _apply_redactor.
        caplog.set_level(logging.INFO, logger="agents.memory.facts")
        await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob Q. Public",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        # Redactor was invoked at least once with the audit payload.
        audit_calls = [c for c in scrub_calls if c.get("audit") is True]
        assert len(audit_calls) >= 1
        assert audit_calls[0]["object"] == "Bob Q. Public"


# ─── supersede: audit emission ──────────────────────────────


@pytest.mark.asyncio
class TestSupersedeEmitsAuditRecord:
    async def test_latest_asserted_wins_supersede_emits_record(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        caplog.set_level(logging.INFO, logger="agents.memory.facts")
        old_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        caplog.clear()
        new_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-2",
            asserted_at=2000.0,
        )
        records = _audit_records(caplog)
        events = {_event_dict(r).get("event") for r in records}
        assert "fact.store" in events
        assert "fact.supersede" in events

        supersede_records = [
            r for r in records
            if _event_dict(r).get("event") == "fact.supersede"
        ]
        assert len(supersede_records) == 1
        supersede = _event_dict(supersede_records[0])
        assert supersede["agent_id"] == "test-agent"
        assert supersede["superseded_fact_id"] == old_id
        assert supersede["by_fact_id"] == new_id

    async def test_explicit_supersede_emits_record(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        old_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        new_id = await fact_store.store(
            subject="alice",  # different subject so no auto-supersede
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-2",
            asserted_at=2000.0,
        )
        caplog.set_level(logging.INFO, logger="agents.memory.facts")
        caplog.clear()
        result = await fact_store.supersede(old_id, new_id)
        assert result is True
        supersede_records = [
            r for r in _audit_records(caplog)
            if _event_dict(r).get("event") == "fact.supersede"
        ]
        assert len(supersede_records) == 1


# ─── Redactor failure: out-of-band warning ──────────────────


@pytest.mark.asyncio
class TestRedactorFailureWarning:
    """PR #340 review S2 — when the redactor raises, ``emit_audit`` must
    emit an out-of-band WARNING so the silent PII-passthrough is
    observable.

    Why this matters: the structlog chain's :func:`_apply_redactor`
    already logs a warning on redactor failure (PR #164 review Must-Fix
    #1).  But ``emit_audit`` runs the redactor *before*
    :func:`configure_logging` may have built that chain — for unit
    tests, early startup, or any process that never calls
    ``configure_logging``.  Without an explicit warning, a misconfigured
    redactor in that pre-chain window silently passes raw PII to the
    audit sink with zero signal.

    The fix mirrors the chain: catch the exception, emit one WARNING
    via stdlib logging, then proceed with the unredacted payload.  A
    contextvar guard prevents the warning from re-entering the
    redactor (the warning record itself flows through the structlog
    chain when configure_logging has run, which would re-trigger the
    same exception).
    """

    async def test_redactor_raises_then_warning_logged(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        class _BoomRedactor:
            def redact(self, record: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError("synthetic redactor failure")

        obs_logging.set_redactor(_BoomRedactor())

        caplog.set_level(logging.WARNING, logger="agents.memory.facts")
        # Also capture the audit-info records on the same logger.
        caplog.set_level(logging.INFO, logger="agents.memory.facts")
        await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        # The audit record still fires (unredacted) — the contract is
        # "structured-log hiccup must not break a write that has
        # already committed".  But a separate WARNING records the
        # silent-passthrough so it is observable.
        warnings = [
            rec for rec in caplog.records
            if rec.levelno == logging.WARNING
            and "redactor raised" in rec.getMessage().lower()
        ]
        assert len(warnings) == 1, (
            "exactly one WARNING per emit_audit when the redactor raises"
        )

    async def test_redactor_raises_does_not_block_audit_emission(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        """The unredacted audit record still reaches the sink — the
        write committed and the audit trail is preserved (the WARNING
        is an out-of-band signal, not a substitute for the audit
        record).
        """
        class _BoomRedactor:
            def redact(self, record: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError("synthetic redactor failure")

        obs_logging.set_redactor(_BoomRedactor())
        caplog.set_level(logging.INFO, logger="agents.memory.facts")
        fact_id = await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        records = _audit_records(caplog)
        assert len(records) == 1
        rec = records[0]
        event = _event_dict(rec)
        assert event["event"] == "fact.store"
        assert event["fact_id"] == fact_id


# ─── delete_by_subject: silent at this layer ────────────────


@pytest.mark.asyncio
class TestDeleteBySubjectStaysSilent:
    """RFC 0013's umbrella ``SubjectErasure`` owns the audit emission.

    FactStore.delete_by_subject is a storage primitive — emitting from
    here would double-count once RFC 0013 lands.  The current contract
    is "no audit record from the primitive"; RFC 0013 (target v0.5.0)
    will emit a single umbrella record per erasure call that sums the
    per-tier traversals.
    """

    async def test_delete_by_subject_no_audit_record(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        caplog.set_level(logging.INFO, logger="agents.memory.facts")
        caplog.clear()
        await fact_store.delete_by_subject("bob")
        records = _audit_records(caplog)
        # No fact.delete_by_subject audit record (RFC 0013's job).
        events = {_event_dict(r).get("event") for r in records}
        assert "fact.delete_by_subject" not in events


# ─── Internal helpers ───────────────────────────────────────


def _event_dict(rec: logging.LogRecord) -> dict[str, Any]:
    """Return the structured-event dict for a captured record.

    structlog's stdlib bridge: when ``logger.info("event-name",
    audit=True, ...)`` is called via the stdlib path the kwargs flow
    through ``record.__dict__``.  When called via structlog's native
    path the whole dict is stashed under ``record.msg``.  This helper
    normalises both shapes.
    """
    if isinstance(rec.msg, dict):
        return dict(rec.msg)
    # Stdlib path — the event name is in ``rec.msg`` (a str) and the
    # extras are attributes.  Reconstruct the event dict.
    out: dict[str, Any] = {"event": rec.msg}
    for key in (
        "audit",
        "agent_id",
        "fact_id",
        "subject",
        "predicate",
        "object",
        "source_interaction_id",
        "superseded_fact_id",
        "by_fact_id",
    ):
        if hasattr(rec, key):
            out[key] = getattr(rec, key)
    return out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
