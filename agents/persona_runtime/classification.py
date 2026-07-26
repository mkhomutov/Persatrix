"""RFC 0037 §A classification lattice — the Python twin of the Go helper.

The fixed, totally ordered four-level confidentiality vocabulary every
channel (and, from the memory-substrate PRs on, every channel-derived
memory entry) is labeled with, plus the canonical rank helper the whole
confidentiality boundary compares through.  This module is the single
Python-side source of the ordering; the Go twin is
``internal/channels/classification.go`` and the SQL-side form arrives
with the §F recall filter (RFC 0037 PR 5).  No code path compares level
strings directly — every comparison goes through one of the helpers
below.

Fail-closed splits into THREE explicit rules (§A, revised 2026-07-19 —
v0.3.12 review items 5/8), because "restrictive" flips direction across
the helper's uses:

* **(a) stamping/labeling** — absent/unknown → ``internal`` (a channel
  the operator forgot to classify is confidential-by-default, never
  public).
* **(b) acting level at gate/recall time** — absent/unknown → the
  ``public`` FLOOR (inject/return LESS; also closes the proto3 ``""``
  version-skew window).
* **(c) entry protection level unknown/unparseable** — the entry is
  WITHHELD and logged (treated as above-``secret``: never injectable on
  a corrupted label).

A single blanket ``unknown → internal`` default would make (c)
unimplementable through the helper — a corrupted entry label would rank
``internal`` and inject cleanly into any ``internal`` turn.  So the core
:func:`_classification_rank` returns ``None`` for anything outside the
vocabulary, and each rule is owned by exactly one named resolver —
:func:`rank_for_stamp` / :func:`normalize_for_stamp` (a),
:func:`acting_rank` (b), :func:`entry_rank_or_withhold` (c).  No caller
applies its own default — and cannot: the core lookup is private on both
sides of the twin (``_classification_rank`` here, the unexported
``classificationRank`` in Go), so the only reachable doors are the three
that name the rule they apply.

**Where rule (c)'s "and logged" half lives: the CALLER, in both
languages.**  Every resolver here is pure — same contract as the Go twin's
``EntryRankOrWithhold``, deliberately, so the two do not disagree about
who owns the log line.  Two reasons the obligation sits with the caller
rather than in the helper:

* *Triage needs identity.*  The helper sees a bare label.  A gate-side
  ``WARNING`` naming the entry, its channel, and the acting level is
  actionable; ``unknown protection_level 'xyz'`` on its own is not.
* *Volume.*  The §F recall filter (RFC 0037 PR 5) evaluates this
  per candidate row, so an in-helper warning is one line per corrupted
  row — a single bad batch becomes a log flood, and the flood is loudest
  exactly when an operator is trying to read the gate's decisions.

The *security* half of rule (c) is not delegated: withholding is carried
by the ``None`` return and cannot be forgotten without ignoring the
result.  The log is observability layered on top, and every gate-side
caller (PRs 4–5) owes it.
"""

from __future__ import annotations

from typing import Final

# The §A lattice, lowest to highest.  The only operations the system needs
# are the total order and max, both taken over the ranks below.
CLASSIFICATION_PUBLIC: Final[str] = "public"
CLASSIFICATION_INTERNAL: Final[str] = "internal"
CLASSIFICATION_RESTRICTED: Final[str] = "restricted"
CLASSIFICATION_SECRET: Final[str] = "secret"

#: The §A rule-(a) stamping default: what an absent-by-policy classification
#: labels to.  Deliberately ``internal``, never ``public``.
DEFAULT_CLASSIFICATION: Final[str] = CLASSIFICATION_INTERNAL

#: The single Python-side source of the §A total order.  Kept in lock-step
#: with ``classificationRanks`` in ``internal/channels/classification.go`` —
#: the cross-language agreement is pinned by identical literal tables in
#: ``tests/unit/python/test_classification.py`` and the Go
#: ``classification_test.go``, so a drift on either side fails that side's
#: suite.
#:
#: The JSON-schema enum is the third copy of this vocabulary and is pinned to
#: THIS table, not to a literal of its own:
#: ``test_channel_config_schema.py`` derives its expected level list from here,
#: so adding or renaming a level without editing
#: ``schemas/channel.schema.json`` fails that suite.  Go ↔ schema agreement is
#: therefore transitive through this dict.
CLASSIFICATION_RANKS: Final[dict[str, int]] = {
    CLASSIFICATION_PUBLIC: 0,
    CLASSIFICATION_INTERNAL: 1,
    CLASSIFICATION_RESTRICTED: 2,
    CLASSIFICATION_SECRET: 3,
}


def _classification_rank(level: str | None) -> int | None:
    """Return the §A lattice ordinal for a KNOWN level, else ``None``.

    Deliberately no default of any direction here — including for
    ``None``/empty input: the three fail-closed rules disagree on what an
    unknown level means, so the default belongs to the named resolvers
    below, never to the core rank lookup (the "restrictive flips
    direction" rationale in the module docstring).  Comparison is exact —
    the vocabulary is lowercase and case-sensitive, matching the schema
    enum.

    PRIVATE on purpose, mirroring the Go twin's unexported
    ``classificationRank``: "each rule is owned by exactly one named
    resolver" should be a property of the module's surface, not a request
    in a docstring.  Importers get the three rule-named resolvers and
    :func:`is_valid_classification`; a gate author (PRs 4–5) has no
    unnamed lookup to open-code a locally chosen default against.  Only
    this module's own tests reach past the underscore.
    """
    if level is None:
        return None
    return CLASSIFICATION_RANKS.get(level)


def is_valid_classification(level: str | None) -> bool:
    """Report whether ``level`` is one of the four §A levels."""
    return level is not None and level in CLASSIFICATION_RANKS


def normalize_for_stamp(level: str | None) -> str:
    """Rule (a) in the level domain: the classification to WRITE when stamping.

    A known level passes through; absent or unknown labels to
    :data:`DEFAULT_CLASSIFICATION` (``internal``), never ``public``.
    """
    if level is not None and level in CLASSIFICATION_RANKS:
        return level
    return DEFAULT_CLASSIFICATION


def rank_for_stamp(level: str | None) -> int:
    """Rule (a) in the rank domain: the ordinal of ``normalize_for_stamp(level)``.

    Provided so stamp-side comparisons and the stamp-side write share one
    rule owner.  Total: ``normalize_for_stamp`` only returns known levels.
    """
    return CLASSIFICATION_RANKS[normalize_for_stamp(level)]


def acting_rank(level: str | None) -> int:
    """Rule (b): the rank of the ACTING classification at gate/recall time.

    A known level ranks as itself; absent or unknown resolves to the
    ``public`` FLOOR — inject/return less.  This is deliberately the
    opposite direction from :func:`rank_for_stamp`: an event arriving with
    no classification (proto3 ``""`` from an older orchestrator, an
    autonomous tick with no channel) must see the least-confidential view,
    not the ``internal`` default a stamp-side coercion would grant.
    """
    rank = _classification_rank(level)
    if rank is not None:
        return rank
    return CLASSIFICATION_RANKS[CLASSIFICATION_PUBLIC]


def acting_at_or_below_internal(level: str | None) -> bool:
    """The §C identity write-through bound: does the ACTING classification
    rank at or below ``internal``?

    ``True`` → the cross-room relationship write-through may proceed;
    ``False`` (a ``restricted``/``secret`` turn) → the caller falls back
    to a room-scoped note, which is stamped and gated.  Resolution is
    rule (b) — an absent/unknown acting level floors to ``public`` and
    proceeds, so every pre-classification path (tick, CLI, pre-v0.3.12
    producer) keeps its exact prior behaviour.  Named here — beside the
    rule owner it composes — so the one call site
    (:mod:`agents.tools.identity_write_through`) states the §A rule it
    applies instead of open-coding a rank comparison.
    """
    return acting_rank(level) <= CLASSIFICATION_RANKS[CLASSIFICATION_INTERNAL]


def entry_rank_or_withhold(level: str | None) -> int | None:
    """Rule (c): the rank of a stored ENTRY protection level.

    A known level ranks as itself; unknown/unparseable returns ``None`` —
    the entry is withheld, treated as above-``secret``, never coerced onto
    the lattice where it could inject on a corrupted label.

    Semantically this is the bare :func:`_classification_rank`, named so
    gate-side callers state which §A rule they are applying instead of
    open-coding a lookup-plus-default that would silently pick a
    direction.  Pure, exactly like the Go twin ``EntryRankOrWithhold``:
    rule (c)'s "and logged" half belongs to the caller, which is the only
    layer holding the entry's identity — see the module docstring for why,
    and note the recall filter calls this once per candidate row.
    """
    return _classification_rank(level)


def injectable_levels(acting: str | None) -> tuple[str, ...]:
    """Rules (b) + (c) in the LEVEL-SET domain: the entry protection levels
    injectable at the ACTING level ``acting`` — every vocabulary level whose
    rank is ``<= acting_rank(acting)``, rank-ascending.

    This is the shape the §D read-surface SQL predicates consume (RFC 0037
    PR 4 — the gated ``recall_notes``): the memory layer must not import
    this lattice (the import direction is ``persona_runtime → memory``), so
    the persona-side read surfaces resolve the acting level to this
    IN-list and pass it down as plain data.  A stored label OUTSIDE the
    returned set — including a corrupted one — falls out of the ``IN``
    predicate, which IS rule (c)'s withhold realised in SQL.  The SQL form
    is deliberately silent per row (the log-flood rationale in the module
    docstring); the Python-side gate over loaded rows owns the aggregated
    rule-(c) WARNING.

    Resolution of ``acting`` is rule (b): unknown/absent floors to
    ``public``, so an unbound turn's IN-list is ``("public",)`` — the
    least-disclosing set, never the stamp default.
    """
    bound = acting_rank(acting)
    return tuple(
        level for level, rank in CLASSIFICATION_RANKS.items() if rank <= bound
    )


def levels_below_stamp(level: str | None) -> tuple[str, ...]:
    """Rule (a) in the LEVEL-SET domain: the levels ranking STRICTLY below
    ``normalize_for_stamp(level)``, rank-ascending.

    The §C ``update_note`` re-stamp consumes this: an edit re-stamps a note
    to ``max(existing protection_level, acting L)`` — never lowers — and the
    memory layer realises that ``max`` as a raise-only SQL ``CASE`` gated on
    the existing value being IN this set (RFC 0037 PR 4).  A corrupted
    existing label is not in the set and therefore keeps its value,
    failing closed at read time per rule (c) rather than being silently
    laundered onto the lattice by an edit.

    Empty for ``level=None``/unknown-floor cases resolving to a ``public``
    stamp?  No — resolution here is rule (a) (absent → ``internal``), so the
    absent case returns ``("public",)``; only an explicit ``public`` acting
    level returns the empty tuple (nothing ranks below the floor).
    """
    bound = rank_for_stamp(level)
    return tuple(
        level_ for level_, rank in CLASSIFICATION_RANKS.items() if rank < bound
    )
