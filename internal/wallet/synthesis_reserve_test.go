// Tests for RFC 0052 (v0.3.11) PR 4a — the record-scaled synthesis reserve and
// the interaction-closed eviction. The reserve is new wallet accounting with no
// shipped analog: it carves a per-interaction cost cap into a working budget that
// bounds the discussion (the soft threshold) and a reserve held back for the
// bounded close path — the chair synthesis turn plus one RFC 0020 summary per
// close-derived record (OQ #6 meters the summary, and the summary is authored
// per-agent), so the reserve is sized for 1 + R close-path LLM calls, NOT a fixed
// two. The eviction releases the running-total residue the shipped wallet never
// prunes for a capped interaction that settled non-zero spend
// (interaction_budget.go "nothing currently evicts it").
//
// R WAS the persona roster N. ISSUE-0082 residuals PR 4b (v0.3.15) re-sized it to
// the CLOSE-RECORD count, because the `(principal, speaker, scope)` re-key made a
// room hold one record — and issue one metered summary — per persona per
// `(principal, speaker)` pair. The multiplier's own tests are below
// (CloseRecordUpperBound); the reserve tests keep passing small literal counts,
// which is now "R records" rather than "N personas" and exercises the same
// arithmetic either way.
//
// PR 4a ships these DARK: no bounded-close path consults them yet (that is PR 4b),
// so these tests exercise the accounting in isolation against real lease state.
package wallet

import (
	"math"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSynthesisReserveTokens_ScalesWithCloseRecords is the headline `1 + R`
// invariant: the reserve is sized for one chair synthesis turn plus one summary
// per close-derived RECORD, so a room that closes more records holds back
// proportionally more. A fixed-two reserve (the framing PR 4 explicitly rejects)
// would deny all but one record's summary on close. The arithmetic here is
// per-RECORD — `CloseRecordUpperBound` is what turns a room into that count, and
// it has its own tests below.
func TestSynthesisReserveTokens_ScalesWithCloseRecords(t *testing.T) {
	// A budget far larger than any reserve so the half-cap clamp never bites and
	// the raw (1+R) sizing is what is under test.
	const budget = int64(10_000_000)
	unit := DefaultSynthesisCallReserveTokens

	// R=0 (nothing to close) reserves only the chair turn: 1 call.
	assert.Equal(t, unit, SynthesisReserveTokens(budget, 0))
	// R=1 reserves chair + 1 summary: 2 calls.
	assert.Equal(t, 2*unit, SynthesisReserveTokens(budget, 1))
	// R=3 reserves chair + 3 summaries: 4 calls.
	assert.Equal(t, 4*unit, SynthesisReserveTokens(budget, 3))

	// Monotonic non-decreasing in the record count.
	prev := int64(-1)
	for n := 0; n <= 8; n++ {
		got := SynthesisReserveTokens(budget, n)
		assert.GreaterOrEqual(t, got, prev, "reserve must not shrink as the record count grows")
		prev = got
	}

	// A negative record count is clamped to zero (the chair-only reserve), never
	// a negative or under-sized reserve.
	assert.Equal(t, unit, SynthesisReserveTokens(budget, -5))
}

// TestSynthesisReserveTokens_UncappedIsZero: an uncapped interaction (budget <= 0)
// has no ceiling to carve, so there is nothing to hold back.
func TestSynthesisReserveTokens_UncappedIsZero(t *testing.T) {
	assert.Zero(t, SynthesisReserveTokens(0, 4))
	assert.Zero(t, SynthesisReserveTokens(-1, 4))
}

// TestSynthesisReserveTokens_ClampedSoDiscussionSurvives: a tiny cap where the raw
// (1+R) reserve would swallow most/all of the budget is clamped so the discussion
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
// the close, and it does not scale down with the record count. So a REALISTIC config
// can still clamp the reserve below what the 1+R sizing says the close path needs.
// This does NOT assert a fix (none exists; it is ISSUE-0138 calibration territory) —
// it documents the tradeoff so a future change to the clamp or the default unit is
// forced to consciously re-examine this case rather than silently shift it.
//
// The fixture is the SHIPPED configuration, which is what the v0.3.15 re-size
// changed about this gap. It used to take a full 20-seat roster against a modest
// cap to clamp; it now takes the bundled `blueprints/autonomous-multivendor`
// roster at the `interaction_budget_tokens` that same blueprint ships. Passing 20
// here after the re-size would have kept passing while quietly testing a ~3-seat
// room, since the second argument is now R and not a member count.
func TestSynthesisReserveTokens_ClampCanUnderfundClose(t *testing.T) {
	// blueprints/autonomous-multivendor: four seats, interaction_budget_tokens
	// 200 000. Not a contrived edge case — it is what `make demo-autonomous`
	// boots.
	const bundledRoster = 4
	const bundledBudget = int64(200_000)

	records := CloseRecordUpperBound(bundledRoster)
	raw := int64(1+records) * DefaultSynthesisCallReserveTokens // what 1+R calls for
	reserve := SynthesisReserveTokens(bundledBudget, records)

	require.Greater(t, raw, bundledBudget/2,
		"fixture must actually exercise the clamp — R=%d on a %d-seat room", records, bundledRoster)
	assert.Less(t, reserve, raw,
		"the bundled four-seat roster at its own shipped cap clamps the reserve below "+
			"the 1+R sizing — the close path, not just a tiny/degenerate cap, is under-funded")
	assert.True(t, SynthesisReserveClamped(bundledBudget, records),
		"and the signal must say so: this is the config an operator meets first")
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

// ─── ISSUE-0082 residuals PR 4b — the v0.3.15 re-size ────────────────

// TestCloseRecordUpperBound_IsThePartitionMaximum pins the multiplier against
// the definition rather than against its closed form. R is
// `personas × principals × speakers` where the personas and the
// principal-bearing people PARTITION the roster (a tenant enters a room only
// when an authenticated person publishes) plus the shared `local` bucket, and
// the speaker axis is the members PLUS the orchestrator's synthetic control
// senders — so the true bound is the maximum over every partition point, and
// the ⌊(c+1)²/4⌋ × (c + ControlSenderSpeakers) the implementation computes is
// only a closed form for it. Brute-forcing the definition is what makes an
// algebra slip fail here instead of silently under-sizing every reserve in the
// fleet.
func TestCloseRecordUpperBound_IsThePartitionMaximum(t *testing.T) {
	for c := 1; c <= 64; c++ {
		speakers := c + ControlSenderSpeakers // members + orchestrator:{convene,synthesis}
		want := 0
		for personas := 0; personas <= c; personas++ {
			principals := (c - personas) + 1 // +1: the shared `local` bucket.
			if r := personas * principals * speakers; r > want {
				want = r
			}
		}
		assert.Equal(t, want, CloseRecordUpperBound(c), "channelSize=%d", c)
	}
}

// TestCloseRecordUpperBound_CountsTheOrchestratorControlSenders is the review
// finding this term exists for: the record key's speaker half is the ingested
// event's `sender_id`, and the two forced-turn directives the orchestrator
// authors ride SYNTHETIC senders that hold no seat — so a bound derived from
// the member count alone is not a bound.
//
// The three-seat room is where that bites, because it has ZERO slack: one
// authenticated operator plus two personas puts the partition exactly at its
// maximum (2 personas × 2 principals = 4 = ⌊4²/4⌋), so every member-speaker
// pair is already spoken for and the convener's `orchestrator:convene` record
// and the chair's `orchestrator:synthesis` record have nowhere to go. Stated as
// the inequality rather than as 20, so the pin survives a unit change.
func TestCloseRecordUpperBound_CountsTheOrchestratorControlSenders(t *testing.T) {
	require.Positive(t, ControlSenderSpeakers,
		"the orchestrator authors directives under senders that are not members")
	for c := 1; c <= 32; c++ {
		memberSpeakersOnly := (((c + 1) * (c + 1)) / 4) * c
		assert.Greater(t, CloseRecordUpperBound(c), memberSpeakersOnly,
			"a %d-seat room's close records are not bounded by its member speakers alone: "+
				"the convene and synthesis directives key records of their own", c)
	}
}

// TestCloseRecordUpperBound_ExceedsThePersonaRoster is the under-sizing the
// re-size closes, stated as an inequality rather than a number: for every room
// bigger than a pair, one close issues strictly more summaries than the persona
// count `1 + N` was sized for. A change that quietly reverted the multiplier to
// a roster count would satisfy every other test in this file.
func TestCloseRecordUpperBound_ExceedsThePersonaRoster(t *testing.T) {
	for c := 3; c <= 32; c++ {
		assert.Greater(t, CloseRecordUpperBound(c), c,
			"a %d-seat room closes more records than it has members", c)
	}
	// The degenerate rooms stay degenerate: nobody to close, nothing to reserve.
	assert.Zero(t, CloseRecordUpperBound(0))
	assert.Zero(t, CloseRecordUpperBound(-3))
	// A one-seat room holds one record per speaker it can hear: itself plus the
	// two control senders, under one tenant.
	assert.Equal(t, 1+ControlSenderSpeakers, CloseRecordUpperBound(1))
}

// TestCloseRecordUpperBound_SaturatesWithoutOverflowing: the bound is cubic in
// the member count, so an absurd roster must saturate rather than wrap the int64
// multiply inside SynthesisReserveTokens. The reserve is long since clamped to
// half the cap at these sizes, so the saturation must not be observable as a
// SMALLER reserve — which is exactly what a wrap would produce.
//
// The saturation must ALSO fit a 32-bit `int`, which is not a portability nicety
// but a compile gate: [CloseRecordUpperBound] returns `int`, so a `maxCloseRecords`
// past math.MaxInt32 fails `GOARCH=386 go build ./internal/wallet` outright
// (the shape this test's math.MaxInt32 assertion was added to keep out). A
// constant-overflow build break is invisible to a 64-bit-only test run, so the
// bound is asserted here rather than left to whoever next cross-compiles.
func TestCloseRecordUpperBound_SaturatesWithoutOverflowing(t *testing.T) {
	const budget = int64(200_000)
	huge := CloseRecordUpperBound(1 << 30)
	assert.Positive(t, huge, "an absurd roster must not wrap to a negative bound")
	assert.LessOrEqual(t, int64(huge), int64(math.MaxInt32),
		"the saturation must fit a 32-bit int — CloseRecordUpperBound returns `int`, "+
			"so a larger constant is a compile error on 32-bit GOARCH, not a runtime bug")
	assert.Equal(t, budget/2, SynthesisReserveTokens(budget, huge),
		"an absurd roster clamps to half the cap, never to a wrapped-small reserve")
	assert.True(t, SynthesisReserveClamped(budget, huge))
	// Monotonic across the saturation seam: the last computed value and the
	// saturated one must agree, or the bound dips as the room grows.
	assert.Equal(t, maxCloseRecords, CloseRecordUpperBound(maxCloseRecordChannelSize),
		"the saturation IS the bound at its own channel size — no step at the seam")
}

// TestSynthesisReserveTokens_SizedForRecordsNotRoster is the behavioural half of
// the re-size on a realistic room: a 4-seat autonomous channel against a
// six-figure cap holds back strictly more than the `1 + N` sizing did, because
// the close now issues one summary per `(principal, speaker)` record per
// persona rather than one per persona.
func TestSynthesisReserveTokens_SizedForRecordsNotRoster(t *testing.T) {
	const budget = int64(1_000_000) // large enough that the clamp does not bite
	const channelSize = 4

	roster := SynthesisReserveTokens(budget, channelSize)                         // the old basis
	records := SynthesisReserveTokens(budget, CloseRecordUpperBound(channelSize)) // the new one

	require.False(t, SynthesisReserveClamped(budget, CloseRecordUpperBound(channelSize)),
		"fixture must exercise the raw sizing, not the clamp")
	assert.Greater(t, records, roster,
		"the re-key multiplied the close-path calls, so the reserve must grow with it")
	assert.Equal(t, int64(1+CloseRecordUpperBound(channelSize))*DefaultSynthesisCallReserveTokens, records)
}

// TestSynthesisReserveClamped_TracksTheReserveItDescribes pins the signal to the
// thing it signals. The predicate exists because the clamped close degrades
// SILENTLY — denied summary leases commit the janitor's unavailable placeholder
// — so a predicate that disagreed with the reserve would be worse than none:
// it would tell an operator the cap is fine while the close is starving.
func TestSynthesisReserveClamped_TracksTheReserveItDescribes(t *testing.T) {
	unit := DefaultSynthesisCallReserveTokens

	for _, budget := range []int64{1, unit, 4 * unit, 100_000, 1_000_000, 10_000_000} {
		for _, records := range []int{0, 1, 3, 12, 80, 500} {
			clamped := SynthesisReserveClamped(budget, records)
			reserve := SynthesisReserveTokens(budget, records)
			// Both public spellings are projections of the ONE evaluation, so
			// the pair function must agree with each of them by construction.
			pairReserve, pairClamped := SynthesisReserve(budget, records)
			assert.Equal(t, reserve, pairReserve,
				"SynthesisReserve's reserve must be SynthesisReserveTokens (budget=%d records=%d)", budget, records)
			assert.Equal(t, clamped, pairClamped,
				"SynthesisReserve's verdict must be SynthesisReserveClamped (budget=%d records=%d)", budget, records)
			raw := int64(1+records) * unit
			if clamped {
				assert.Equal(t, budget/2, reserve,
					"a clamped reserve IS the ceiling (budget=%d records=%d)", budget, records)
				assert.Less(t, reserve, raw,
					"clamped means the close path is under-funded relative to its sizing")
			} else {
				assert.Equal(t, raw, reserve,
					"an unclamped reserve IS the raw sizing (budget=%d records=%d)", budget, records)
			}
		}
	}

	// An uncapped interaction carves nothing, so it clamps nothing — reporting a
	// clamp there would warn about a close that is not funded from a cap at all.
	assert.False(t, SynthesisReserveClamped(0, 500))
	assert.False(t, SynthesisReserveClamped(-1, 500))
}
