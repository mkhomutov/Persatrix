"""Shared reader for the facts tier's captured audit records.

Extracted so ``test_fact_store_audit.py`` and
``test_facts_written_together.py`` read a captured ``fact.*`` audit
record through ONE helper.  The record reaches stdlib logging in two
shapes, and two copies of that handling would drift the day the shape
changes.

The pattern mirrors ``_close_path_test_helpers.py`` — a private module
sibling to the test files, imported by name.
"""

from __future__ import annotations

import logging
from typing import Any


def event_dict(rec: logging.LogRecord) -> dict[str, Any]:
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
