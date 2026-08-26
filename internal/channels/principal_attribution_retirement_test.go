package channels

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// principal_attribution_retirement_test.go — ISSUE-0124 (R-2): how stimuli
// LEAVE the table. Split from principal_attribution_test.go when the PR 2
// review's crossover gates pushed that file past the 500-line review cap (the
// router_publish_async.go precedent); the write/verdict rules and the shared
// test helpers (the hand-wound clock) stay there.
//
// One rule owns this file: the consuming read is the table's only retirement
// — an agent that publishes has answered whatever it held. Expiry
// DISQUALIFIES a stimulus from resolving but never removes it, because an
// unanswered stimulus may be exactly what a slow agent is still working on
// (the crossover rule in principal_attribution.go), and the sweep reclaims
// only whole rows gone cold — every stimulus two turn budgets old.

// TestPrincipalAttribution_TakeAttributionRetiresTheStimuliItAnswered pins the
// consuming read. An agent that publishes has answered whatever it was
// holding, so the read that re-stamps its reply must retire those stimuli
// rather than leave them to compete with the NEXT one.
func TestPrincipalAttribution_TakeAttributionRetiresTheStimuliItAnswered(t *testing.T) {
	table, _ := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")

	got, ok := table.TakeAttribution("group:planning", "iron-fox")
	require.True(t, ok, "a live unambiguous entry must resolve for the re-stamp")
	assert.Equal(t, "alice-person", got)

	assert.Equal(t, 0, table.len(), "the stimuli the reply answered must be retired")
	_, ok = table.TakeAttribution("group:planning", "iron-fox")
	assert.False(t, ok, "a second reply answers nothing the orchestrator can name")
}

// TestPrincipalAttribution_TakeAttributionRetiresAnAmbiguousRow pins that the
// retirement is unconditional. The agent spoke, so its stimuli are spent
// whether or not the orchestrator could name who caused them — leaving an
// ambiguous row behind is what would let it go on ambiguating later replies.
func TestPrincipalAttribution_TakeAttributionRetiresAnAmbiguousRow(t *testing.T) {
	table, _ := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	table.Record("group:planning", "iron-fox", "bob-person")

	_, ok := table.TakeAttribution("group:planning", "iron-fox")
	require.False(t, ok, "two live principals resolve nothing")
	assert.Equal(t, 0, table.len(), "an ambiguous row is still answered and still retired")
}

// TestPrincipalAttribution_LookupDoesNotRetire pins the other half: the
// non-consuming read is safe for tests and observability, so asking what the
// table holds cannot change the answer the next reply gets.
func TestPrincipalAttribution_LookupDoesNotRetire(t *testing.T) {
	table, _ := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")

	for range 3 {
		got, ok := table.Lookup("group:planning", "iron-fox")
		require.True(t, ok, "a plain read must not consume the entry")
		assert.Equal(t, "alice-person", got)
	}
	assert.Equal(t, 1, table.len(), "a plain read leaves the row in place")
}

// TestPrincipalAttribution_ExpiredEntryIsAMiss pins the TTL: past the
// persona's worst realistic turn the stimulus can no longer be vouched for as
// the cause of the reply in hand, so the reply degrades to `'local'`.
func TestPrincipalAttribution_ExpiredEntryIsAMiss(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	clock.advance(principalAttributionTTL)

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "an entry at or past the TTL must not resolve")
}

// TestPrincipalAttribution_ExpiredStimulusStillBlocksAFreshOne is the
// regression gate for the expiry-crossover mis-attribution (PR 2 review,
// finding 1).
//
// The delivery ack is pre-ingest, so an agent can hold a stimulus past the
// turn budget while still being about to answer it — queue backlog and LLM
// retries are not bounded by the budget the TTL was sized on. If expiry
// silently REMOVES that stimulus, a fresh stimulus from someone else survives
// alone and the late reply resolves to the wrong person: Alice's relayed
// content lands in Bob's tenant. Expiry may only ever cost a MISS, so an
// expired, unanswered stimulus keeps blocking resolution until the agent
// speaks — only the consuming read retires candidates.
func TestPrincipalAttribution_ExpiredStimulusStillBlocksAFreshOne(t *testing.T) {
	table, clock := newTestAttributionTable()

	// Alice's stimulus; the agent's turn runs long.
	table.Record("group:planning", "iron-fox", "alice-person")

	// Bob publishes just inside Alice's budget...
	clock.advance(principalAttributionTTL - time.Second)
	table.Record("group:planning", "iron-fox", "bob-person")

	// ...and the agent's reply lands just past it. Alice's stimulus has
	// expired, Bob's is live and alone — but the reply may be answering
	// Alice, so it must resolve nothing.
	clock.advance(5 * time.Second)
	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "an expired, unanswered stimulus must keep blocking a fresh one")

	got, ok := table.TakeAttribution("group:planning", "iron-fox")
	assert.False(t, ok, "the consuming read must fail closed on the same crossover")
	assert.Empty(t, got)

	// The reply consumed everything, so the pair recovers immediately: Bob's
	// next stimulus stands alone and his next reply is his.
	table.Record("group:planning", "iron-fox", "bob-person")
	got, ok = table.TakeAttribution("group:planning", "iron-fox")
	require.True(t, ok, "recovery is the consuming read's job, not expiry's")
	assert.Equal(t, "bob-person", got)
}

// TestPrincipalAttribution_ExpiredStimulusBlocksARestartedRow pins the same
// rule on the write path. A row whose every stimulus has aged out is NOT
// "indistinguishable from no row at all" — the agent may still be mid-turn on
// what it holds — so a fresh write must join the ghosts rather than start a
// clean single-stimulus row for the late reply to resolve.
func TestPrincipalAttribution_ExpiredStimulusBlocksARestartedRow(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")

	// Everything the agent holds ages out, then Bob's stimulus arrives.
	clock.advance(principalAttributionTTL)
	table.Record("group:planning", "iron-fox", "bob-person")

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "a write onto an all-expired row must not restart it as unambiguous")

	_, ok = table.TakeAttribution("group:planning", "iron-fox")
	assert.False(t, ok, "the late reply may be answering the expired stimulus")
}

// TestPrincipalAttribution_ExpiredAmbiguityDoesNotStartFresh pins the other
// half of the stickiness rule, and the crossover rule with it: ambiguity does
// NOT die with the clock, because the agent may still be mid-turn on a
// stimulus that has aged out (the ack is pre-ingest). A write onto the aged
// pair joins the ghosts rather than starting a fresh unambiguous fact. What
// un-latches the room is the agent's own reply: the consuming read retires
// everything it was holding, and the next stimulus stands alone.
func TestPrincipalAttribution_ExpiredAmbiguityDoesNotStartFresh(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	table.Record("group:planning", "iron-fox", "bob-person")
	clock.advance(principalAttributionTTL)
	table.Record("group:planning", "iron-fox", "alice-person")

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "unanswered stimuli must keep ambiguating past their budget")

	// The agent replies — everything it held is spent — and Alice speaks
	// again. Now hers is the only stimulus outstanding.
	_, ok = table.TakeAttribution("group:planning", "iron-fox")
	require.False(t, ok, "the ghosted pair itself resolves nothing")
	table.Record("group:planning", "iron-fox", "alice-person")

	got, ok := table.Lookup("group:planning", "iron-fox")
	require.True(t, ok, "the reply is what un-latches the room, not the clock")
	assert.Equal(t, "alice-person", got)
}

// TestPrincipalAttribution_SecondStimulusBlocksUntilTheAgentSpeaks pins that
// ambiguity is a property of the stimuli the agent still HOLDS, not a flag a
// room carries once it has been earned — and that holding does not end with
// the clock. Bob speaks once; Alice keeps the room busy past Bob's turn
// budget. Bob's unanswered stimulus keeps blocking however stale it gets (it
// may be exactly what the slow agent is answering — the crossover rule); the
// pair recovers the moment the agent replies, because the consuming read
// retires everything it was holding. The earlier sticky-FLAG shape was still
// worse: refreshed by every write, it pinned an active room to `'local'` for
// as long as the conversation lasted, with no reply able to clear it.
func TestPrincipalAttribution_SecondStimulusBlocksUntilTheAgentSpeaks(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	table.Record("group:planning", "iron-fox", "bob-person")
	_, ok := table.Lookup("group:planning", "iron-fox")
	require.False(t, ok, "both stimuli live — the reply may be answering either")

	// Alice keeps dispatching, more often than the TTL, right past the point
	// where Bob's single stimulus could still be vouched for.
	for range 5 {
		clock.advance(principalAttributionTTL / 2)
		table.Record("group:planning", "iron-fox", "alice-person")
	}

	_, ok = table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "Bob's unanswered stimulus must keep blocking, however stale")

	// The agent replies (retiring Bob's ghost with everything else), and
	// Alice speaks again: the busy room recovers on the reply, not the clock.
	_, _ = table.TakeAttribution("group:planning", "iron-fox")
	table.Record("group:planning", "iron-fox", "alice-person")

	got, ok := table.Lookup("group:planning", "iron-fox")
	require.True(t, ok, "a busy room must recover once the agent has answered what it held")
	assert.Equal(t, "alice-person", got)
}

// TestPrincipalAttribution_UnauthenticatedStimulusBlocksUntilAnswered pins
// that the anonymous stimulus obeys the same crossover rule as any other:
// expiry disqualifies it from ever being an answer (it never could be one),
// but does not remove it — an unanswered forced turn may be exactly what the
// slow agent is working on, so the authenticated stimulus beside it must not
// resolve just because the clock ran. The agent's own reply is what clears
// it.
func TestPrincipalAttribution_UnauthenticatedStimulusBlocksUntilAnswered(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	table.Record("group:planning", "iron-fox", "")
	_, ok := table.Lookup("group:planning", "iron-fox")
	require.False(t, ok)

	// Keep Alice's stimulus alive across the anonymous one's expiry.
	clock.advance(principalAttributionTTL - time.Second)
	table.Record("group:planning", "iron-fox", "alice-person")
	clock.advance(2 * time.Second)

	_, ok = table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "an expired forced turn still blocks the stimulus that outlived it")

	// The reply retires both; Alice's next stimulus stands alone.
	_, _ = table.TakeAttribution("group:planning", "iron-fox")
	table.Record("group:planning", "iron-fox", "alice-person")

	got, ok := table.Lookup("group:planning", "iron-fox")
	require.True(t, ok, "recovery is the consuming read's job, not expiry's")
	assert.Equal(t, "alice-person", got)
}

// TestPrincipalAttribution_LookupLeavesTheExpiredEntryInPlace pins that the
// non-consuming read is PURE even on a miss. The expired stimulus is
// load-bearing — it must go on blocking a fresher one until the agent speaks
// (the crossover rule) — so a read that misses on age must not evict it. The
// consuming read is what retires the row; the sweep reclaims it if the agent
// never speaks.
func TestPrincipalAttribution_LookupLeavesTheExpiredEntryInPlace(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	require.Equal(t, 1, table.len())

	clock.advance(principalAttributionTTL)
	_, ok := table.Lookup("group:planning", "iron-fox")
	require.False(t, ok)
	assert.Equal(t, 1, table.len(),
		"the expired stimulus must survive the read: it still blocks a fresh one")

	_, ok = table.TakeAttribution("group:planning", "iron-fox")
	require.False(t, ok)
	assert.Equal(t, 0, table.len(), "the consuming read is what retires the row")
}

// TestPrincipalAttribution_SweepReclaimsColdRowsOnly pins the eager half and
// its horizon. The consuming read only retires pairs whose agent publishes —
// an agent dispatched to in a room it never speaks in would otherwise hold
// its row for the life of the process, so the sweep reclaims it. But not at
// the resolve TTL: an expired stimulus may still be the one the agent is
// grinding on, and its blocking work is the crossover fix — so a row is
// reclaimed only once everything in it is a further full turn budget past
// its expiry (age >= 2×TTL), with the agent silent throughout.
func TestPrincipalAttribution_SweepReclaimsColdRowsOnly(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:quiet", "silent-heron", "alice-person")
	require.Equal(t, 1, table.len())

	// One turn budget on: expired, but not yet cold. The piggybacking write
	// must leave the row in place — it is still blocking.
	clock.advance(principalAttributionTTL)
	table.Record("group:planning", "iron-fox", "alice-person")
	assert.Equal(t, 2, table.len(),
		"an expired-but-not-cold row must survive the sweep: the agent may still answer it")

	// A second turn budget on: cold. The next piggybacked sweep reclaims it.
	clock.advance(principalAttributionTTL)
	table.Record("group:planning", "iron-fox", "alice-person")
	assert.Equal(t, 1, table.len(), "the sweep must drop the cold row it was never asked about")
	_, ok := table.Lookup("group:quiet", "silent-heron")
	assert.False(t, ok, "the cold row is gone, not merely unreadable")
	_, ok = table.Lookup("group:planning", "iron-fox")
	assert.True(t, ok, "the sweep must not touch a live entry")
}

// TestPrincipalAttribution_UnauthenticatedDispatchStillSweeps pins a property
// the anonymous-stimulus rule carries with it. The eager sweep is piggybacked
// on the write path, so it only runs from a call that gets that far — and
// while an empty principal was an early return, a deployment whose
// authenticated traffic stopped (agent-origin and autonomous turns continuing
// alone) would never sweep again, exactly when nothing else reclaims the rows.
func TestPrincipalAttribution_UnauthenticatedDispatchStillSweeps(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:quiet", "silent-heron", "alice-person")
	require.Equal(t, 1, table.len())

	// Authenticated traffic stops here; only agent-origin turns continue. Two
	// turn budgets on, the abandoned row is cold (the reclaim horizon — see
	// [TestPrincipalAttribution_SweepReclaimsColdRowsOnly]).
	clock.advance(2 * principalAttributionTTL)
	table.Record("group:planning", "iron-fox", "")

	// The cold row is reclaimed; what remains is the anonymous dispatch's
	// own row, which resolves nothing.
	assert.Equal(t, 1, table.len(),
		"an unauthenticated dispatch must still reclaim the rows that went cold")
	_, ok := table.Lookup("group:quiet", "silent-heron")
	assert.False(t, ok, "the cold authenticated row is gone, not merely unreadable")
	_, ok = table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "and the row it left behind can never be an answer")
}

// TestPrincipalAttribution_CadenceRoomRecoversWhenTheAgentSpeaks is the
// regression gate for the permanent-ambiguity hole.
//
// A room on the RFC 0052 convener cadence dispatches principal-less forced
// turns to the same agent faster than the turn budget retires them, so under
// expiry alone an authenticated stimulus is ambiguous on arrival and STAYS
// ambiguous for as long as the cadence runs — the pair never resolves and the
// row is never reclaimable, because something is always live in it. The agent
// answering its stimuli is what breaks the accumulation.
func TestPrincipalAttribution_CadenceRoomRecoversWhenTheAgentSpeaks(t *testing.T) {
	table, clock := newTestAttributionTable()

	// The cadence ticks well inside the turn budget, so the anonymous stimulus
	// is restated before it can ever age out.
	tick := principalAttributionTTL / 2

	clock.advance(tick)
	table.Record("group:planning", "iron-fox", "")
	clock.advance(tick)
	table.Record("group:planning", "iron-fox", "")

	// Alice speaks into the running cadence. Correctly ambiguous: the agent is
	// holding her message AND an unanswered forced turn.
	clock.advance(time.Second)
	table.Record("group:planning", "iron-fox", "alice-person")
	_, ok := table.TakeAttribution("group:planning", "iron-fox")
	require.False(t, ok, "an unanswered forced turn racing Alice's message is ambiguous")

	// That reply retired both. The cadence ticks on, the agent answers it, and
	// Alice speaks again — now hers is the only stimulus outstanding.
	clock.advance(tick)
	table.Record("group:planning", "iron-fox", "")
	_, ok = table.TakeAttribution("group:planning", "iron-fox")
	require.False(t, ok, "the forced turn alone can never be an answer")

	clock.advance(time.Second)
	table.Record("group:planning", "iron-fox", "alice-person")

	got, ok := table.TakeAttribution("group:planning", "iron-fox")
	require.True(t, ok,
		"a cadence room must recover once the agent has answered what it was holding")
	assert.Equal(t, "alice-person", got)
	assert.Equal(t, 0, table.len(), "and the row does not outlive the reply it explained")
}
