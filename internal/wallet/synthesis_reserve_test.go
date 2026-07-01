// Tests for RFC 0052 (v0.3.11) PR 4a — the roster-scaled synthesis reserve and
// the interaction-closed eviction. The reserve is new wallet accounting with no
// shipped analog: it carves a per-interaction cost cap into a working budget that
// bounds the discussion (the soft threshold) and a reserve held back for the
// bounded close path — the chair synthesis turn plus one RFC 0020 summary per
// participating persona (OQ #6 meters the summary, and the summary is authored
// per-agent), so the reserve is sized for 1 + N close-path LLM calls, NOT a fixed
// two. The eviction releases the running-total residue the shipped wallet never
// prunes for a capped interaction that settled non-zero spend
// (interaction_budget.go "nothing currently evicts it").
//
// PR 4a ships these DARK: no bounded-close path consults them yet (that is PR 4b),
// so these tests exercise the accounting in isolation against real lease state.
package wallet

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSynthesisReserveTokens_ScalesWithRoster is the headline 1+N invariant: the
// reserve is sized for one chair synthesis turn plus one summary per persona, so a
// larger roster holds back proportionally more. A fixed-two reserve (the framing
// PR 4 explicitly rejects) would deny all but one persona's summary on close.
func TestSynthesisReserveTokens_ScalesWithRoster(t *testing.T) {
	// A budget far larger than any reserve so the half-cap clamp never bites and
	// the raw (1+N) sizing is what is under test.
	const budget = int64(10_000_000)
	unit := DefaultSynthesisCallReserveTokens

	// N=0 (lone roster) reserves only the chair turn: 1 call.
	assert.Equal(t, unit, SynthesisReserveTokens(budget, 0))
	// N=1 reserves chair + 1 summary: 2 calls.
	assert.Equal(t, 2*unit, SynthesisReserveTokens(budget, 1))
	// N=3 reserves chair + 3 summaries: 4 calls.
	assert.Equal(t, 4*unit, SynthesisReserveTokens(budget, 3))

	// Monotonic non-decreasing in roster size.
	prev := int64(-1)
	for n := 0; n <= 8; n++ {
		got := SynthesisReserveTokens(budget, n)
		assert.GreaterOrEqual(t, got, prev, "reserve must not shrink as the roster grows")
		prev = got
	}

	// A negative roster size is clamped to zero (the chair-only reserve), never a
	// negative or under-sized reserve.
	assert.Equal(t, unit, SynthesisReserveTokens(budget, -5))
}

// TestSynthesisReserveTokens_UncappedIsZero: an uncapped interaction (budget <= 0)
// has no ceiling to carve, so there is nothing to hold back.
func TestSynthesisReserveTokens_UncappedIsZero(t *testing.T) {
	assert.Zero(t, SynthesisReserveTokens(0, 4))
	assert.Zero(t, SynthesisReserveTokens(-1, 4))
}

// TestSynthesisReserveTokens_ClampedSoDiscussionSurvives: a tiny cap where the raw
// (1+N) reserve would swallow most/all of the budget is clamped so the discussion
// always retains a positive working budget — a small cap cannot starve the
// conversation to nothing to fund the close path. The soft threshold stays > 0.
func TestSynthesisReserveTokens_ClampedSoDiscussionSurvives(t *testing.T) {
	// Pick a budget smaller than the raw reserve for a 4-call close path.
	raw := 4 * DefaultSynthesisCallReserveTokens
	budget := raw - 1 // raw reserve would exceed the cap

	reserve := SynthesisReserveTokens(budget, 3)
	soft := SynthesisSoftBudgetTokens(budget, 3)

	assert.Less(t, reserve, budget, "the reserve can never be the whole cap")
	assert.Positive(t, soft, "the discussion always keeps a positive working budget")
	assert.Equal(t, budget, reserve+soft, "reserve + soft budget reconstitute the cap")
	assert.LessOrEqual(t, reserve, budget/2, "the reserve is clamped to at most half the cap")
}

// TestSynthesisReserveTokens_ClampCanUnderfundClose pins a KNOWN, tracked gap (see
// the package doc's KNOWN GAP note): the half-cap clamp protects the discussion, not
// the close, and it does not scale down with roster size. So a REALISTIC config — a
// full-size roster against a merely "modest" cap, not an extreme/tiny one — can still
// clamp the reserve below what the 1+N sizing says the close path needs. This does
// NOT assert a fix (none exists yet; it is PR 4b / OQ #5 calibration territory) — it
// documents the tradeoff so a future change to the clamp or the default unit is forced
// to consciously re-examine this case rather than silently shift it.
func TestSynthesisReserveTokens_ClampCanUnderfundClose(t *testing.T) {
	// 20 mirrors internal/channels.DefaultSalienceMaxChannelMembers — a full,
	// realistic roster, not a contrived edge case. 100_000 is a "modest" cap, well
	// above the tiny-cap regime the clamp doc-comment's rationale is framed around.
	const fullRoster = 20
	const modestBudget = int64(100_000)

	raw := int64(1+fullRoster) * DefaultSynthesisCallReserveTokens // what 1+N calls for
	reserve := SynthesisReserveTokens(modestBudget, fullRoster)

	require.Greater(t, raw, modestBudget/2, "fixture must actually exercise the clamp")
	assert.Less(t, reserve, raw,
		"a full roster against a modest cap clamps the reserve below the 1+N sizing — "+
			"the close path, not just a tiny/degenerate cap, can be under-funded")
}

// TestSynthesisSoftBudgetTokens_IsCapMinusReserve pins the soft threshold as the
// exact complement of the reserve, and uncapped as no threshold (0).
func TestSynthesisSoftBudgetTokens_IsCapMinusReserve(t *testing.T) {
	const budget = int64(200_000)
	assert.Equal(t, budget-SynthesisReserveTokens(budget, 3),
		SynthesisSoftBudgetTokens(budget, 3))
	assert.Zero(t, SynthesisSoftBudgetTokens(0, 3), "uncapped has no soft threshold")
}

// TestInteractionSpend_TracksRunningTotal: InteractionSpend reads the per-
// interaction running token total the wallet folds in on each grant — the value
// the bounded close (PR 4b) compares against the soft threshold. An untracked id
// (never seen, uncapped, or the empty id) reads as zero.
func TestInteractionSpend_TracksRunningTotal(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())

	require.NotNil(t, acquireForInteraction(t, w, "int-1", 100_000, 1000, 1000).GetGrant())
	assert.Equal(t, int64(2000), w.InteractionSpend("int-1"), "one 1000/1000 grant folds 2000")

	require.NotNil(t, acquireForInteraction(t, w, "int-1", 100_000, 500, 500).GetGrant())
	assert.Equal(t, int64(3000), w.InteractionSpend("int-1"), "a second grant accumulates")

	assert.Zero(t, w.InteractionSpend("never-seen"), "an untracked interaction reads zero")
	assert.Zero(t, w.InteractionSpend(""), "the empty id is never tracked")
}

// TestEvictInteraction_DropsResidue: the bounded close (PR 4b) calls
// EvictInteraction after the close path settles to release the residue the shipped
// wallet never prunes for a capped interaction that settled non-zero spend.
// Idempotent and empty-id-safe.
func TestEvictInteraction_DropsResidue(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())

	require.NotNil(t, acquireForInteraction(t, w, "int-1", 100_000, 1000, 1000).GetGrant())
	require.Equal(t, int64(2000), w.InteractionSpend("int-1"))

	assert.True(t, w.EvictInteraction("int-1"), "evicting a tracked interaction removes its entry")
	assert.Zero(t, w.InteractionSpend("int-1"), "the running total is gone after eviction")

	w.mu.Lock()
	residual := len(w.interactionTokens)
	w.mu.Unlock()
	assert.Zero(t, residual, "no residue leaks after the interaction closes")

	assert.False(t, w.EvictInteraction("int-1"), "a second evict is an idempotent no-op")
	assert.False(t, w.EvictInteraction(""), "the empty id never evicts")
}

// TestEvictInteraction_ResetsCeilingForReuse is a belt-and-braces check that the
// eviction fully releases the ceiling accounting: a fresh interaction reusing the
// id after a close starts its running total from zero (it does not inherit the
// closed interaction's spend).
func TestEvictInteraction_ResetsCeilingForReuse(t *testing.T) {
	w, _ := newTestWallet(t, testCostConfig(), interactionBudgetWalletCfg())

	require.NotNil(t, acquireForInteraction(t, w, "int-1", 4000, 1000, 1000).GetGrant()) // 2000 of 4000
	require.NotNil(t, w.EvictInteraction("int-1"))

	// Same id, fresh interaction: a 2000 lease grants because the prior 2000 was
	// evicted on close, not carried forward into the next convening.
	require.NotNil(t, acquireForInteraction(t, w, "int-1", 4000, 1000, 1000).GetGrant(),
		"a reused id starts fresh after eviction")
	assert.Equal(t, int64(2000), w.InteractionSpend("int-1"))
}
