"""Relationship memory data types and constants.

Pure data definitions with no database dependency — separated from
``relationship.py`` for clean import boundaries and file-size hygiene.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Maximum per-call trust delta — prevents single interactions from
# swinging trust dramatically.
_MAX_TRUST_DELTA = 0.2

# RFC 0031 amendment (F-7 Option D, ISSUE-0093): identity fields whose
# value is a *list* and which union across turns rather than being
# overwritten.  Everything else is scalar last-writer-wins.
_IDENTITY_LIST_KEYS = ("prefs",)

# Default trust score for unknown agent pairs.
_DEFAULT_TRUST = 0.5

# Maximum number of recent interactions returned by get_relationship_summary().
_MAX_RECENT_INTERACTIONS = 10


def merge_identity(
    existing: dict[str, Any], incoming: dict[str, Any],
) -> dict[str, Any]:
    """Shallow-merge ``incoming`` person-identity fields into ``existing``.

    RFC 0031 amendment (F-7 Option D, ISSUE-0093) decision D-2 — identity
    is a small structured object (``{name, role, prefs: [...]}``) with
    deterministic supersede semantics:

    * **scalar keys** (``name`` / ``role`` / …) are *last-writer-wins* — a
      new value replaces the old one,
    * **list keys** (:data:`_IDENTITY_LIST_KEYS`, currently ``prefs``)
      *union* — new entries append, order-preserving, de-duplicated, so a
      preference learned in a later turn accumulates rather than clobbers,
    * an **absent** incoming value — ``None`` *or* an empty string ``""`` —
      is **skipped** so a partial update (e.g. learning a role without
      re-stating the name) or a failed extraction cannot null an existing
      field.  The empty string is treated identically to ``None`` because an
      upstream extractor that emits ``""`` for a field it could not resolve
      must be just as non-destructive as one that emits ``None`` — scalar
      overwrite would otherwise wipe a good value (PR #553 deep-review #2).
      The same rule drops empty / ``None`` *items* from a list-key union, so
      a failed per-item extraction adds nothing rather than a blank pref.

    Pure: neither argument is mutated; a fresh dict is returned.  Lives
    here (no DB dependency) so the merge rule is unit-testable in isolation
    and reused by the write path in
    :func:`agents.memory.relationship_mutations.upsert_identity`.
    """
    merged = dict(existing)
    for key, value in incoming.items():
        if _is_absent(value):
            continue
        if key in _IDENTITY_LIST_KEYS:
            current = merged.get(key, [])
            # Defensive: a non-list existing value (legacy / hand-written
            # JSON) is a single element, not an iterable to splat — ``list``
            # of a ``str`` would explode it into characters.
            base = list(current) if isinstance(current, list) else [current]
            items = value if isinstance(value, list) else [value]
            for item in items:
                if not _is_absent(item) and item not in base:
                    base.append(item)
            merged[key] = base
        else:
            merged[key] = value
    return merged


def _is_absent(value: Any) -> bool:
    """An identity value that carries no information — skipped by
    :func:`merge_identity` so it can never overwrite a stored field.

    ``None`` and the empty string ``""`` are the two "no value" sentinels.
    Deliberately a value/identity check, not general falsiness: ``0`` and
    ``False`` are *meaningful* scalars and must survive (``0 == ""`` and
    ``False == ""`` are both ``False`` in Python, so the equality test below
    already excludes them — spelled out here so a future edit does not
    "simplify" it to ``not value`` and start dropping legitimate zeros)."""
    return value is None or value == ""


@dataclass
class Interaction:
    """A single recorded interaction between two participants."""

    id: str
    participant_id: str
    participant_type: str
    other_participant_id: str
    other_participant_type: str
    interaction_type: str
    outcome: str | None
    sentiment: float
    created_at: float


@dataclass
class RelationshipSummary:
    """Summary of a relationship for LLM prompt injection."""

    other_participant_id: str
    other_participant_type: str
    trust_score: float
    interaction_count: int
    last_interaction_at: float | None
    notes: str | None
    recent_interactions: list[Interaction] = field(default_factory=list)
    # RFC 0021 PR 2: timestamp of the earliest stored interaction, used
    # to compute the relationship-cadence bucket ("frequent" / "regular"
    # / "sparse") in :mod:`agents.persona_runtime.memory_context`.
    # Computed via ``MIN(created_at)`` on the interactions table inside
    # ``get_relationship_summary``.  ``None`` for relationships that were
    # seeded from config without any recorded interaction yet, or for
    # legacy rows with no interaction history.
    first_interaction_at: float | None = None
