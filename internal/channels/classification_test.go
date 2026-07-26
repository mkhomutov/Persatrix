// RFC 0037 PR 1 — the §A classification lattice contract, pinned AT THE
// HELPER where it is defined. The three fail-closed directions asserted here
// (stamp→`internal`, acting→`public` floor, entry→withhold) are re-asserted
// *through the §D gate* in the RFC 0037 PR 4 tests; this file is the
// source-of-truth pin so a helper regression surfaces without a gate in the
// loop.
//
// Cross-language contract: the literal (level → rank) table asserted in
// TestClassificationRank_TableIsPinned is duplicated verbatim in
// tests/unit/python/test_classification.py against
// agents/persona_runtime/classification.py. The shared enum is finite (four
// levels), so the two exhaustive literal pins ARE the agreement property — a
// drift on either side fails that side's suite.
package channels

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

// unknownLevels is the shared negative fixture: everything outside the §A
// vocabulary, including the empty string (proto3 absent), casing variants
// (the vocabulary is lowercase, case-sensitive), whitespace damage, and a
// plausible-but-wrong synonym.
var unknownLevels = []Classification{
	"",
	"confidential", // plausible synonym — NOT a lattice level
	"PUBLIC",       // case-sensitive
	"Internal",
	" secret",  // whitespace-damaged label
	"secret\n", // trailing-newline corruption
	"top-secret",
}

// TestClassificationRank_TableIsPinned pins the exact §A ordinals. This is
// the Go half of the cross-language agreement pin (see file header).
func TestClassificationRank_TableIsPinned(t *testing.T) {
	want := map[Classification]int{
		ClassificationPublic:     0,
		ClassificationInternal:   1,
		ClassificationRestricted: 2,
		ClassificationSecret:     3,
	}
	assert.Len(t, classificationRanks, len(want),
		"the §A lattice is fixed at four levels for v0.3.x (RFC 0037 OQ #1)")
	for level, rank := range want {
		got, ok := classificationRank(level)
		assert.True(t, ok, "level %q must be known", level)
		assert.Equal(t, rank, got, "level %q rank", level)
		assert.True(t, level.Valid())
	}
}

// TestClassificationRank_TotalOrder asserts the ordering the whole boundary
// depends on: public < internal < restricted < secret, strictly.
func TestClassificationRank_TotalOrder(t *testing.T) {
	ordered := []Classification{
		ClassificationPublic,
		ClassificationInternal,
		ClassificationRestricted,
		ClassificationSecret,
	}
	for i := 1; i < len(ordered); i++ {
		lo, okLo := classificationRank(ordered[i-1])
		hi, okHi := classificationRank(ordered[i])
		assert.True(t, okLo)
		assert.True(t, okHi)
		assert.Less(t, lo, hi, "%q must rank strictly below %q", ordered[i-1], ordered[i])
	}
}

// TestClassificationRank_UnknownIsNotOK pins the core helper's refusal to
// default: anything outside the vocabulary returns ok=false, so no caller
// can accidentally ride a blanket default in either direction.
func TestClassificationRank_UnknownIsNotOK(t *testing.T) {
	for _, level := range unknownLevels {
		_, ok := classificationRank(level)
		assert.False(t, ok, "level %q must be unknown to the core rank lookup", level)
		assert.False(t, level.Valid(), "level %q must not validate", level)
	}
}

// TestRankForStamp_FailDirection pins §A rule (a): an absent/unknown level at
// a STAMPING boundary labels `internal` — confidential-by-default, never
// `public`. Known levels pass through unchanged.
func TestRankForStamp_FailDirection(t *testing.T) {
	internalRank, _ := classificationRank(ClassificationInternal)
	for _, level := range unknownLevels {
		assert.Equal(t, internalRank, RankForStamp(level),
			"unknown %q must stamp-rank as internal (rule (a))", level)
		assert.Equal(t, ClassificationInternal, NormalizeForStamp(level),
			"unknown %q must normalize to internal for the write (rule (a))", level)
	}
	for level, want := range classificationRanks {
		assert.Equal(t, want, RankForStamp(level), "known %q passes through", level)
		assert.Equal(t, level, NormalizeForStamp(level), "known %q is not rewritten", level)
	}
}

// TestActingRank_FailDirection pins §A rule (b): an absent/unknown ACTING
// level resolves to the `public` FLOOR — inject/return less. This is the
// direction that closes the proto3 "" version-skew window and covers the
// channel-less autonomous tick.
func TestActingRank_FailDirection(t *testing.T) {
	publicRank, _ := classificationRank(ClassificationPublic)
	for _, level := range unknownLevels {
		assert.Equal(t, publicRank, ActingRank(level),
			"unknown %q must act at the public floor (rule (b))", level)
	}
	for level, want := range classificationRanks {
		assert.Equal(t, want, ActingRank(level), "known %q passes through", level)
	}
}

// TestEntryRankOrWithhold_FailDirection pins §A rule (c): an unknown ENTRY
// protection level is withheld (ok=false — treated as above-`secret`), never
// coerced onto the lattice where it could inject on a corrupted label.
func TestEntryRankOrWithhold_FailDirection(t *testing.T) {
	for _, level := range unknownLevels {
		_, ok := EntryRankOrWithhold(level)
		assert.False(t, ok, "unknown %q must be withheld (rule (c))", level)
	}
	for level, want := range classificationRanks {
		got, ok := EntryRankOrWithhold(level)
		assert.True(t, ok)
		assert.Equal(t, want, got, "known %q passes through", level)
	}
}

// TestInjectableLevels_SetsPinned pins the rules-(b)+(c) LEVEL-SET resolver
// the §F recall filter binds into its SQL IN predicate (RFC 0037 PR 5):
// exact, rank-ascending literal sets for every acting level, and the `public`
// floor for every unknown/absent acting value. The Python twin
// (`injectable_levels`) is pinned by the same exhaustive literals in
// tests/unit/python/test_classification.py — the finite enum makes the two
// literal pins the cross-language agreement property, as with the rank table.
func TestInjectableLevels_SetsPinned(t *testing.T) {
	want := map[Classification][]Classification{
		ClassificationPublic:   {ClassificationPublic},
		ClassificationInternal: {ClassificationPublic, ClassificationInternal},
		ClassificationRestricted: {
			ClassificationPublic, ClassificationInternal, ClassificationRestricted,
		},
		ClassificationSecret: {
			ClassificationPublic, ClassificationInternal,
			ClassificationRestricted, ClassificationSecret,
		},
	}
	for acting, levels := range want {
		assert.Equal(t, levels, InjectableLevels(acting),
			"acting %q serves exactly the ≤-rank set, rank-ascending", acting)
	}
	for _, level := range unknownLevels {
		assert.Equal(t, []Classification{ClassificationPublic}, InjectableLevels(level),
			"unknown acting %q floors to the public-only set (rule (b))", level)
	}
}

// TestFailDirections_DisagreeOnUnknown pins the reason the default is split
// into three named resolvers at all (§A, revised 2026-07-19): on the SAME
// unknown input the three rules resolve in three different directions —
// stamp says `internal` (1), acting says `public` (0), entry says withhold.
// A single blanket default could satisfy at most one of these.
func TestFailDirections_DisagreeOnUnknown(t *testing.T) {
	const unknown = Classification("corrupted-label")

	stamp := RankForStamp(unknown)
	acting := ActingRank(unknown)
	_, entryOK := EntryRankOrWithhold(unknown)

	assert.Equal(t, 1, stamp, "rule (a): stamp defaults to internal")
	assert.Equal(t, 0, acting, "rule (b): acting floors to public")
	assert.False(t, entryOK, "rule (c): entry is withheld")
	assert.NotEqual(t, stamp, acting,
		"the stamp and acting defaults must differ — 'restrictive' flips direction")
}
