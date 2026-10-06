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
recall of facts the store already holds for this agent in its epoch
and principal — from any room when ``memory.facts.cross_room`` is
``live``, from the current room otherwise — and every recalled row
still passes the RFC 0037 §D injection gate downstream.

The room's own subject (ISSUE-0180).  A message rarely repeats a stored
subject word for word — "please critique this plan" names nothing — so
a fact about what a room is for was stored and never recalled there.
The room's description is therefore a second text the stored subjects
are matched against, by the same rule: a subject it names seeds on a
turn in that room whatever the message says.  The description's
first-named subject seeds first, and a subject it names is read from
both ends, the newest fact and the oldest in turn: it is recalled turn
after turn while every room that files under it adds to its list, so
read newest first, the facts a room was first told would be the first
a later room pushes out (:func:`topic_recall_seeds`).

That seed is only as good as the match, and nothing above DEBUG says
when it does not fire: the description and the stored subject differ by
a word, an article or a spelling; the subject is not among the
``TOPIC_SUBJECT_SCAN_LIMIT`` most recently asserted; its name is too
short or a function word (``_seed_eligible``); the turn's roster fetch
failed, since the description comes with the roster; or the subject was
taught in another room and ``memory.facts.cross_room`` is ``shadow`` or
``off``.

Bounds (amendment §Security):

* the store enumeration is capped (``TOPIC_SUBJECT_SCAN_LIMIT``) so
  per-event matching cost cannot scale with total store size;
* at most ``TOPIC_SEED_LIMIT`` topic seeds join the person seeds, so
  the per-seed recall fan-out and the per-subject header overage in
  ``render_facts_section`` stay bounded.  The room's description adds
  no slot: its subjects share that cap, and beside a subject the
  message names they take at most ``ROOM_SEED_LIMIT`` of it.  A slot is
  not a line, though: the facts section spends its tokens in seed order
  with no share per subject, so on such a turn the room's first subject
  is read only ``ROOM_SEED_ROWS_BESIDE_MESSAGE`` rows deep;
* a topic seed recalls ONLY topic rows (every seed this module returns
  carries ``TOPIC_PREDICATES`` as its filter), so an induced ``topic.*``
  tuple about a person cannot turn that person's name into a general
  fact-read key;
* subjects below ``TOPIC_SEED_MIN_CHARS`` or in the function-word set
  never seed, from either text — see ``_seed_eligible``;
* matching is word-boundary on the canonical fold, so ``atlas`` does
  not fire inside ``atlases`` (over-seeding burns budget, not safety —
  but bounded is bounded).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING

from ..memory.fact_predicates import TOPIC_PREDICATES

if TYPE_CHECKING:
    from ..memory.facts import FactStore, RecallOrder

logger = logging.getLogger(__name__)

__all__ = [
    "ROOM_SEED_LIMIT",
    "ROOM_SEED_ROWS_BESIDE_MESSAGE",
    "TOPIC_SEED_LIMIT",
    "TOPIC_SUBJECT_SCAN_LIMIT",
    "match_topic_subjects",
    "topic_recall_seeds",
]

# A topic seed as the recall loop takes it: the subject, the predicates
# its read is confined to, the order its facts come back in, and a row
# cap of its own (``None`` for the caller's).
_TopicSeed = tuple[str, frozenset[str], "RecallOrder", int | None]


# Maximum topic seeds appended to the person seed list per event.  Each
# seed costs one ``FactStore.recall`` round-trip and one potential
# per-subject header in the rendered section (~5 tokens of soft-slice
# overage — see the ``render_facts_section`` overage note).  Three
# keeps a multi-topic stimulus useful without letting a keyword-stuffed
# message fan out unboundedly.
TOPIC_SEED_LIMIT: int = 3

# How many of those slots the room's description may fill beside a
# subject the message names (ISSUE-0180).  One fewer than the cap, so a
# description that names many stored subjects cannot stop the message's
# own subject from seeding in that room.  Most rooms name one.  A turn
# whose message names nothing leaves the description every slot.
ROOM_SEED_LIMIT: int = TOPIC_SEED_LIMIT - 1

# How deep the room's first subject is read on a turn whose message
# names a subject of its own: three facts from each end.
# ``render_facts_section`` spends the tier's tokens in seed order and
# gives no subject a share, so read to the full row cap, a dozen facts
# under the room's subject would leave the message's subject, which is
# printed after it, no line at all.  Six lines are about half of what
# the default 200-token slice prints.
ROOM_SEED_ROWS_BESIDE_MESSAGE: int = 6

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


def _mention(folded: str, subject: str) -> int | None:
    """Where ``folded`` first names ``subject`` as whole words, or ``None``."""
    found = re.search(rf"(?<!\w){re.escape(subject)}(?!\w)", folded)
    return found.start() if found else None


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
        if _mention(folded, subject) is not None:
            matched.append(subject)
    return matched


def _named_in_order(
    text: str, subjects: Iterable[str], *, exclude: Collection[str],
) -> list[str]:
    """Every subject ``text`` names, in the order it first names them.

    The same match and the same eligibility rule as
    :func:`match_topic_subjects`, with a different order and no cap: a
    room's description is short, fixed text whose first words say what
    the room is for, so its own order ranks its subjects better than
    which of them the store heard of last.  Ranked by recency, one fact
    filed under a part the description goes on to mention would take the
    seed from the organisation it names first.  Where two subjects start
    at the same word, the longer, more exact name comes first.
    """
    folded = " ".join(text.casefold().split())
    found: dict[str, int] = {}
    for subject in subjects:
        if subject in exclude or subject in found or not _seed_eligible(subject):
            continue
        at = _mention(folded, subject)
        if at is not None:
            found[subject] = at
    return sorted(found, key=lambda subject: (found[subject], -len(subject)))


def _text(value: object) -> str:
    """``value`` if it is text worth matching against, else ``""``.

    ``isinstance`` and not truthiness alone: a bridge may hand a non-str
    ``content`` through (``channel_ingest`` deliberately passes malformed
    wire values unchanged), a room's description comes off a REST body,
    and this runs OUTSIDE the tier's try-block — an ``AttributeError``
    here would escape ``_inject_memory_context``'s "never fail the
    event" contract and fail the whole turn.
    """
    return value if isinstance(value, str) and value.strip() else ""


async def topic_recall_seeds(
    fact_store: FactStore | None,
    stimulus: str | None,
    *,
    exclude: Collection[str],
    room_text: str | None = None,
    sessions: list[str] | str | None = None,
) -> list[_TopicSeed]:
    """Derive one event's topic seeds, each with how to read it.

    A seed is ``(subject, predicates, order, rows)``.  Every seed
    carries ``TOPIC_PREDICATES``, so no caller can read a topic seed
    wider than topic rows; ``rows`` is a row cap of the seed's own, or
    ``None`` for the caller's.

    Two texts name subjects.  ``room_text`` is the acting room's
    description, and the stored subjects it names are taken in the order
    it names them (:func:`_named_in_order`): the first always seeds, and
    seeds first.  ``stimulus`` is the turn's memory query: the subjects
    it names come next, most recently asserted first as before, and
    beside them the description keeps at most ``ROOM_SEED_LIMIT`` slots.
    The description's other subjects come last, and take any slot the
    message leaves.  A subject both name seeds once.  With no
    ``room_text`` the result is the shipped message-only seeding.

    How a subject is read depends on the two texts alone, never on the
    slot it won.  One only the message names is read ``"newest"``: it
    answers what was just said, where the latest facts matter most.  One
    the description names is read ``"both_ends"``: it is recalled turn
    after turn while every room that files under it adds to its list, so
    the facts tier's row cap and token budget would otherwise cut what
    the room was first told (:meth:`FactStore.recall`, ``order``).  On a
    turn whose message names a subject of its own, the room's first
    subject is read only ``ROOM_SEED_ROWS_BESIDE_MESSAGE`` rows deep, so
    that the message's subject, printed after it, still gets its lines.

    Fail-open to ``[]``, mirroring the facts tier's log-and-continue
    idiom: a backend failure degrades to person-only seeding rather
    than blocking the event.  Callers gate on the person-seed
    short-circuit first (sender-less events never reach here), so the
    empty-context cost guard for TICK events is preserved one layer up;
    with neither text the store is not read at all.

    ``sessions`` is forwarded unchanged to
    :meth:`FactStore.topic_subjects`: ``None`` keeps the §D default
    scope (the current room), and ``"*"`` lets a topic taught in another
    room seed the recall.  The callers choose.  The prompt path
    (``recall_facts_for_event``) passes ``"*"`` when
    ``memory.facts.cross_room`` is ``live`` and ``None`` under
    ``shadow`` or ``off``; the L2 cross-room shadow pass
    (:mod:`.facts_shadow`) runs only under ``shadow`` and passes
    ``"*"`` for its own read.  The mode's default is
    :data:`~agents.persona_runtime.cross_room.DEFAULT_FACTS_CROSS_ROOM`.
    """
    room, message = _text(room_text), _text(stimulus)
    if fact_store is None or not (room or message):
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
    named = _named_in_order(room, subjects, exclude=exclude)
    if room and not named:
        logger.debug(
            "Agent %s: the channel's description names no stored topic "
            "subject; nothing seeds from it",
            fact_store.agent_id,
        )
    kept = named[:ROOM_SEED_LIMIT]
    message_seeds = match_topic_subjects(
        message, subjects, limit=TOPIC_SEED_LIMIT - len(kept),
        exclude={*exclude, *kept},
    )
    room_seeds = named[:TOPIC_SEED_LIMIT - len(message_seeds)]
    described = set(named)

    def seed(subject: str, rows: int | None = None) -> _TopicSeed:
        order: RecallOrder = "both_ends" if subject in described else "newest"
        return subject, TOPIC_PREDICATES, order, rows

    beside = ROOM_SEED_ROWS_BESIDE_MESSAGE if message_seeds else None
    return [
        *(seed(subject, beside) for subject in room_seeds[:1]),
        *(seed(subject) for subject in message_seeds),
        *(seed(subject) for subject in room_seeds[1:]),
    ]
