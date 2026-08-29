"""``insert_fact`` — the RFC 0026 fact write path.

Extracted from :mod:`agents.memory.facts` (ISSUE-0131, v0.3.15) — the
sixth ``_facts_*`` split from that module and the same idiom as
:mod:`._facts_erasure` / :mod:`._facts_topics`: the SQL body and its
rationale live here, ``FactStore`` keeps a thin delegating method that
resolves the ambient session / principal / epoch and hands them down.

The immediate cause was the 500-line cap: ``facts.py`` sat at exactly
500, so the migration-18 ``speaker_id`` parameter could not be added
with its own documentation — or, in fact, at all.  The write path was
the right thing to move regardless, being the largest method and the
one carrying the most rationale per line.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING

from ..observability.metrics import try_get_instruments
from ._facts_audit import emit_audit as _emit_audit
from ._facts_supersede import apply_supersession as _apply_supersession
from ._migration_protection import PROTECTION_LEVEL_DEFAULT
from .fact_predicates import canonicalize_subject, validate_object, validate_subject

if TYPE_CHECKING:
    from collections.abc import Callable

    import aiosqlite

__all__ = ["insert_fact"]


async def insert_fact(
    db: aiosqlite.Connection,
    agent_id: str,
    *,
    subject: str,
    predicate: str,
    object: str,  # noqa: A002 — RFC 0026 §A names this field literally
    source_interaction_id: str | None,
    asserted_at: float,
    certainty: float,
    session_id: str,
    principal_id: str,
    epoch_id: str,
    predicate_validator: Callable[[str], None],
    protection_level: str = PROTECTION_LEVEL_DEFAULT,
    source_channel_id: str | None = None,
    speaker_id: str | None = None,
) -> str:
    """Validate, INSERT and supersede one fact tuple; return its id.

    The caller (:meth:`agents.memory.facts.FactStore.store`) resolves the
    ambient ``session_id`` / ``principal_id`` / ``epoch_id`` and passes
    them in, matching :func:`._facts_erasure.delete_by_subject` — the
    tier owns "which tenant am I", this helper owns "what does a write
    do".

    ``speaker_id`` (ISSUE-0131 — migration 18) is WHO said the content
    this fact was derived from: the speaker half of the source
    interaction's ``(principal, speaker, scope)`` key, projected onto the
    row.  ``None`` for a speakerless source (a tick, a single-turn scope,
    a direct operator or test write) and for every pre-v18 row, whose
    speaker is genuinely unknowable — the aggregate it came from spanned
    every speaker in the room, which is the defect the key exists to fix,
    and recovering it after the fact would need exactly the model-elected
    attribution the Phase 0b scope lock forbids.  Not to be confused with
    ``subject``: the subject is who a fact is ABOUT and the speaker is
    who said it, and a counterparty fact differs in the two.  Sound only
    because a record is single-speaker by construction — the RFC 0020 §G
    room-close turn, the sole exception, is dropped from the extractor's
    input upstream (``summarize_close._interaction_to_entries``), so no
    tuple reaching here can have come from another speaker's words.

    ``protection_level`` / ``source_channel_id`` (RFC 0037 §C — v16,
    PR 3) persist VERBATIM — rule-(a) normalization is owned by the
    persona-side stamp site (the close-consolidation extractor via
    ``normalize_for_stamp``), which memory must not import.  Omitted
    (direct/test/operator callers) → the ``internal`` default, so no path
    writes a fact without a protection level; a mislabeled row fails
    closed at read time (§A rule (c)).  Supersession restamps by
    construction — a superseding assertion is a NEW row stamped from its
    own source (§C item 3) — and reinforcement never touches the column.

    Enforces RFC 0026 §F **symmetric latest-asserted-wins**: only one
    live row per ``(agent_id, subject, predicate)`` survives, and the row
    with the greatest ``asserted_at`` wins.  Out-of-order and
    equal-timestamp writes resolve deterministically — see
    :mod:`agents.memory._facts_supersede` for the chain rule (including
    why equal timestamps are now REACHABLE in production) and
    :class:`tests.unit.python.test_fact_store_supersede.TestSymmetricLatestAssertedWins`
    for the pinned cases.

    Predicate validation runs through the injected validator (PR 2 wires
    the enumerated allowlist).

    Subject canonicalisation (PR 5c — PR #341 review L-2)
    -----------------------------------------------------
    The PR 2 extractor canonicalises before calling here, but direct
    callers (fixtures, operator-seeded facts per OQ #9, the future
    RFC 0013 erasure backfill) bypass that discipline and would write
    rows the ``_subject_seeds → canonicalize_subject`` recall path can
    never reach — defeating the MT-MEMORY-005 invariant.  The storage
    primitive is authoritative instead: every persisted row carries the
    canonical subject.
    """
    # Cheap value checks first — surfacing "subject must not be empty" or
    # a certainty-range error before the (potentially PR 2
    # allowlist-backed) predicate validator means a caller that violates
    # two preconditions sees the more obviously-wrong one first.
    if not subject or not subject.strip():
        raise ValueError("subject must not be empty")
    if not 0.0 <= certainty <= 1.0:
        raise ValueError(
            f"certainty must be in [0.0, 1.0], got {certainty}",
        )
    predicate_validator(predicate)
    # Topic-amendment blast-radius bound: object length + RFC 0009
    # delimiter escape, enforced at the storage boundary so every write
    # path (extractor, operator-seeded, fixtures) is covered.
    validate_object(object)
    # Canonicalise after the empty-check so the ValueError text stays
    # familiar; ``canonicalize_subject`` is idempotent so the production
    # write path (extractor pre-canonicalises) is unaffected.  The
    # blast-radius bound runs on the canonical form (write boundary only
    # — see ``validate_subject``).
    subject = canonicalize_subject(subject)
    validate_subject(subject)

    fact_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO facts
            (fact_id, agent_id, subject, predicate, object,
             certainty, source_interaction_id, asserted_at,
             last_recalled_at, superseded_by, session_id, principal_id,
             epoch_id, protection_level, source_channel_id, speaker_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            fact_id, agent_id, subject, predicate, object, certainty,
            source_interaction_id, asserted_at, session_id, principal_id,
            epoch_id, protection_level, source_channel_id, speaker_id,
        ),
    )
    result = await _apply_supersession(
        db, agent_id=agent_id, subject=subject, predicate=predicate,
        asserted_at=asserted_at, new_fact_id=fact_id,
        session_id=session_id, principal_id=principal_id, epoch_id=epoch_id,
    )
    await db.commit()

    # Telemetry lives outside the persistence path — a metrics-backend
    # failure must not surface as a write failure (the row is already
    # persisted).  Mirrors ``EpisodicMemory.store_episode``'s
    # ``sessions.writes`` pattern.
    with contextlib.suppress(Exception):
        inst = try_get_instruments()
        if inst is not None:
            inst.facts_stored.add(1, attributes={"agent.id": agent_id})
            n_superseded = len(result.superseded_older_ids) + (
                1 if result.self_superseded_by else 0
            )
            if n_superseded:
                inst.facts_superseded.add(
                    n_superseded, attributes={"agent.id": agent_id},
                )

    # RFC 0026 §G audit emission — after commit so the log cannot record
    # a write that did not happen.
    _emit_audit(
        "fact.store", agent_id=agent_id, fact_id=fact_id, subject=subject,
        predicate=predicate, object=object,
        source_interaction_id=source_interaction_id,
    )
    for older_id in result.superseded_older_ids:
        _emit_audit(
            "fact.supersede", agent_id=agent_id,
            superseded_fact_id=older_id, by_fact_id=fact_id,
        )
    if result.self_superseded_by is not None:
        _emit_audit(
            "fact.supersede", agent_id=agent_id, superseded_fact_id=fact_id,
            by_fact_id=result.self_superseded_by,
        )
    return fact_id
