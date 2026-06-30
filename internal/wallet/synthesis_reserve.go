package wallet

// synthesis_reserve.go — RFC 0052 (v0.3.11) PR 4a, the close-path accounting half
// of the bounded close. New wallet accounting with no shipped analog, carved out
// of wallet.go (at the review-friendly size cap) alongside the Layer 1 ceiling it
// extends in interaction_budget.go.
//
// An autonomous interaction MUST terminate AND always leave both artifacts — the
// chair synthesis turn and a per-persona RFC 0020 summary — even on a budget-
// exhausted close ([RFC 0052 §D](../../docs/rfcs/0052-autonomous-agent-channels.md)).
// The fail-closed per-interaction cost ceiling (interaction_budget.go) would
// otherwise deny the very leases that PRODUCE those artifacts once the discussion
// has spent the cap. The reserve prevents that: it splits the cap into
//
//   - a SOFT threshold ([SynthesisSoftBudgetTokens]) the DISCUSSION is bounded by,
//     and
//   - a RESERVE ([SynthesisReserveTokens]) held back for the close path.
//
// When running spend ([WalletService.InteractionSpend]) reaches the soft
// threshold, the bounded close (PR 4b) synthesizes-and-closes — drawing the close
// path's leases from the reserve — BEFORE the hard cap (interaction_budget_tokens)
// would deny them. The reserve is sized for `1 + N` close-path calls: the chair
// synthesis turn PLUS one RFC 0020 summary per participating persona. OQ #6 meters
// that summary so it counts toward the cap, and the close summary is authored
// per-agent (close_path.py spawns one finalize_closed_interaction per agent_id),
// so an N-persona roster issues N metered summary calls on the shared per-
// interaction budget — hence `1 + N`, not the fixed two an earlier framing assumed.
//
// PR 4a ships these DARK: AcquireLease still enforces only the hard cap, and no
// bounded-close path consults the soft threshold or the eviction yet. PR 4b wires
// the bounded close to [WalletService.InteractionSpend] / [SynthesisSoftBudgetTokens]
// and emits the [WalletService.EvictInteraction] on close.

// DefaultSynthesisCallReserveTokens is the conservative worst-case token cost of
// ONE close-path LLM call, used to size the reserve. It tracks the RFC 0020 close
// summary's bounds (agents/persona_runtime/summarize_close.py:
// SUMMARIZATION_TARGET_TOKENS=2000 input context + SUMMARIZATION_MAX_OUTPUT_TOKENS=1024
// output ≈ 3024) with headroom for the prompt/envelope overhead and the chair
// synthesis turn (the "1" of `1 + N`, bounded by the same Layer-0 depth cap). A
// conservative default (RFC 0052 OQ #5); the calibration tracked-issue tunes it
// after a soak on real rosters.
const DefaultSynthesisCallReserveTokens int64 = 3500

// maxSynthesisReserveNumerator / maxSynthesisReserveDenominator cap the reserve at
// HALF the cap, so the discussion always retains a positive working budget even
// under a tiny per-interaction cap where the raw `1 + N` reserve would otherwise
// swallow the whole ceiling and starve the conversation to nothing. The clamp only
// bites for small caps; with a realistic cap the raw `(1 + N) × unit` reserve is a
// small fraction. Expressed as a fraction (not a float) to stay integer-exact.
const (
	maxSynthesisReserveNumerator   int64 = 1
	maxSynthesisReserveDenominator int64 = 2
)

// SynthesisReserveTokens returns the tokens held back from a per-interaction cost
// cap for the bounded close path: the chair synthesis turn + one RFC 0020 summary
// per participating persona = `1 + rosterSize` close-path LLM calls (RFC 0052
// OQ #6 — the close summary is authored per-agent). `rosterSize` is N, the count of
// participating personas; a negative value is clamped to zero (the chair-only
// reserve). An uncapped interaction (`budgetTokens <= 0`) has no ceiling to carve
// and reserves nothing. The reserve is clamped to at most half the cap
// ([maxSynthesisReserve*]) so the soft threshold ([SynthesisSoftBudgetTokens]) is
// always positive.
func SynthesisReserveTokens(budgetTokens int64, rosterSize int) int64 {
	if budgetTokens <= 0 {
		return 0
	}
	if rosterSize < 0 {
		rosterSize = 0
	}
	closePathCalls := int64(1 + rosterSize) // 1 chair synthesis turn + N summaries.
	raw := closePathCalls * DefaultSynthesisCallReserveTokens
	if ceiling := budgetTokens * maxSynthesisReserveNumerator / maxSynthesisReserveDenominator; raw > ceiling {
		return ceiling
	}
	return raw
}

// SynthesisSoftBudgetTokens returns the working budget the DISCUSSION is bounded by
// — the per-interaction cap minus the synthesis reserve. When running spend reaches
// this soft threshold the bounded close (PR 4b) synthesizes-and-closes BEFORE the
// hard cap (`budgetTokens`) would deny the close-path leases. An uncapped
// interaction (`budgetTokens <= 0`) has no soft threshold (returns 0).
func SynthesisSoftBudgetTokens(budgetTokens int64, rosterSize int) int64 {
	if budgetTokens <= 0 {
		return 0
	}
	return budgetTokens - SynthesisReserveTokens(budgetTokens, rosterSize)
}

// InteractionSpend returns the running token total the wallet tracks for
// interactionID — the worst-case estimate folded in on each grant
// (recordInteractionGrantLocked), reconciled to actuals on settle. It is the value
// the bounded close (PR 4b) compares against [SynthesisSoftBudgetTokens] to decide
// when to synthesize-and-close. An untracked id (never seen, uncapped, already
// evicted, or the empty id) reads as zero. Takes w.mu for the map read, the same
// lock the grant/finalize helpers in interaction_budget.go hold.
func (w *WalletService) InteractionSpend(interactionID string) int64 {
	if interactionID == "" {
		return 0
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.interactionTokens[interactionID]
}

// EvictInteraction drops interactionID's running-total entry from the wallet, the
// interaction-lifecycle eviction the shipped wallet lacks: recordInteractionGrantLocked
// documents that a capped interaction that settled non-zero spend "keeps its entry
// for the orchestrator's process lifetime — nothing currently evicts it", so a
// standing autonomous channel would leak one map entry per convening (RFC 0052
// PR 7). The bounded close (PR 4b) calls this on interaction close — AFTER the
// close path's leases have settled — so the entry is released once the interaction
// is done. Returns whether an entry was removed (idempotent: a second call, an
// untracked id, or the empty id is a no-op returning false). Takes w.mu, the same
// lock the grant/finalize helpers hold.
func (w *WalletService) EvictInteraction(interactionID string) bool {
	if interactionID == "" {
		return false
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if _, ok := w.interactionTokens[interactionID]; !ok {
		return false
	}
	delete(w.interactionTokens, interactionID)
	return true
}
