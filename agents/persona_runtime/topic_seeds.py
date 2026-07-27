"""Topic-subject recall seeding (RFC 0026 topic-predicate amendment —
RFC 0049 Phase 1 PR 1).

The retrieval leg of the scenario-2 capture path: stored ``topic.*``
facts are only useful if a later turn that *mentions* the topic seeds
:meth:`FactStore.recall` for it.  Person seeds (``self`` + canonical
sender) come from ``facts_section._subject_seeds``; this module adds
the topic seeds by matching the store's known topic subjects against
the inbound stimulus text.

Deterministic by construction — no LLM in the recall path (the
extractor's LLM proposes subjects at *write* time; the read side is a
bounded string match).  That keeps the seeding surface out of the
prompt-injection blast radius: a hostile stimulus can at most cause
recall of facts the store already holds for this agent, in this
session scope, and every recalled row still passes the RFC 0037 §D
injection gate downstream.

Bounds (amendment §Security):

* the store enumeration is capped (``TOPIC_SUBJECT_SCAN_LIMIT``) so
  per-event matching cost cannot scale with total store size;
* at most ``TOPIC_SEED_LIMIT`` topic seeds join the person seeds, so
  the per-seed recall fan-out and the per-subject header overage in
  ``render_facts_section`` stay bounded;
* matching is word-boundary on the canonical fold, so ``atlas`` does
  not fire inside ``atlases`` (over-seeding burns budget, not safety —
  but bounded is bounded).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.facts import FactStore

logger = logging.getLogger(__name__)

__all__ = [
    "TOPIC_SEED_LIMIT",
    "TOPIC_SUBJECT_SCAN_LIMIT",
    "match_topic_subjects",
    "topic_subject_seeds",
]


# Maximum topic seeds appended to the person seed list per event.  Each
# seed costs one ``FactStore.recall`` round-trip and one potential
# per-subject header in the rendered section (~5 tokens of soft-slice
# overage — see the ``render_facts_section`` overage note).  Three
# keeps a multi-topic stimulus useful without letting a keyword-stuffed
# message fan out unboundedly.
TOPIC_SEED_LIMIT: int = 3

# Bound on the distinct-subject enumeration pulled from the store per
# event.  Matching cost is O(scan × stimulus length); 200 recent topics
# is far beyond any realistic working set while keeping the worst case
# small.  Topics that age past the window simply stop seeding — the
# most-recently-asserted-first order means the live working set wins.
TOPIC_SUBJECT_SCAN_LIMIT: int = 200


def match_topic_subjects(
    stimulus: str,
    subjects: Iterable[str],
    *,
    limit: int = TOPIC_SEED_LIMIT,
    exclude: Collection[str] = (),
) -> list[str]:
    """Return the subjects mentioned in ``stimulus``, capped at ``limit``.

    ``subjects`` are canonical (store-produced) forms; the stimulus is
    folded the same way (casefold + whitespace collapse) so mentions
    match regardless of casing or spacing.  Word-boundary lookarounds
    (``(?<!\\w) … (?!\\w)``) prevent substring bleed.  ``exclude``
    drops subjects already seeded by the person path (self / sender)
    so no subject is recalled twice.  Order follows ``subjects`` —
    the store's most-recently-asserted-first enumeration — for
    deterministic, golden-trace-portable output.
    """
    if not stimulus or not stimulus.strip():
        return []
    folded = " ".join(stimulus.casefold().split())
    matched: list[str] = []
    for subject in subjects:
        if len(matched) >= limit:
            break
        if subject in exclude or subject in matched:
            continue
        if re.search(rf"(?<!\w){re.escape(subject)}(?!\w)", folded):
            matched.append(subject)
    return matched


async def topic_subject_seeds(
    fact_store: FactStore | None,
    stimulus: str | None,
    *,
    exclude: Collection[str],
    limit: int = TOPIC_SEED_LIMIT,
) -> list[str]:
    """Derive topic seeds for one event, fail-open to ``[]``.

    Mirrors the facts tier's log-and-continue idiom: a backend failure
    degrades to person-only seeding rather than blocking the event.
    Callers gate on the person-seed short-circuit first (sender-less
    events never reach here), so the empty-context cost guard for TICK
    events is preserved one layer up.
    """
    if fact_store is None or not stimulus or not stimulus.strip():
        return []
    try:
        subjects = await fact_store.topic_subjects(
            limit=TOPIC_SUBJECT_SCAN_LIMIT,
        )
    except Exception:
        logger.warning(
            "Agent %s: topic-subject enumeration failed; "
            "person-only seeding",
            fact_store.agent_id, exc_info=True,
        )
        return []
    return match_topic_subjects(
        stimulus, subjects, limit=limit, exclude=exclude,
    )
