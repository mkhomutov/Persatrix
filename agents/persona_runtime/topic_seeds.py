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
* a topic seed recalls ONLY topic rows (the caller passes
  ``TOPIC_PREDICATES``), so an induced ``topic.*`` tuple about a
  person cannot turn that person's name into a general fact-read key;
* subjects below ``TOPIC_SEED_MIN_CHARS`` or in the function-word set
  never seed — see ``_seed_eligible``;
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

# Seeding eligibility (amendment §Security).  A subject this short, or
# one that is nothing but function words, matches almost every message
# — and because the scan is most-recently-asserted-first, whoever wrote
# the newest topic row gets first claim on the seed slots.  Together
# that let one induced tuple named ``the`` occupy every slot on every
# subsequent turn.  Eligibility is a READ-side rule: such rows still
# store (they may be legitimate), they just do not seed.
TOPIC_SEED_MIN_CHARS: int = 3

# Deliberately tiny — the highest-frequency English function words that
# survive the length floor.  Not a language model: the floor does the
# heavy lifting, this closes the short-but-ubiquitous tail.
_STOPWORD_SUBJECTS: frozenset[str] = frozenset({
    "the", "and", "for", "you", "your", "our", "this", "that", "with",
    "from", "have", "has", "was", "were", "are", "not", "but", "all",
    "any", "can", "will", "what", "when", "who", "how", "why", "yes",
    "one", "out", "get", "got", "new", "now", "day", "way", "let",
})


def _seed_eligible(subject: str) -> bool:
    """Whether a canonical subject may act as a recall seed."""
    return (
        len(subject) >= TOPIC_SEED_MIN_CHARS
        and subject not in _STOPWORD_SUBJECTS
    )


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
    if not isinstance(stimulus, str) or not stimulus.strip():
        return []
    folded = " ".join(stimulus.casefold().split())
    matched: list[str] = []
    for subject in subjects:
        if len(matched) >= limit:
            break
        if subject in exclude or subject in matched:
            continue
        if not _seed_eligible(subject):
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
    sessions: list[str] | str | None = None,
) -> list[str]:
    """Derive topic seeds for one event, fail-open to ``[]``.

    Mirrors the facts tier's log-and-continue idiom: a backend failure
    degrades to person-only seeding rather than blocking the event.
    Callers gate on the person-seed short-circuit first (sender-less
    events never reach here), so the empty-context cost guard for TICK
    events is preserved one layer up.

    ``sessions`` forwards to :meth:`FactStore.topic_subjects` — the
    live path leaves it ``None`` (§D default scope); the L2 cross-room
    SHADOW pass (:mod:`.facts_shadow`, RFC 0049 PR 2) widens it so a
    topic taught in another room can seed the shadow read.
    """
    if (
        fact_store is None
        or not isinstance(stimulus, str)
        or not stimulus.strip()
    ):
        # ``isinstance`` and not truthiness alone: a bridge may hand a
        # non-str ``content`` through (``channel_ingest`` deliberately
        # passes malformed wire values unchanged), and this runs
        # OUTSIDE the tier's try-block — an ``AttributeError`` here
        # would escape ``_inject_memory_context``'s "never fail the
        # event" contract and fail the whole turn.
        return []
    try:
        subjects = await fact_store.topic_subjects(
            limit=TOPIC_SUBJECT_SCAN_LIMIT,
            sessions=sessions,
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
