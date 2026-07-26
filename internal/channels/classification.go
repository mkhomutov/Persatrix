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
// [classificationRank] takes only known levels (ok/sentinel return) and each
// rule is owned by exactly one named resolver — [RankForStamp] /
// [NormalizeForStamp] (a), [ActingRank] (b), [EntryRankOrWithhold] (c). No
// caller applies its own default.
package channels

import (
	"errors"
)

// Classification is an RFC 0037 §A confidentiality level. The vocabulary is
// fixed at the four constants below for v0.3.x (operator-defined lattice
// levels are RFC 0037 Open Question #1).
type Classification string

// The §A lattice, lowest to highest. The only operations the system needs
// are the total order (`a ≤ b`) and `max`, both taken over the ranks
// returned by [classificationRank].
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

// The item-8 dark-window guard (DarkWindowMaxClassification /
// ErrClassificationAboveDarkWindow / CheckDarkWindowClassification) lived
// here from PR 1 and was DELETED in the PR that armed the §D hard gate
// (RFC 0037 PR 4), exactly as its REMOVAL note prescribed: with the gate,
// the gated recall_notes, and the §B single-channel-turn guard live, a
// declared level above `internal` is now an enforced boundary rather than
// an unkeepable promise. Operators may classify channels `restricted` /
// `secret` from v0.3.12 on.

// classificationRanks is the single Go-side source of the §A total order.
// Kept in lock-step with `CLASSIFICATION_RANKS` in
// agents/persona_runtime/classification.py — the cross-language agreement is
// pinned by identical literal tables in classification_test.go and
// tests/unit/python/test_classification.py, so a drift on either side fails
// that side's suite.
//
// The `schemas/channel.schema.json` enum is the third copy of this vocabulary.
// It is pinned to the PYTHON table (test_channel_config_schema.py derives its
// expected level list from `CLASSIFICATION_RANKS` rather than re-typing a
// literal), so this table's agreement with the schema is transitive: a level
// added here must be added to the Python table to pass the pin above, and that
// edit is what trips the schema test.
var classificationRanks = map[Classification]int{
	ClassificationPublic:     0,
	ClassificationInternal:   1,
	ClassificationRestricted: 2,
	ClassificationSecret:     3,
}

// classificationRank returns the §A lattice ordinal for a KNOWN level, and
// ok=false for anything else — including the empty string. Deliberately no
// default of any direction here: the three fail-closed rules disagree on what
// an unknown level means, so the default belongs to the named resolvers
// below, never to the core rank lookup (§A, the "restrictive flips direction"
// rationale in the file header).
//
// UNEXPORTED on purpose, so "each rule is owned by exactly one named resolver"
// is enforced by the compiler rather than by convention. An out-of-package
// gate author (PRs 4–5) can reach only [RankForStamp]/[NormalizeForStamp],
// [ActingRank], and [EntryRankOrWithhold] — each of which names the §A rule it
// applies — and cannot open-code a bare lookup plus a locally chosen default,
// which is the one mistake this file exists to prevent. Export it only
// alongside a fourth §A rule that genuinely needs it.
func classificationRank(level Classification) (rank int, ok bool) {
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
	rank, _ := classificationRank(NormalizeForStamp(level))
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
	if rank, ok := classificationRank(level); ok {
		return rank
	}
	rank, _ := classificationRank(ClassificationPublic)
	return rank
}

// EntryRankOrWithhold is rule (c): the rank of a stored ENTRY protection
// level. A known level ranks as itself; unknown/unparseable returns ok=false
// — the entry is withheld, treated as above-`secret`, never coerced onto the
// lattice where it could inject on a corrupted label. Semantically this is
// the bare [classificationRank], named so gate-side callers state which §A
// rule they are applying instead of open-coding a lookup-plus-default that
// would silently pick a direction.
//
// Pure, and so is the Python twin `entry_rank_or_withhold` — rule (c)'s "and
// logged" half belongs to the CALLER in both languages, by design rather than
// by omission. The caller is the only layer holding the entry's identity (a
// bare `unknown protection_level "xyz"` cannot be triaged), and the §F recall
// filter calls this once per candidate row, so an in-helper warning would turn
// one corrupted batch into a log flood — loudest exactly when an operator is
// trying to read the gate's decisions. The security half is not delegated:
// withholding rides ok=false and cannot be forgotten without ignoring the
// result.
func EntryRankOrWithhold(level Classification) (rank int, ok bool) {
	return classificationRank(level)
}
