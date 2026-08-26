package channels

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// ISSUE-0124 (R-2) PR 1 — the causal-attribution table, dormant.
//
// These tests are the table's ONLY readers in this PR: the re-stamp that
// consults it lands in PR 2. They pin the rules the re-stamp will depend on,
// each of which fails closed — an unattributable reply keeps resolving
// `'local'`, which is today's behaviour, so every degradation here is a
// no-regression rather than a wrong answer.
//
// The rules are all one rule seen from different sides: a pair resolves when
// exactly one stimulus to that agent is still live and it has a principal.
// Everything else — nothing live, two principals live, a principal racing an
// unauthenticated turn — resolves nothing.
//
// The clock is injected on every table so expiry is asserted deterministically
// rather than by sleeping (the activityNow pattern used across this package).

// fakeAttributionClock is a hand-wound clock for the TTL assertions.
type fakeAttributionClock struct{ t time.Time }

func (c *fakeAttributionClock) now() time.Time          { return c.t }
func (c *fakeAttributionClock) advance(d time.Duration) { c.t = c.t.Add(d) }

// newTestAttributionTable returns a table wound to a fixed instant, plus the
// clock so a test can advance it.
func newTestAttributionTable() (*PrincipalAttributionTable, *fakeAttributionClock) {
	clock := &fakeAttributionClock{t: time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)}
	table := NewPrincipalAttributionTable()
	table.now = clock.now
	return table, clock
}

// TestPrincipalAttribution_RecordsAndLooksUpOneDispatch pins the base case:
// one dispatch under one principal makes that principal readable for the
// `(channel, agent)` pair it was dispatched to, and for no other pair.
func TestPrincipalAttribution_RecordsAndLooksUpOneDispatch(t *testing.T) {
	table, _ := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")

	got, ok := table.Lookup("group:planning", "iron-fox")
	require.True(t, ok, "a live unambiguous entry must resolve")
	assert.Equal(t, "alice-person", got)

	_, ok = table.Lookup("group:planning", "nova-sparrow")
	assert.False(t, ok, "attribution is per-agent: a sibling recipient has its own entry")

	_, ok = table.Lookup("group:other", "iron-fox")
	assert.False(t, ok, "attribution is per-channel: the same agent in another room has its own entry")
}

// TestPrincipalAttribution_SamePrincipalRefreshes pins that a repeat dispatch
// under the SAME principal is not ambiguous — it restates the same true fact —
// and that it refreshes the entry's lifetime rather than leaving it to expire
// on the first dispatch's stamp.
func TestPrincipalAttribution_SamePrincipalRefreshes(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	clock.advance(principalAttributionTTL - time.Second)
	table.Record("group:planning", "iron-fox", "alice-person")

	// Past the FIRST dispatch's expiry, inside the second's.
	clock.advance(2 * time.Second)
	got, ok := table.Lookup("group:planning", "iron-fox")
	require.True(t, ok, "a same-principal re-dispatch must refresh the entry, not let it expire")
	assert.Equal(t, "alice-person", got)
}

// TestPrincipalAttribution_SecondPrincipalAmbiguates pins the edge case the
// design turns on: two people publishing into one channel both fan out to the
// same agent, so a last-write-wins table would attribute the agent's reply to
// whichever person spoke most recently. The entry is marked ambiguous instead
// and resolves nothing — the reply collapses to `'local'`.
func TestPrincipalAttribution_SecondPrincipalAmbiguates(t *testing.T) {
	table, _ := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	table.Record("group:planning", "iron-fox", "bob-person")

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "two live principals must not resolve to either of them")
}

// TestPrincipalAttribution_AmbiguityIsStickyWhileLive pins that ambiguity is
// not cleared by a later dispatch: once Alice's and Bob's stimuli are both in
// flight to one agent, a third dispatch from Alice does not make the agent's
// next reply Alice's — it may still be answering Bob.
//
// "Sticky" is a consequence here, not a mechanism: nothing is latched, Bob's
// stimulus is simply still live. Once it ages out the pair resolves again —
// see [TestPrincipalAttribution_AmbiguityDecaysWithTheSecondStimulus], which
// is the half a stored-and-refreshed flag got wrong.
func TestPrincipalAttribution_AmbiguityIsStickyWhileLive(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	table.Record("group:planning", "iron-fox", "bob-person")
	clock.advance(time.Second)
	table.Record("group:planning", "iron-fox", "alice-person")

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "a live ambiguous entry can never be disambiguated by a later dispatch")
}

// TestPrincipalAttribution_ExpiredEntryIsAMiss pins the TTL: past the
// persona's worst realistic turn the stimulus can no longer have caused the
// reply in hand, so the entry is gone and the reply degrades to `'local'`.
func TestPrincipalAttribution_ExpiredEntryIsAMiss(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	clock.advance(principalAttributionTTL)

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "an entry at or past the TTL must not resolve")
}

// TestPrincipalAttribution_ExpiredAmbiguityStartsFresh pins the other half of
// the stickiness rule: ambiguity dies with the stimuli that made it. Once both
// have aged out, the next dispatch is a fresh unambiguous fact — so a busy
// room does not latch itself into permanent `'local'`.
func TestPrincipalAttribution_ExpiredAmbiguityStartsFresh(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	table.Record("group:planning", "iron-fox", "bob-person")
	clock.advance(principalAttributionTTL)
	table.Record("group:planning", "iron-fox", "alice-person")

	got, ok := table.Lookup("group:planning", "iron-fox")
	require.True(t, ok, "ambiguity must not outlive the entry that carried it")
	assert.Equal(t, "alice-person", got)
}

// TestPrincipalAttribution_EmptyIdentifiersAreNotRecorded pins the STRUCTURAL
// write gate: a blank channel or agent id means a caller lost the id, not that
// something is unknown, so it writes nothing at all.
//
// The empty PRINCIPAL is deliberately not in this test. It is a different
// case — a real stimulus that cannot name anyone — and it IS recorded; see
// TestPrincipalAttribution_UnauthenticatedStimulusCreatesAnUnresolvableRow and
// TestPrincipalAttribution_AnonymousStimulusAmbiguatesALaterPrincipal.
func TestPrincipalAttribution_EmptyIdentifiersAreNotRecorded(t *testing.T) {
	table, _ := newTestAttributionTable()

	table.Record("group:planning", "", "alice-person")
	table.Record("", "iron-fox", "alice-person")

	assert.Equal(t, 0, table.len(), "no entry may be created from an empty channel or agent id")

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "a dispatch whose ids were lost resolves nothing")
}

// TestPrincipalAttribution_LookupEvictsTheExpiredEntry pins the lazy half of
// the expiry story: a read that misses on age also drops the row, so a table
// that is read at all does not need the sweep to stay bounded.
func TestPrincipalAttribution_LookupEvictsTheExpiredEntry(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	require.Equal(t, 1, table.len())

	clock.advance(principalAttributionTTL)
	_, ok := table.Lookup("group:planning", "iron-fox")
	require.False(t, ok)

	assert.Equal(t, 0, table.len(), "an expired entry must be evicted by the read that missed on it")
}

// TestPrincipalAttribution_SweepDropsUnreadExpiredEntries pins the eager half.
// Lazy expiry only reclaims pairs somebody looks up, and the re-stamp only
// looks up agents that publish — an agent dispatched to in a room it never
// speaks in would otherwise hold its row for the life of the process.
func TestPrincipalAttribution_SweepDropsUnreadExpiredEntries(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:quiet", "silent-heron", "alice-person")
	require.Equal(t, 1, table.len())

	// Age the entry out, then give the table a write it can piggyback the
	// sweep on. The swept row is never read.
	clock.advance(principalAttributionTTL)
	table.Record("group:planning", "iron-fox", "alice-person")

	assert.Equal(t, 1, table.len(), "the sweep must drop the expired row it was never asked about")
	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.True(t, ok, "the sweep must not touch a live entry")
}

// TestPrincipalAttribution_NilTableIsInert pins the nil receiver. The table is
// optional wiring (a channels-disabled deployment has no dispatcher to hold
// one), so both methods must be safe on a nil handle rather than making every
// call site guard.
func TestPrincipalAttribution_NilTableIsInert(t *testing.T) {
	var table *PrincipalAttributionTable

	assert.NotPanics(t, func() { table.Record("group:planning", "iron-fox", "alice-person") })

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "a nil table resolves nothing")
}

// TestPrincipalAttribution_AmbiguityDecaysWithTheSecondStimulus pins that
// ambiguity is a property of the stimuli that are LIVE, not a flag a room
// carries once it has been earned. Bob speaks once; Alice keeps the room
// busy past Bob's turn budget. The pair must resolve to Alice again — the
// earlier sticky-flag shape refreshed its own stamp on every write, so one
// message from Bob pinned an active room to `'local'` for as long as the
// conversation lasted (and a cascade keeps itself busy by construction).
func TestPrincipalAttribution_AmbiguityDecaysWithTheSecondStimulus(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	table.Record("group:planning", "iron-fox", "bob-person")
	_, ok := table.Lookup("group:planning", "iron-fox")
	require.False(t, ok, "both stimuli live — the reply may be answering either")

	// Alice keeps dispatching, more often than the TTL, right past the point
	// where Bob's single stimulus can still explain anything.
	for range 5 {
		clock.advance(principalAttributionTTL / 2)
		table.Record("group:planning", "iron-fox", "alice-person")
	}

	got, ok := table.Lookup("group:planning", "iron-fox")
	require.True(t, ok, "a busy room must recover once the second speaker's stimulus ages out")
	assert.Equal(t, "alice-person", got)
}

// TestPrincipalAttribution_UnauthenticatedStimulusAmbiguates pins the other
// half of the same rule. An agent-origin, autonomous or otherwise
// unauthenticated dispatch is a competing stimulus the agent's next reply may
// be answering — it just has no principal to name. Recording it as the
// anonymous stimulus is what makes the pair ambiguous; ignoring it (the
// empty-principal write gate's earlier shape) left the live authenticated
// entry to answer for a turn nobody authenticated caused.
func TestPrincipalAttribution_UnauthenticatedStimulusAmbiguates(t *testing.T) {
	table, _ := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	require.Equal(t, 1, table.len())

	// The convene / synthesis-timeout / agent-origin shape: a real dispatch
	// to the same agent in the same room, carrying no principal.
	table.Record("group:planning", "iron-fox", "")

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "a live unauthenticated stimulus must not resolve to the authenticated one")
}

// TestPrincipalAttribution_UnauthenticatedStimulusCreatesAnUnresolvableRow
// pins that the anonymous stimulus is recorded like any other — it creates a
// row, and that row simply never resolves.
//
// An earlier shape skipped the write when no row existed, reasoning that with
// nothing authenticated outstanding there was nothing the anonymous stimulus
// could be mistaken for. That reasoning holds only at the instant of the
// write; the ordering gate below is the case it missed.
func TestPrincipalAttribution_UnauthenticatedStimulusCreatesAnUnresolvableRow(t *testing.T) {
	table, _ := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "")
	table.Record("group:other", "nova-sparrow", "")

	assert.Equal(t, 2, table.len(), "an unauthenticated dispatch is a live stimulus and is recorded")

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "a stimulus that cannot name anyone can never be an answer")
}

// TestPrincipalAttribution_AnonymousStimulusAmbiguatesALaterPrincipal is the
// regression gate for the ordering hole the anonymous-creates-nothing rule
// left open.
//
// The rule made an anonymous stimulus invisible to the authenticated one that
// arrived NEXT: the later write created a clean single-stimulus row, so the
// pair resolved a principal whose message the agent may never have been
// answering — the mis-attribution this table exists to make impossible, and
// reachable from any principal-less forced turn (a convene tick, a
// synthesis-close timeout — see principal_context.go's origin enumeration)
// landing within one turn budget before an authenticated publish.
//
// Ambiguity must not depend on arrival order.
func TestPrincipalAttribution_AnonymousStimulusAmbiguatesALaterPrincipal(t *testing.T) {
	table, clock := newTestAttributionTable()

	// The principal-less forced turn reaches the agent first.
	table.Record("group:planning", "iron-fox", "")

	// Well inside the turn budget Alice publishes, and the router elects the
	// same agent. It now holds BOTH stimuli and may be answering either.
	clock.advance(5 * time.Second)
	table.Record("group:planning", "iron-fox", "alice-person")

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok,
		"an anonymous stimulus still live when an authenticated one arrives must ambiguate it, in either arrival order")

	// The reverse order has always been ambiguous; pin the symmetry.
	reverse, _ := newTestAttributionTable()
	reverse.Record("group:planning", "iron-fox", "alice-person")
	reverse.Record("group:planning", "iron-fox", "")
	_, ok = reverse.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "the reverse order must resolve nothing either")

	// And the ambiguity still expires with the stimulus that caused it: once
	// the anonymous one ages out, Alice's — refreshed meanwhile — resolves.
	clock.advance(principalAttributionTTL - time.Second)
	table.Record("group:planning", "iron-fox", "alice-person")
	clock.advance(2 * time.Second)

	got, ok := table.Lookup("group:planning", "iron-fox")
	require.True(t, ok, "the anonymous stimulus must age out like any other")
	assert.Equal(t, "alice-person", got)
}

// TestPrincipalAttribution_UnauthenticatedStimulusExpires pins that the
// anonymous stimulus is bound by the same turn budget as any other: once the
// unattributable turn it stood for can no longer be the one in hand, the
// authenticated stimulus that outlived it resolves again.
func TestPrincipalAttribution_UnauthenticatedStimulusExpires(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	table.Record("group:planning", "iron-fox", "")
	_, ok := table.Lookup("group:planning", "iron-fox")
	require.False(t, ok)

	// Keep Alice's stimulus alive across the anonymous one's expiry.
	clock.advance(principalAttributionTTL - time.Second)
	table.Record("group:planning", "iron-fox", "alice-person")
	clock.advance(2 * time.Second)

	got, ok := table.Lookup("group:planning", "iron-fox")
	require.True(t, ok, "the anonymous stimulus must age out like any other")
	assert.Equal(t, "alice-person", got)
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

	// Authenticated traffic stops here; only agent-origin turns continue.
	clock.advance(principalAttributionTTL)
	table.Record("group:planning", "iron-fox", "")

	// The expired row is reclaimed; what remains is the anonymous dispatch's
	// own row, which resolves nothing.
	assert.Equal(t, 1, table.len(),
		"an unauthenticated dispatch must still reclaim the rows that aged out")
	_, ok := table.Lookup("group:quiet", "silent-heron")
	assert.False(t, ok, "the expired authenticated row is gone, not merely unreadable")
	_, ok = table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "and the row it left behind can never be an answer")
}

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
