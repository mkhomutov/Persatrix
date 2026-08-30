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
// the agent holds exactly one stimulus, it is still inside the turn budget,
// and it has a principal. Everything else — nothing held, two stimuli held
// (expired ones included: age disqualifies a stimulus but only the agent's
// own reply removes one — the crossover rule), a principal racing an
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
// stimulus is simply still held. It stops mattering when the agent next
// replies — see [TestPrincipalAttribution_SecondStimulusBlocksUntilTheAgentSpeaks]
// — never by ageing out, because expiry disqualifies a stimulus without
// removing it (the crossover rule).
func TestPrincipalAttribution_AmbiguityIsStickyWhileLive(t *testing.T) {
	table, clock := newTestAttributionTable()

	table.Record("group:planning", "iron-fox", "alice-person")
	table.Record("group:planning", "iron-fox", "bob-person")
	clock.advance(time.Second)
	table.Record("group:planning", "iron-fox", "alice-person")

	_, ok := table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "a live ambiguous entry can never be disambiguated by a later dispatch")
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

	// The anonymous ghost does not age out of candidacy — the forced turn may
	// be exactly what the slow agent is answering (the crossover rule) — so
	// the pair stays blocked past its budget. It recovers when the agent
	// speaks: the reply retires both, and Alice's next stimulus stands alone.
	clock.advance(principalAttributionTTL)
	table.Record("group:planning", "iron-fox", "alice-person")
	_, ok = table.Lookup("group:planning", "iron-fox")
	assert.False(t, ok, "an expired forced turn still blocks until the agent answers")

	_, _ = table.TakeAttribution("group:planning", "iron-fox")
	table.Record("group:planning", "iron-fox", "alice-person")

	got, ok := table.Lookup("group:planning", "iron-fox")
	require.True(t, ok, "the reply retires the forced turn; Alice's next stimulus stands alone")
	assert.Equal(t, "alice-person", got)
}
