package channels

import (
	"context"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// ISSUE-0124 (R-2) PR 2 — the re-stamp: where the table PR 1 landed stops
// being dormant and a persona's reply starts carrying the tenant of the person
// who caused it.
//
// The defect these tests close is invisible in storage — R-1 re-attributes the
// relayed turns at close, which is why the live 2026-08-07 run read clean on
// `principal_id` while 9 of 15 dispatches carried no tenant at all. So the
// assertions here are on the WIRE, exactly where the live MT looks: what
// principal is on the context of each dispatch the router makes.
//
// Every negative asserts the same fail-closed outcome — no principal, i.e.
// `'local'` persona-side, i.e. the behaviour before this PR. A missed
// attribution is a no-regression; a wrong one would be the defect.

// restampDispatcher is a faithful stand-in for [GRPCMessageDispatcher]'s two
// halves that matter here: it records the principal on each dispatch context,
// and it writes the attribution table under production's own rule — delivered
// dispatches the router elected a reply from ([DispatchEnvelope.ExpectsReply]).
// That rule is pinned against the real dispatcher in
// grpc_dispatcher_attribution_test.go; simulating it here is what lets a whole
// cascade run without a gRPC fleet.
type restampDispatcher struct {
	mu    sync.Mutex
	table *PrincipalAttributionTable
	calls []restampCall
}

type restampCall struct {
	participantID string
	senderID      string
	principal     string
}

func (d *restampDispatcher) Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	principal := PrincipalFromContext(ctx)
	d.mu.Lock()
	d.calls = append(d.calls, restampCall{
		participantID: env.Recipient.ParticipantID,
		senderID:      msg.SenderID,
		principal:     principal,
	})
	d.mu.Unlock()
	if env.ExpectsReply {
		d.table.Record(msg.ChannelID, env.Recipient.ParticipantID, principal)
	}
	return nil
}

// principalTo returns the principal carried on the dispatch to `participantID`,
// and whether one was dispatched at all.
func (d *restampDispatcher) principalTo(participantID string) (string, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	for _, c := range d.calls {
		if c.participantID == participantID {
			return c.principal, true
		}
	}
	return "", false
}

func (d *restampDispatcher) snapshot() []restampCall {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]restampCall, len(d.calls))
	copy(out, d.calls)
	return out
}

func (d *restampDispatcher) reset() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls = nil
}

// newRestampRouter builds a router with the attribution table wired on BOTH
// sides — the dispatcher writes it, the router consumes it — which is the
// production wiring cmd/orchestrator/channels.go performs. The room is a group
// of one human and two `always` personas, the topology R-2 was measured in.
func newRestampRouter(t *testing.T) (*ChannelRouter, *restampDispatcher, *PrincipalAttributionTable, *fakeAttributionClock, string) {
	t.Helper()
	store := newTestStore(t, SQLiteOptions{})
	table, clock := newTestAttributionTable()
	disp := &restampDispatcher{table: table}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	router.SetPrincipalAttribution(table)
	id := mustCreateGroupWithPolicies(t, store, "planning", map[string]RespondPolicy{
		"alice":        RespondNever,
		"iron-fox":     RespondAlways,
		"nova-sparrow": RespondAlways,
	}, "alice", "iron-fox", "nova-sparrow")
	return router, disp, table, clock, id
}

// publishTurn is one turn on the room, under whatever principal `ctx`
// carries — the package already has a `publishAs` for the autonomous
// acceptance suite, whose signature is claim-shaped rather than ctx-shaped.
func publishUnder(t *testing.T, router *ChannelRouter, ctx context.Context, channelID, sender string) {
	t.Helper()
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: channelID, SenderID: sender, Content: "…",
	}, ""))
}

// TestRestamp_RelayedReplyCarriesTheCausalPrincipal is the PR: an
// authenticated person publishes, a persona replies through the REST hop that
// loses the tenant, and the dispatch descending from that reply carries the
// person's principal instead of nothing.
func TestRestamp_RelayedReplyCarriesTheCausalPrincipal(t *testing.T) {
	router, disp, _, _, id := newRestampRouter(t)

	// Alice, authenticated: the middleware's WithPrincipal is on this ctx.
	publishUnder(t, router, WithPrincipal(context.Background(), "alice-person"), id, "alice")
	for _, agent := range []string{"iron-fox", "nova-sparrow"} {
		got, ok := disp.principalTo(agent)
		require.True(t, ok, "alice's publish must reach %s", agent)
		assert.Equal(t, "alice-person", got, "a turn's own dispatches already carried the tenant (v0.3.14)")
	}

	// Iron-fox's reply re-enters as a fresh UNAUTHENTICATED publish — the hop
	// that dropped the tenant for the whole cascade below it.
	disp.reset()
	publishUnder(t, router, context.Background(), id, "iron-fox")

	got, ok := disp.principalTo("nova-sparrow")
	require.True(t, ok, "the relayed reply must fan out to the other persona")
	assert.Equal(t, "alice-person", got,
		"the relayed turn must descend under the principal that caused it, not 'local'")
}

// TestRestamp_InheritsTransitivelyDownTheCascade pins the consequence the
// issue asks to be stated rather than discovered: one authenticated publish
// tags a whole discussion. Nova-sparrow was never dispatched to under alice's
// own turn in this room — it inherits through iron-fox's re-stamped reply.
func TestRestamp_InheritsTransitivelyDownTheCascade(t *testing.T) {
	router, disp, _, _, id := newRestampRouter(t)

	publishUnder(t, router, WithPrincipal(context.Background(), "alice-person"), id, "alice")
	publishUnder(t, router, context.Background(), id, "iron-fox")

	// Hop 3: nova-sparrow answers the re-stamped relay.
	disp.reset()
	publishUnder(t, router, context.Background(), id, "nova-sparrow")

	got, ok := disp.principalTo("iron-fox")
	require.True(t, ok, "the second-hop reply must fan out")
	assert.Equal(t, "alice-person", got,
		"attribution propagates along the causal chain, bounded by cascade_depth")
}

// TestRestamp_ConsumesTheStimulus pins TakeAttribution over Lookup, the
// distinction the PR checklist calls out. A second reply with no intervening
// dispatch has nothing left to answer, so it resolves nothing — which is what
// keeps a busy autonomous room from staying permanently ambiguous.
func TestRestamp_ConsumesTheStimulus(t *testing.T) {
	router, disp, table, _, id := newRestampRouter(t)

	publishUnder(t, router, WithPrincipal(context.Background(), "alice-person"), id, "alice")
	publishUnder(t, router, context.Background(), id, "iron-fox")

	_, live := table.Lookup(id, "iron-fox")
	assert.False(t, live, "the reply must retire the stimulus it answered")

	// Iron-fox speaks again unprompted. Nothing was dispatched to it in
	// between, so nothing explains this turn.
	disp.reset()
	publishUnder(t, router, context.Background(), id, "iron-fox")

	got, ok := disp.principalTo("nova-sparrow")
	require.True(t, ok, "the second reply must still fan out")
	assert.Empty(t, got, "a second reply answering nothing must not inherit the first one's principal")
}

// TestRestamp_NeverOverridesAnAuthenticatedPublish pins gate 1. The caller's
// own verified identity outranks anything the orchestrator can infer, and the
// table is left untouched — an authenticated publish is not an agent answering.
func TestRestamp_NeverOverridesAnAuthenticatedPublish(t *testing.T) {
	router, disp, table, _, id := newRestampRouter(t)

	table.Record(id, "iron-fox", "alice-person")

	// Iron-fox publishes on a context that already names bob — the shape a
	// cascade hop below a re-stamp arrives in.
	publishUnder(t, router, WithPrincipal(context.Background(), "bob-person"), id, "iron-fox")

	got, ok := disp.principalTo("nova-sparrow")
	require.True(t, ok)
	assert.Equal(t, "bob-person", got, "an inferred principal must never beat the one on the context")

	still, live := table.Lookup(id, "iron-fox")
	require.True(t, live, "gate 1 returns before the consuming read")
	assert.Equal(t, "alice-person", still)
}

// TestRestamp_AmbiguousEntryStaysLocal — two people with live stimuli to one
// agent. The reply may be answering either, so it degrades to today's
// behaviour rather than picking the most recent speaker.
func TestRestamp_AmbiguousEntryStaysLocal(t *testing.T) {
	router, disp, table, _, id := newRestampRouter(t)

	table.Record(id, "iron-fox", "alice-person")
	table.Record(id, "iron-fox", "bob-person")

	publishUnder(t, router, context.Background(), id, "iron-fox")

	got, ok := disp.principalTo("nova-sparrow")
	require.True(t, ok)
	assert.Empty(t, got, "an ambiguous entry must resolve nothing")
}

// TestRestamp_ExpiredEntryStaysLocal — past the turn budget the stimulus can
// no longer explain the reply.
func TestRestamp_ExpiredEntryStaysLocal(t *testing.T) {
	router, disp, table, clock, id := newRestampRouter(t)

	table.Record(id, "iron-fox", "alice-person")
	clock.advance(principalAttributionTTL)

	publishUnder(t, router, context.Background(), id, "iron-fox")

	got, ok := disp.principalTo("nova-sparrow")
	require.True(t, ok)
	assert.Empty(t, got, "an expired stimulus must not be inherited")
}

// TestRestamp_AutonomousPublishStaysLocal — a tick-origin turn was never
// caused by anyone, so no entry exists and nothing is inherited. This is also
// the `auth.mode: disabled` shape at the level of one publish.
func TestRestamp_AutonomousPublishStaysLocal(t *testing.T) {
	router, disp, _, _, id := newRestampRouter(t)

	publishUnder(t, router, context.Background(), id, "iron-fox")

	got, ok := disp.principalTo("nova-sparrow")
	require.True(t, ok)
	assert.Empty(t, got, "a publish nothing caused must not be attributed to anyone")
}

// TestRestamp_AuthDisabledIsUnchanged pins the release's no-delta criterion on
// the mode most deployments run: no request ever carries a principal, so every
// dispatch of a full cascade is anonymous and the dispatch topology is
// identical to a router with no table wired at all.
func TestRestamp_AuthDisabledIsUnchanged(t *testing.T) {
	router, disp, _, _, id := newRestampRouter(t)

	// A full round: human turn, persona reply, second persona reply — none of
	// them authenticated, as under auth.mode: disabled.
	publishUnder(t, router, context.Background(), id, "alice")
	publishUnder(t, router, context.Background(), id, "iron-fox")
	publishUnder(t, router, context.Background(), id, "nova-sparrow")
	withTable := disp.snapshot()

	for _, c := range withTable {
		assert.Empty(t, c.principal,
			"no principal may appear anywhere when none ever entered the process")
	}

	// The same round with no table on either side.
	store := newTestStore(t, SQLiteOptions{})
	bare := &restampDispatcher{table: nil}
	unwired := NewChannelRouter(store, bare, zap.NewNop(), nil)
	bareID := mustCreateGroupWithPolicies(t, store, "planning", map[string]RespondPolicy{
		"alice":        RespondNever,
		"iron-fox":     RespondAlways,
		"nova-sparrow": RespondAlways,
	}, "alice", "iron-fox", "nova-sparrow")
	publishUnder(t, unwired, context.Background(), bareID, "alice")
	publishUnder(t, unwired, context.Background(), bareID, "iron-fox")
	publishUnder(t, unwired, context.Background(), bareID, "nova-sparrow")

	assert.Equal(t, len(bare.snapshot()), len(withTable),
		"wiring the table must not change how many dispatches a round makes")
}

// TestRestamp_HumanSenderCannotConsumeAnAgentsAttribution pins that the table
// KEY is what implements "the sender must be a registered agent" — no separate
// participant-type check exists, and none is needed. A human's unauthenticated
// publish (the auth.mode: disabled case) reaches the same consuming read and
// takes nothing, because the orchestrator never dispatches to a human and so
// holds no entry under one.
//
// Asserted on the helper rather than through a full publish on purpose: a real
// publish by alice ALSO dispatches to iron-fox, and that dispatch legitimately
// records an anonymous stimulus which ambiguates the entry (principal_attribution.go).
// The entry would then read empty for a reason that has nothing to do with the
// question being asked. Driving the read directly isolates it.
func TestRestamp_HumanSenderCannotConsumeAnAgentsAttribution(t *testing.T) {
	router, _, table, _, id := newRestampRouter(t)

	table.Record(id, "iron-fox", "alice-person")

	ctx := router.restampCausalPrincipal(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice",
	})
	assert.Empty(t, PrincipalFromContext(ctx), "a human publish must not lift an agent's attribution")

	still, live := table.Lookup(id, "iron-fox")
	require.True(t, live, "the agent's own stimulus must survive someone else's publish")
	assert.Equal(t, "alice-person", still)
}

// TestRestamp_IsTheOnlyPrincipalStampInThisPackage is the route-table-style
// pin ISSUE-0124 asks for: "a second re-stamp site cannot be added by
// omission". It parses this package's own non-test sources rather than
// introspecting behaviour, because the hazard is a NEW call site nobody
// reviews as one — adding it stays compile-green, and only a structural
// assertion turns it red.
//
// Two things are pinned, and since v0.3.15 both allowlists hold exactly ONE
// entry. [PrincipalAttributionTable.TakeAttribution] — the consuming read —
// must have exactly one caller, or the retirement accounting is wrong and
// stimuli no reply answered get spent. [WithPrincipal] likewise:
// principal_restamp.go INFERS a principal the caller never presented, which is
// the mis-attribution surface, and one site is the whole design.
//
// The allowlist USED to carry a second, weaker entry: synthesis_close.go
// re-applied a principal the arming request had presented onto the background
// context its timer goroutine owns, so the close-notification fan landed in the
// interaction's own tenant. ISSUE-0082 residuals PR 4b retired it — the
// `(principal, speaker, scope)` re-key made the fan's ambient tenant select
// nothing, since each record now binds its OWN frozen principal for its whole
// derivation (the audit is in synthesis_close.go's header). Narrowing the
// allowlist is the point: the fewer contexts anyone stamps, the smaller the
// surface this pin has to trust, so re-widening it back to two must be a
// reviewed edit here and not a quiet reintroduction.
//
// A second site of either kind turns this red, which is the inversion
// ISSUE-0124 asks for: a second re-stamp must not be addable by omission.
//
// SCOPE CAVEAT: this scans only THIS package's non-test sources. [WithPrincipal]
// is exported and is already called from another package (internal/server's auth
// middleware, which stamps a VERIFIED account principal, not a wire value), so a
// future re-stamp added in internal/server or cmd/orchestrator from an
// insufficiently-verified source would be invisible to this pin. The guarantee
// here is therefore "no second inferring site inside internal/channels", not the
// repo-wide "addable by omission" the name suggests; a module-wide scan (only two
// non-test WithPrincipal callers exist) would be needed to close that gap.
func TestRestamp_IsTheOnlyPrincipalStampInThisPackage(t *testing.T) {
	sites := map[string][]string{"WithPrincipal": nil, "TakeAttribution": nil}

	entries, err := os.ReadDir(".")
	require.NoError(t, err)
	fset := token.NewFileSet()
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		file, pErr := parser.ParseFile(fset, filepath.Join(".", name), nil, 0)
		require.NoError(t, pErr, "parsing %s", name)
		ast.Inspect(file, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			var fn string
			switch f := call.Fun.(type) {
			case *ast.Ident:
				fn = f.Name
			case *ast.SelectorExpr:
				fn = f.Sel.Name
			}
			if _, watched := sites[fn]; watched {
				sites[fn] = append(sites[fn], name)
			}
			return true
		})
	}

	assert.Equal(t, []string{"principal_restamp.go"}, sites["WithPrincipal"],
		"principal_restamp.go is the ONLY site that may stamp a principal onto a context "+
			"in this package; a second is the mis-attribution hazard ISSUE-0124 is built to "+
			"exclude, and re-adding synthesis_close.go's retired re-stamp (PR 4b) must be a "+
			"reviewed edit to this allowlist, not a quiet reintroduction")
	assert.Equal(t, []string{"principal_restamp.go"}, sites["TakeAttribution"],
		"the consuming read must happen once per publish — a second caller would retire "+
			"stimuli that no reply answered")
}

// TestRestamp_ExpiredCrossoverStaysLocal is the wire-level regression gate for
// the expiry-crossover mis-attribution (PR 2 review, finding 1): the one
// sequence that could make the re-stamp name the WRONG person, which is the
// failure mode every other negative in this file only has to miss on.
//
// Alice's stimulus ages past the turn budget while the agent is still working
// (the delivery ack is pre-ingest, so a long turn is real); Bob's lands just
// inside it. The late reply may be answering Alice, so it must dispatch with
// no principal at all — never with Bob's.
func TestRestamp_ExpiredCrossoverStaysLocal(t *testing.T) {
	router, disp, table, clock, id := newRestampRouter(t)

	table.Record(id, "iron-fox", "alice-person")
	clock.advance(principalAttributionTTL - time.Second)
	table.Record(id, "iron-fox", "bob-person")
	clock.advance(5 * time.Second)

	publishUnder(t, router, context.Background(), id, "iron-fox")

	got, ok := disp.principalTo("nova-sparrow")
	require.True(t, ok, "the late reply must still fan out")
	assert.Empty(t, got,
		"a reply that may answer an expired stimulus must not inherit the surviving one's principal")
}

// TestRestamp_AsyncSeamCarriesTheCausalPrincipal drives the whole R-2 arc
// through [ChannelRouter.PublishAsync] — the seam a persona's reply actually
// re-enters on (the REST handler routes there, not through `Publish`, since
// the RFC 0048 latency fix) — and asserts the re-stamped principal survives
// the [context.WithoutCancel] detach onto the fanout goroutine.
//
// The other tests in this file publish through the synchronous `Publish`;
// both entry points share `publishCommit`, but the detached-context threading
// is PublishAsync's own plumbing, and this pin is what turns red if a
// refactor detaches from the caller's context instead of the returned one.
func TestRestamp_AsyncSeamCarriesTheCausalPrincipal(t *testing.T) {
	router, disp, _, _, id := newRestampRouter(t)

	require.NoError(t, router.PublishAsync(WithPrincipal(context.Background(), "alice-person"),
		ChannelMessage{ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "…"}, ""))
	router.WaitForPendingFanout()

	disp.reset()
	require.NoError(t, router.PublishAsync(context.Background(),
		ChannelMessage{ID: uuid.NewString(), ChannelID: id, SenderID: "iron-fox", Content: "…"}, ""))
	router.WaitForPendingFanout()

	got, ok := disp.principalTo("nova-sparrow")
	require.True(t, ok, "the relayed reply must fan out on the detached goroutine")
	assert.Equal(t, "alice-person", got,
		"the re-stamped principal must ride the detached fanout context on the async seam")
}
