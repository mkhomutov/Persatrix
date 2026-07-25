// classification.go — the RFC 0037 §A confidentiality classification lattice:
// the fixed, totally ordered four-level vocabulary every channel and (from the
// memory-substrate PRs on) every channel-derived memory entry is labeled with,
// plus the canonical rank helper the whole confidentiality boundary compares
// through. This file is the single Go-side source of the ordering; the Python
// twin is agents/persona_runtime/classification.py and the SQL-side form
// arrives with the §F recall filter (RFC 0037 PR 5). No code path compares
// level strings directly — every comparison goes through one of the helpers
// below.
//
// Fail-closed splits into THREE explicit rules (§A, revised 2026-07-19 —
// v0.3.12 review items 5/8), because "restrictive" flips direction across the
// helper's uses:
//
//	(a) stamping/labeling      — absent/unknown → `internal` (a channel the
//	    operator forgot to classify is confidential-by-default, never public)
//	(b) acting level at gate/  — absent/unknown → the `public` FLOOR (inject/
//	    recall time              return LESS; also closes the proto3 ""
//	                             version-skew window)
//	(c) entry protection level — unknown/unparseable → WITHHELD (treated as
//	    unknown/unparseable       above-`secret`: never injectable on a
//	                              corrupted label)
//
// A single blanket `unknown → internal` default would make (c)
// unimplementable through the helper — a corrupted entry label would rank
// `internal` and inject cleanly into any `internal` turn. So the core
// [ClassificationRank] takes only known levels (ok/sentinel return) and each
// rule is owned by exactly one named resolver — [RankForStamp] /
// [NormalizeForStamp] (a), [ActingRank] (b), [EntryRankOrWithhold] (c). No
// caller applies its own default.
package channels

import "errors"

// Classification is an RFC 0037 §A confidentiality level. The vocabulary is
// fixed at the four constants below for v0.3.x (operator-defined lattice
// levels are RFC 0037 Open Question #1).
type Classification string

// The §A lattice, lowest to highest. The only operations the system needs
// are the total order (`a ≤ b`) and `max`, both taken over the ranks
// returned by [ClassificationRank].
const (
	// ClassificationPublic — rank 0. No confidentiality expectation; the
	// safe floor rule (b) resolves an unknown ACTING level to.
	ClassificationPublic Classification = "public"
	// ClassificationInternal — rank 1. Ordinary in-org conversation; the
	// default rule (a) stamps an unclassified channel/entry with.
	ClassificationInternal Classification = "internal"
	// ClassificationRestricted — rank 2. Sensitive; need-to-know within a
	// subset.
	ClassificationRestricted Classification = "restricted"
	// ClassificationSecret — rank 3. Highly sensitive; disclosure is a
	// material harm.
	ClassificationSecret Classification = "secret"
)

// DefaultClassification is the §A rule-(a) stamping default: what an
// absent-by-policy classification labels to. Deliberately `internal`, never
// `public` — a channel the operator forgot to classify is
// confidential-by-default.
const DefaultClassification = ClassificationInternal

// ErrInvalidClassification is returned by [Config.Validate] when an operator
// declares a classification outside the §A vocabulary. Load-time rejection is
// the belt-and-suspenders for the schema enum (`make validate`); the runtime
// resolvers never see the bad value.
var ErrInvalidClassification = errors.New("channels: invalid classification")

// classificationRanks is the single Go-side source of the §A total order.
// Kept in lock-step with `CLASSIFICATION_RANKS` in
// agents/persona_runtime/classification.py — the cross-language agreement is
// pinned by identical literal tables in classification_test.go and
// tests/unit/python/test_classification.py, so a drift on either side fails
// that side's suite.
var classificationRanks = map[Classification]int{
	ClassificationPublic:     0,
	ClassificationInternal:   1,
	ClassificationRestricted: 2,
	ClassificationSecret:     3,
}

// ClassificationRank returns the §A lattice ordinal for a KNOWN level, and
// ok=false for anything else — including the empty string. Deliberately no
// default of any direction here: the three fail-closed rules disagree on what
// an unknown level means, so the default belongs to the named resolvers
// below, never to the core rank lookup (§A, the "restrictive flips direction"
// rationale in the file header).
func ClassificationRank(level Classification) (rank int, ok bool) {
	rank, ok = classificationRanks[level]
	return rank, ok
}

// Valid reports whether level is one of the four §A levels. Comparison is
// exact — the vocabulary is lowercase and case-sensitive, matching the schema
// enum.
func (c Classification) Valid() bool {
	_, ok := classificationRanks[c]
	return ok
}

// NormalizeForStamp is rule (a) in the level domain: the classification to
// WRITE when stamping/labeling. A known level passes through; absent or
// unknown labels to [DefaultClassification] (`internal`), never `public`.
// This is what DM creation stamps from the `dm_default_classification` knob
// ([sqliteStore.GetOrCreateDM]) and what every future labeling path uses.
func NormalizeForStamp(level Classification) Classification {
	if level.Valid() {
		return level
	}
	return DefaultClassification
}

// RankForStamp is rule (a) in the rank domain: the ordinal of
// [NormalizeForStamp](level). Provided so stamp-side comparisons and the
// stamp-side write share one rule owner.
func RankForStamp(level Classification) int {
	rank, _ := ClassificationRank(NormalizeForStamp(level))
	return rank
}

// ActingRank is rule (b): the rank of the ACTING classification at gate/
// recall time. A known level ranks as itself; absent or unknown resolves to
// the `public` FLOOR — inject/return less. This is deliberately the opposite
// direction from [RankForStamp]: an event arriving with no classification
// (proto3 "" from an older orchestrator, an autonomous tick with no channel)
// must see the least-confidential view, not the `internal` default a
// stamp-side coercion would grant.
func ActingRank(level Classification) int {
	if rank, ok := ClassificationRank(level); ok {
		return rank
	}
	rank, _ := ClassificationRank(ClassificationPublic)
	return rank
}

// EntryRankOrWithhold is rule (c): the rank of a stored ENTRY protection
// level. A known level ranks as itself; unknown/unparseable returns ok=false
// — the entry is withheld (treated as above-`secret`) and the caller logs it
// with the entry's identity. Semantically this is the bare
// [ClassificationRank], named so gate-side callers state which §A rule they
// are applying instead of open-coding a lookup-plus-default that would
// silently pick a direction.
func EntryRankOrWithhold(level Classification) (rank int, ok bool) {
	return ClassificationRank(level)
}
