"""RFC 0037 PR 1 — the §A classification lattice contract, Python side.

Pins the helper contract where it is defined
(:mod:`agents.persona_runtime.classification`): the exact (level → rank)
table, the total order, and the THREE fail-closed directions —
stamp → ``internal`` (rule (a)), acting → the ``public`` floor (rule
(b)), entry → withheld-and-logged (rule (c)).  The RFC 0037 PR 4 gate
tests re-assert the same three directions *through the §D gate*; this
file is the source-of-truth pin so a helper regression surfaces without
a gate in the loop.

Cross-language contract: ``test_rank_table_is_pinned`` duplicates the
literal table asserted by ``TestClassificationRank_TableIsPinned`` in
``internal/channels/classification_test.go``.  The shared enum is finite
(four levels), so the two exhaustive literal pins ARE the agreement
property — a drift on either side fails that side's suite.
"""

from __future__ import annotations

import logging

import pytest

from agents.persona_runtime.classification import (
    CLASSIFICATION_INTERNAL,
    CLASSIFICATION_PUBLIC,
    CLASSIFICATION_RANKS,
    CLASSIFICATION_RESTRICTED,
    CLASSIFICATION_SECRET,
    DEFAULT_CLASSIFICATION,
    acting_rank,
    classification_rank,
    entry_rank_or_withhold,
    is_valid_classification,
    normalize_for_stamp,
    rank_for_stamp,
)

# Everything outside the §A vocabulary: None (no field at all), the empty
# string (proto3 absent), casing variants (the vocabulary is lowercase,
# case-sensitive), whitespace damage, and a plausible-but-wrong synonym.
UNKNOWN_LEVELS = [
    None,
    "",
    "confidential",  # plausible synonym — NOT a lattice level
    "PUBLIC",  # case-sensitive
    "Internal",
    " secret",  # whitespace-damaged label
    "secret\n",  # trailing-newline corruption
    "top-secret",
]


def test_rank_table_is_pinned() -> None:
    """The exact §A ordinals — the Python half of the cross-language pin."""
    assert CLASSIFICATION_RANKS == {
        "public": 0,
        "internal": 1,
        "restricted": 2,
        "secret": 3,
    }
    assert DEFAULT_CLASSIFICATION == CLASSIFICATION_INTERNAL == "internal"


def test_total_order() -> None:
    """public < internal < restricted < secret, strictly."""
    ordered = [
        CLASSIFICATION_PUBLIC,
        CLASSIFICATION_INTERNAL,
        CLASSIFICATION_RESTRICTED,
        CLASSIFICATION_SECRET,
    ]
    ranks: list[int] = []
    for level in ordered:
        rank = classification_rank(level)
        assert rank is not None, f"{level!r} must be a known level"
        ranks.append(rank)
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks), "the order is strict — no ties"


@pytest.mark.parametrize("level", UNKNOWN_LEVELS)
def test_classification_rank_unknown_is_none(level: str | None) -> None:
    """The core lookup refuses to default: unknown → ``None``, so no caller
    can accidentally ride a blanket default in either direction."""
    assert classification_rank(level) is None
    assert not is_valid_classification(level)


@pytest.mark.parametrize("level", UNKNOWN_LEVELS)
def test_rank_for_stamp_fails_closed_to_internal(level: str | None) -> None:
    """Rule (a): an absent/unknown level at a STAMPING boundary labels
    ``internal`` — confidential-by-default, never ``public``."""
    assert rank_for_stamp(level) == CLASSIFICATION_RANKS[CLASSIFICATION_INTERNAL]
    assert normalize_for_stamp(level) == CLASSIFICATION_INTERNAL


@pytest.mark.parametrize(("level", "rank"), sorted(CLASSIFICATION_RANKS.items()))
def test_known_levels_pass_through_every_resolver(level: str, rank: int) -> None:
    """A known level is never rewritten by any of the three resolvers."""
    assert rank_for_stamp(level) == rank
    assert normalize_for_stamp(level) == level
    assert acting_rank(level) == rank
    assert entry_rank_or_withhold(level) == rank


@pytest.mark.parametrize("level", UNKNOWN_LEVELS)
def test_acting_rank_fails_closed_to_public_floor(level: str | None) -> None:
    """Rule (b): an absent/unknown ACTING level resolves to the ``public``
    FLOOR — inject/return less.  This is the direction that closes the
    proto3 ``""`` version-skew window and covers the channel-less
    autonomous tick."""
    assert acting_rank(level) == CLASSIFICATION_RANKS[CLASSIFICATION_PUBLIC] == 0


@pytest.mark.parametrize("level", UNKNOWN_LEVELS)
def test_entry_rank_or_withhold_is_none_and_logs(
    level: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule (c): an unknown ENTRY protection level is withheld (``None`` —
    treated as above-``secret``) AND logged, so a corrupted label can never
    inject and never disappears silently."""
    with caplog.at_level(logging.WARNING, logger="agents.persona_runtime.classification"):
        assert entry_rank_or_withhold(level) is None
    assert any(
        "withheld" in record.message for record in caplog.records
    ), "the 'and logged' half of rule (c) lives in the helper"


def test_fail_directions_disagree_on_unknown() -> None:
    """The reason the default splits into three named resolvers at all
    (§A, revised 2026-07-19): on the SAME unknown input the three rules
    resolve in three different directions — stamp says ``internal`` (1),
    acting says ``public`` (0), entry says withhold (``None``).  A single
    blanket default could satisfy at most one of these."""
    unknown = "corrupted-label"
    assert rank_for_stamp(unknown) == 1
    assert acting_rank(unknown) == 0
    assert entry_rank_or_withhold(unknown) is None
    assert rank_for_stamp(unknown) != acting_rank(unknown), (
        "the stamp and acting defaults must differ — 'restrictive' flips direction"
    )
