"""Light structuring of a contact-note ``content`` string into the
structured person-identity object stored on the relationship tier.

RFC 0031 amendment (F-7 Option D, ISSUE-0093) **PR D2** — the write-through
intercept (``store_note(topic="contact:<id>")`` → ``upsert_identity``)
needs to turn the free-text ``content`` the model writes into the small
``{name, role, prefs}`` object that
:func:`agents.memory.relationship_types.merge_identity` merges onto the
relationship row.

This step is deliberately a **pure, deterministic regex / keyed-prefix
parse — never an LLM call** (decision D-1 in the amendment rejects a
per-turn extractor for latency/cost reasons): the
``memory-tool-usage`` prompt already asks for a name-and-details shape,
and anything the parser cannot key is preserved verbatim under a ``raw``
key so no information is lost and the relationship-tier merge stays
non-destructive.  Living here (no DB dependency) keeps the parse
unit-testable in isolation and importable from the
``agents.tools.builtin`` write boundary without dragging the store in.
"""

from __future__ import annotations

import re

__all__ = ["parse_identity_fields"]

# Synonyms the model reaches for when stating each identity field.  Kept
# small and lower-cased; matched against the *key* half of a
# ``key: value`` segment (see ``_KEYED_RE``).  ``prefs`` is the only
# list-valued field (it unions across turns — see ``_IDENTITY_LIST_KEYS``
# in ``relationship_types``); ``name`` / ``role`` are scalar.
_NAME_KEYS = frozenset({"name", "full name", "real name"})
_ROLE_KEYS = frozenset({"role", "title", "job", "position", "occupation"})
_PREF_KEYS = frozenset({
    "prefers", "prefer", "preference", "preferences",
    "likes", "like", "favorite", "favourite", "fav",
    "enjoys", "enjoy",
})

# Preference *lead* words — the model often qualifies a preference key
# ("Favorite language", "Preferred editor"), so a key whose first word is
# one of these is treated as a preference even when the full key is not in
# ``_PREF_KEYS``.  Scalar fields (name / role) match the full key only.
_PREF_LEAD_KEYS = frozenset({
    "prefers", "prefer", "preferred", "preference", "preferences",
    "likes", "like", "favorite", "favourite", "fav", "enjoys", "enjoy",
})

# A contact note is a handful of short clauses.  Split on sentence /
# clause boundaries (``.`` ``;`` newline) so each segment can be keyed
# independently; commas stay *inside* a segment so a preference list
# ("Rust, Go") is not shredded into separate clauses.
_SEGMENT_RE = re.compile(r"[.;\n]+")

# A keyed clause: ``Name: Max`` / ``Role = engineer``.  The key is a
# short alphabetic run (allowing internal spaces, e.g. "full name"); the
# value is everything after the first ``:`` / ``=``.
_KEYED_RE = re.compile(r"^([A-Za-z][A-Za-z ]*?)\s*[:=]\s*(.+)$")

# Unkeyed natural phrasing for the name only — "my name is Max" / "name
# is Max" / "I am Max" / "call me Max".  Deliberately narrow: name is the
# one field the model frequently states without a ``Name:`` prefix, and
# it is the load-bearing "who is this" signal.  Other fields fall through
# to ``raw`` when unkeyed.  The ``(.+)`` capture is greedy, so a match is
# additionally gated by :func:`_is_namelike` to reject prose ("I am happy
# to help") — see that helper.
_NAME_PHRASE_RE = re.compile(
    r"^(?:my name is|name is|i am|i'm|call me)\s+(.+)$", re.IGNORECASE,
)

# Split a preference value into individual items: commas and a trailing
# "and" ("Rust, Go and Python").
_PREF_SPLIT_RE = re.compile(r"\s*,\s*|\s+and\s+", re.IGNORECASE)

# Upper bound on the word count of an *inferred* (natural-phrase) name.  A
# proper name is a short run; anything longer is prose that happened to
# start with "I am" / "call me" and is rejected (kept under ``raw``).  The
# keyed ``Name:`` path is unbounded — an explicit key is trusted as-is.
_MAX_INFERRED_NAME_WORDS = 4


def parse_identity_fields(content: str) -> dict[str, object]:
    """Parse a contact-note ``content`` string into structured identity.

    Returns a dict that may contain any of ``name`` (str), ``role`` (str),
    ``prefs`` (list[str]), and ``raw`` (str — clauses that matched no
    field, joined with ``". "``).  An empty / whitespace-only input
    returns ``{}``.  Pure and deterministic — never an LLM call.

    The shape is exactly what
    :func:`agents.memory.relationship_types.merge_identity` expects:
    scalar fields are last-writer-wins, ``prefs`` unions, and absent
    fields are simply omitted (so a partial note never nulls a stored
    value on merge).

    Known limitation: ``.`` is a clause separator (so ``"Name: A. Role:
    B."`` keys both fields), which means a ``.`` *inside* a value is also a
    boundary — ``"Name: Dr. Smith"`` keys ``name="Dr"`` and spills
    ``"Smith"`` to ``raw``. Acceptable for a deterministic parser: the
    spilled remainder is preserved (and now rendered) under ``raw`` rather
    than lost, and the multi-clause shape the model actually writes
    dominates over abbreviations.
    """
    fields: dict[str, object] = {}
    prefs: list[str] = []
    raw_parts: list[str] = []

    for segment in _SEGMENT_RE.split(content):
        clause = segment.strip()
        if not clause:
            continue
        keyed = _KEYED_RE.match(clause)
        if keyed is not None:
            key = keyed.group(1).strip().lower()
            value = keyed.group(2).strip()
            if not value:
                continue
            if key in _NAME_KEYS:
                fields["name"] = value
                continue
            if key in _ROLE_KEYS:
                fields["role"] = value
                continue
            if key in _PREF_KEYS or key.split(" ", 1)[0] in _PREF_LEAD_KEYS:
                prefs.extend(_split_prefs(value))
                continue
            # A keyed clause under an unknown key — keep the whole clause
            # verbatim so the detail is not lost.
            raw_parts.append(clause)
            continue
        name_phrase = _NAME_PHRASE_RE.match(clause)
        if name_phrase is not None and "name" not in fields:
            candidate = name_phrase.group(1).strip()
            if _is_namelike(candidate):
                fields["name"] = candidate
                continue
        raw_parts.append(clause)

    if prefs:
        fields["prefs"] = prefs
    if raw_parts:
        fields["raw"] = ". ".join(raw_parts)
    return fields


def _split_prefs(value: str) -> list[str]:
    """Split a preference clause value into de-blanked individual items."""
    return [item.strip() for item in _PREF_SPLIT_RE.split(value) if item.strip()]


def _is_namelike(value: str) -> bool:
    """Whether an *inferred* natural-phrase capture is shaped like a name.

    The natural-name phrases ("I am …", "call me …") match greedily to the
    end of the clause, so prose like "I am happy to help" otherwise becomes
    ``name="happy to help"`` — which renders as the load-bearing "who is
    this" line and, since ``name`` is scalar last-writer-wins, can clobber a
    real name (PR #554 deep-review #2).  A proper name is a *short* run whose
    first character is uppercase, so we require both; non-matching prose
    falls through to ``raw`` (preserved, not lost).  Only the *inferred*
    path is gated — an explicit ``Name:`` key is trusted regardless of case.
    """
    if not value or not value[0].isupper():
        return False
    return len(value.split()) <= _MAX_INFERRED_NAME_WORDS
