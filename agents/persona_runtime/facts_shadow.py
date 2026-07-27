"""L2 cross-room fact recall — SHADOW mode (RFC 0049 Phase 1 PR 2).

The `RFC 0031 fact-scope amendment
<../../docs/rfcs/0031-amendment-fact-scope-by-consolidation-level.md>`_
drops the room (session) wall from the L2 facts tier: a consolidated
fact is cross-room regardless of subject, with visibility owned by the
RFC 0037 protection level.  This module is the amendment's **shadow**
implementation: each sender-bearing turn computes what the widened
recall *would* have injected — the cross-room delta, passed through the
same §D gate as the live tiers — and records it as a structured log
trace, WITHOUT any of it entering the live prompt.  The RFC 0044
harness captures these traces (``evaluators/persona_driver.py``); the
PR 4 measurement gate reads them to decide the shadow → live flip.

Scope invariants (what the widening does and does not touch):

* Only the **session** axis widens (``sessions="*"`` on the shadow
  read).  ``epoch`` and ``principal`` remain strict-equality SQL
  clauses on every branch of :meth:`FactStore.recall` — cross-room is
  never cross-run or cross-tenant.
* Every shadow candidate passes the RFC 0037 §D gate at the turn's
  acting classification before it is recorded as "would inject" — a
  ``restricted``-stamped fact acting-``public`` is a *withheld* count,
  never a candidate.
* Topic seeds stay predicate-scoped (``TOPIC_PREDICATES``) exactly
  like the live path — the widened read must not weaken the PR 1
  subject-reachability bound.

F-3 posture (the ``sessions="*"`` security pin): the RFC 0031 §D pin
says the persona-runtime *prompt-context* path never reaches ``"*"``.
This module deliberately reads all-sessions but is NOT a prompt-context
path — nothing here touches :class:`WorkingMemory`, the RFC 0017
budget, the §G manifest, or the reinforcement write; the sole output is
a log record.  ``test_facts_shadow.py`` pins the no-prompt-leak
property; ``test_session_recall_default_path.py`` documents the
carve-out.

Log-egress bound: the trace names each candidate's ``fact_id`` /
``subject`` / ``predicate`` / ``protection_level`` / provenance
(``session_id``, ``source_channel_id``) but never the fact **object**
text — the process log is its own egress surface, and dumping
restricted objects into it would undo at the log what the §D gate
enforces at the prompt.  The PR 4 measurement joins ``fact_id`` back
against the store when it needs content.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from ..memory._session_filter import SESSIONS_ALL
from ..memory.fact_predicates import TOPIC_PREDICATES
from .facts_section import FACTS_RECALL_LIMIT, _subject_seeds
from .injection_gate import TurnInjectionGate, acting_classification_for_event
from .topic_seeds import topic_subject_seeds

if TYPE_CHECKING:
    from collections.abc import Collection

    from ..memory.facts import Fact, FactStore
    from ..persona_types import AgentEvent

logger = logging.getLogger(__name__)

__all__ = [
    "CROSS_ROOM_MODES",
    "CROSS_ROOM_OFF",
    "CROSS_ROOM_SHADOW",
    "DEFAULT_FACTS_CROSS_ROOM",
    "SHADOW_LOGGER_NAME",
    "SHADOW_TRACE_ATTR",
    "emit_facts_shadow",
    "resolve_facts_cross_room",
]

#: The logger name the RFC 0044 harness attaches its capture handler to.
SHADOW_LOGGER_NAME: Final[str] = __name__

#: Attribute on each shadow log record carrying the structured trace
#: payload (set via ``logger.info(..., extra={SHADOW_TRACE_ATTR: ...})``).
SHADOW_TRACE_ATTR: Final[str] = "facts_shadow"

#: ``memory.facts.cross_room`` modes.  PR 4 adds ``"live"`` behind the
#: golden-trace measurement gate; until then the schema and this
#: resolver both reject it, so a config cannot flip early.
CROSS_ROOM_OFF: Final[str] = "off"
CROSS_ROOM_SHADOW: Final[str] = "shadow"
CROSS_ROOM_MODES: Final[frozenset[str]] = frozenset(
    {CROSS_ROOM_OFF, CROSS_ROOM_SHADOW},
)

#: Shadow-first is the shipped v0.3.12 posture (master plan §Scope): the
#: widening runs everywhere so traces accumulate for the PR 4 verdict.
DEFAULT_FACTS_CROSS_ROOM: Final[str] = CROSS_ROOM_SHADOW


def resolve_facts_cross_room(config: dict) -> str:
    """Resolve ``memory.facts.cross_room`` from a persona config.

    Absent / ``None`` → :data:`DEFAULT_FACTS_CROSS_ROOM` (the
    ``resolve_facts_config`` null-collapse precedent).  An unknown
    string raises ``ValueError`` at agent construction — deliberately
    louder than a silent floor, because the value a future config is
    most likely to carry early is ``"live"`` (the PR 4 flip), and
    silently degrading a requested live widening to shadow would
    misreport what the deployment is doing.  Production configs are
    already schema-gated to the enum; this is the programmatic-path
    twin of that gate.
    """
    facts_cfg = (config.get("memory") or {}).get("facts") or {}
    raw = facts_cfg.get("cross_room")
    if raw is None:
        return DEFAULT_FACTS_CROSS_ROOM
    # ``isinstance`` narrows the untyped config value for mypy AND folds
    # non-str garbage into the same loud rejection as an unknown mode.
    if not isinstance(raw, str) or raw not in CROSS_ROOM_MODES:
        raise ValueError(
            f"memory.facts.cross_room must be one of "
            f"{sorted(CROSS_ROOM_MODES)}, got {raw!r}",
        )
    return raw


async def _widened_candidates(
    fact_store: FactStore,
    event: AgentEvent,
    *,
    stimulus: str | None,
    live_fact_ids: Collection[str],
) -> list[Fact]:
    """The cross-room delta: widened-recall rows not reachable live.

    Mirrors ``recall_facts_for_event``'s seed derivation — person seeds
    (every predicate class) plus topic seeds (``TOPIC_PREDICATES``
    only) — with both the topic enumeration and the per-seed recall
    widened to ``sessions="*"``.  Rows whose ``fact_id`` the live
    (room-scoped) recall already returned are dropped: the delta is the
    widening's *contribution*, and a same-room-but-gate-withheld row
    must not be re-reported as cross-room.

    Per-seed failures log-and-continue, the live path's idiom: on a
    partially-failing backend the live prompt still gets the surviving
    seeds' facts, so a whole-turn abort here would skew the PR 4
    shadow-vs-live measurement against that partial recall.
    """
    person_seeds = _subject_seeds(event)
    if not person_seeds:
        return []
    seeds: list[tuple[str, Collection[str] | None]] = [
        (subject, None) for subject in person_seeds
    ]
    seeds += [
        (subject, TOPIC_PREDICATES)
        for subject in await topic_subject_seeds(
            fact_store, stimulus, exclude=set(person_seeds),
            sessions=SESSIONS_ALL,
        )
    ]
    delta: list[Fact] = []
    seen: set[str] = set(live_fact_ids)
    for subject, predicates in seeds:
        try:
            rows = await fact_store.recall(
                subject=subject, limit=FACTS_RECALL_LIMIT,
                predicates=predicates, sessions=SESSIONS_ALL,
            )
        except Exception:
            logger.warning(
                "Agent %s: shadow facts recall for subject=%r failed; "
                "skipping seed",
                fact_store.agent_id, subject, exc_info=True,
            )
            continue
        for fact in rows:
            if fact.fact_id in seen:
                continue
            seen.add(fact.fact_id)
            delta.append(fact)
    return delta


async def emit_facts_shadow(
    fact_store: FactStore | None,
    event: AgentEvent,
    *,
    stimulus: str | None,
    live_fact_ids: Collection[str],
    agent_id: str,
    mode: str = DEFAULT_FACTS_CROSS_ROOM,
) -> None:
    """Compute and record the turn's L2 cross-room shadow trace.

    One structured INFO record per turn with a non-empty cross-room
    delta; quiet turns (no delta, sender-less events, ``mode="off"``,
    missing store) emit nothing, so single-room deployments see zero
    log volume.  Runs OUTSIDE the live tier pipeline and never raises —
    a shadow failure degrades to a WARNING, honouring
    ``_inject_memory_context``'s "never fail the event" contract.

    The §D gate application uses a dedicated :class:`TurnInjectionGate`
    instance whose aggregated log emission is intentionally **not**
    fired: the live gate's WARNING describes entries withheld from a
    real prompt, and a shadow row never had a prompt to be withheld
    from.  Withhold counts ride the shadow trace instead, split by
    cause — ``withheld`` (clean above-rank) vs ``unknown_label`` (rule
    (c): the stored label failed to parse) — so the PR 4 measurement
    can tell "gate working" from "labels corrupt".
    """
    if mode != CROSS_ROOM_SHADOW or fact_store is None:
        return
    try:
        delta = await _widened_candidates(
            fact_store, event, stimulus=stimulus,
            live_fact_ids=live_fact_ids,
        )
        if not delta:
            return
        acting = acting_classification_for_event(event)
        gate = TurnInjectionGate(acting=acting, agent_id=agent_id)
        candidates = gate.filter_entries("facts", delta, id_attr="fact_id")
        trace = {
            "agent_id": agent_id,
            "acting": acting,
            "candidates": [
                {
                    "fact_id": f.fact_id,
                    "subject": f.subject,
                    "predicate": f.predicate,
                    "protection_level": f.protection_level,
                    "session_id": f.session_id,
                    "source_channel_id": f.source_channel_id,
                }
                for f in candidates
            ],
            "withheld": gate.withheld_count,
            "unknown_label": gate.unknown_label_count,
        }
        logger.info(
            "Agent %s: L2 cross-room shadow — %d fact(s) would inject "
            "at acting=%r (%d withheld above rank, %d unknown-label); "
            "live prompt unchanged",
            agent_id, len(candidates), acting,
            trace["withheld"], trace["unknown_label"],
            extra={SHADOW_TRACE_ATTR: trace},
        )
    except Exception:
        logger.warning(
            "Agent %s: L2 cross-room shadow pass failed; skipping",
            agent_id, exc_info=True,
        )
