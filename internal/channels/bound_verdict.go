package channels

// bound_verdict.go — the RFC 0052 bounded close's config re-validation matrix,
// split out of bounded_close.go at the 500-line cap (ISSUE-0082 residuals PR 4b,
// which added the reserve-clamp signal to that file). The seam is the natural
// one: every other line in bounded_close.go is the TAIL HOOK — when a bound is
// crossed and what happens next — while this pair answers one question that
// three separate action points ask, and asks nothing of the router. Keeping it
// here means an action point can be read against the whole matrix rather than
// its own subset, which is the failure mode the matrix exists to prevent (see
// [boundStandsAgainst]).

// boundVerdict is [boundStandsAgainst]'s answer at a bound action point.
type boundVerdict int

const (
	// boundStands — the crossing survives the fresh config; act on it.
	boundStands boundVerdict = iota
	// boundDisabled — an RFC 0050 disable landed since the crossing: the
	// operator took manual control, leave the interaction open.
	boundDisabled
	// boundExtended — a mid-flight `max_rounds` raise re-covers the structural
	// crossing: the operator extended the discussion, the tally survives and
	// re-crosses against the raised bound.
	boundExtended
)

// boundStandsAgainst re-validates a crossed bound against the CURRENT config
// at an ACTION point — THE one matrix of which config halves apply (PR #718
// review, twice: the tail and the timeout net each originally hand-rolled a
// subset and each shipped missing a half — the net's first shape re-checked
// only Enabled and silently ignored a mid-arm raise). Every action point that
// acts on a crossed bound (the tail's arm-or-close, the timeout net's fire,
// any future close trigger) routes its `fresh` re-read through here and keeps
// only its own verdict-to-action mapping; the deliberate asymmetries live
// here once: the COST half is never re-checked (its per-interaction snapshot
// immutability is the documented wallet-consistent design,
// interaction_budget.go), so callers collapse their trigger label FIRST —
// budget-crossed prefers `cost`, which a raise cannot extend. The REPLY path
// deliberately consults nothing: the synthesis artifact is in hand, closing
// with it is §D's point, and a raise governs the successor interaction.
// `tally` is the caller's structural round count — live at the tail, frozen
// on the armed entry at the fire.
func boundStandsAgainst(fresh AutonomousConfig, trigger string, tally int) boundVerdict {
	if !fresh.Enabled {
		return boundDisabled
	}
	if trigger == structuralTrigger && tally < fresh.MaxRounds {
		return boundExtended
	}
	return boundStands
}
