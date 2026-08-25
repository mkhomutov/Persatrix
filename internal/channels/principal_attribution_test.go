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
// consults it lands in PR 2. They pin the four rules the re-stamp will
// depend on, each of which fails closed — an unattributable reply keeps
// resolving `'local'`, which is today's behaviour, so every degradation
// here is a no-regression rather than a wrong answer.
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
// the stickiness rule: ambiguity dies with the entry it marked. Once both
// stimuli have aged out, the next dispatch is a fresh unambiguous fact — so a
// busy room does not latch itself into permanent `'local'`.
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

// TestPrincipalAttribution_EmptyInputsAreNotRecorded pins the write gate. The
// empty principal is the important one: it is what every unauthenticated
// caller, every agent-origin turn and the whole of `auth.mode: disabled`
// resolve, so recording it would key the table on a value that means "no
// tenant" and hand PR 2's re-stamp a hit that says nothing.
func TestPrincipalAttribution_EmptyInputsAreNotRecorded(t *testing.T) {
	table, _ := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "")
	table.Record("group:planning", "", "alice-person")
	table.Record("", "iron-fox", "alice-person")

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "an empty principal, agent or channel must write nothing")
	assert.Equal(t, 0, table.len(), "no entry may be created from an empty component")
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
