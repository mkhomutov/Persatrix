package channels

// autonomous_acceptance_test.go — RFC 0052 PR 5 (Phase 1e): the Phase-1
// integration suite (docs/rfcs/0052-pr-plan.md). Where the PR 1–4b matrices pin
// each mechanism in isolation, this suite chains them END TO END against a REAL
// wallet ([wallet.WalletService] wired both directions — the router→wallet
// spend read and the wallet→router budget resolver, exactly as
// cmd/orchestrator wires them) and is the deterministic mock-provider backbone
// of MT-AUTONOMOUS-001 (docs/manual-tests/MT-AUTONOMOUS-001.md):
//
//   - the FULL-CYCLE leg — convene → discuss → converge → terminate →
//     synthesize with ZERO human turns, both §D artifacts leased, spend ≤ cap;
//   - the NO-RUNAWAY leg — an adversarial "everyone always wants to talk"
//     roster stays bounded: turns capped by `max_rounds`, exactly one close,
//     no dispatch into the terminated discussion;
//   - the CLOSE-BY-BUDGET leg (MT-AUTONOMOUS-001's Step 4 ground truth, on a
//     ≥2-persona roster) — the soft threshold trips the close while the
//     roster-scaled `1 + N` reserve still funds EVERY close-path lease (the
//     chair synthesis turn + one OQ #6-metered RFC 0020 summary per persona),
//     where the fail-closed hard cap alone would have denied them.
//
// The Python halves of the same acceptance (the convener/chair authoring, the
// metered summarize_close call, the placeholder-vs-real summary contract) are
// pinned in tests/unit/python/test_autonomous_phase1_acceptance.py.

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
	"github.com/mkhomutov/persatrix/internal/wallet"
)

// acceptanceRoster is the 3-persona Phase-1 roster: a convener, a responder,
// and the escalation chair — every seat a persona (no human/operator seat at
// all, so "zero human turns" holds by construction AND is asserted over the
// committed history).
var acceptanceRoster = []string{"nova-sparrow", "ember-owl", "iron-fox"}

// newAcceptanceWallet builds a real WalletService over a real TokenCounter +
// BudgetEnforcer. Dollar budgets are generous so the per-interaction TOKEN
// ceiling (the RFC 0052 mandatory cap) is the only binding constraint —
// the wallet-package tests' posture.
func newAcceptanceWallet(t *testing.T) *wallet.WalletService {
	t.Helper()
	costCfg := &cost.CostConfig{
		Pricing: map[string]cost.ModelPricing{
			"claude-haiku": {InputPer1MTokens: 0.8, OutputPer1MTokens: 4.0},
		},
		Budgets: cost.BudgetThresholds{
			Global:      cost.GlobalBudget{MaxDailyUSD: 100.0, OnExceed: "fail"},
			PerWorkflow: cost.PerWorkflowBudget{DefaultMaxUSD: 10.0},
			PerAgent:    cost.PerAgentBudget{DefaultMaxUSD: 5.0},
		},
	}
	counter := cost.NewTokenCounter(costCfg, zap.NewNop())
	enforcer := cost.NewBudgetEnforcer(counter, costCfg, zap.NewNop())
	return wallet.NewWalletService(counter, enforcer,
		wallet.Config{TTL: 90 * time.Second, ReaperInterval: time.Hour, MaxActiveLeases: 64},
		zap.NewNop())
}

// acceptanceHarness wires the production shape end to end: a floor-controlled
// armed group of three personas (convener nova-sparrow, chair iron-fox), a
// real wallet coupled to the router in BOTH directions, and both governance
// counters. `budgetTokens` > 0 sets the mandatory per-interaction cap.
func acceptanceHarness(t *testing.T, maxRounds int, budgetTokens int64) (*ChannelRouter, *wallet.WalletService, *dispatchRecorder, ChannelStore, string, *sdkmetric.ManualReader) {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	closed, err := mp.Meter("test").Int64Counter("channel.conversation.interaction_closed")
	require.NoError(t, err)
	synth, err := mp.Meter("test").Int64Counter("channel.conversation.synthesis_turn")
	require.NoError(t, err)
	store := newTestStore(t, SQLiteOptions{})
	disp := &dispatchRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), &RouterMetrics{
		InteractionClosed: closed,
		SynthesisTurn:     synth,
	})
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"nova-sparrow": RespondAlways, // the convener
			"ember-owl":    RespondAlways,
			"iron-fox":     RespondAlways, // the escalation chair
		}, acceptanceRoster...)
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetAutonomous(ch, AutonomousConfig{
		Enabled: true, MaxRounds: maxRounds, Convener: "nova-sparrow",
		Topic:  "Should we adopt a monorepo?",
		Agenda: []string{"Build tooling cost", "Migration effort"},
		Goal:   "A synthesized recommendation.",
	})
	router.SetEscalationChair(ch, "iron-fox")

	w := newAcceptanceWallet(t)
	router.SetInteractionSpender(w)
	w.SetInteractionBudgetResolver(router.ResolveInteractionBudgetForInteraction)
	if budgetTokens > 0 {
		router.SetInteractionBudgetTokens(ch, budgetTokens)
	}
	return router, w, disp, store, ch, reader
}

// acquireLease acquires one claude-haiku lease attributed to interactionID.
// The budget rides the WIRED RESOLVER (the store-authoritative RFC 0050
// amendment path), never the request field — the production shape.
func acquireLease(t *testing.T, w *wallet.WalletService, agentID, interactionID string, input, output int64) *walletpb.LeaseResponse {
	t.Helper()
	resp, err := w.AcquireLease(context.Background(), &walletpb.LeaseRequest{
		AgentId: agentID, Model: "claude-haiku",
		EstimatedInputTokens: input, EstimatedMaxOutputTokens: output,
		Cause:         walletpb.Cause_CAUSE_CHANNEL_MESSAGE,
		InteractionId: interactionID,
	})
	require.NoError(t, err, "a budget denial is an in-band response, not a gRPC error")
	return resp
}

// publishAs publishes one message from a persona seat, echoing `claimID` (the
// dispatched-under interaction id every production agent reply carries — the
// PR 4b-i origin-pair echo) when non-empty.
func publishAs(t *testing.T, router *ChannelRouter, ch, sender, content, claimID string) {
	t.Helper()
	var md map[string]any
	if claimID != "" {
		md = map[string]any{interactionIDMetadataKey: claimID}
	}
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: sender, Content: content, Metadata: md,
	}, ""))
}

// TestAutonomousAcceptance_FullCycle — the Phase-1 contract in one pass
// (RFC 0052 §B + §D; MT-AUTONOMOUS-001 Steps 1–3): convene dispatches exactly
// one directed opener directive; the convener's opening turn (its lease
// documented-uncapped, §B) mints the interaction; leased member rounds run to
// the structural bound; the bound dispatches the goal-directed chair synthesis
// turn instead of closing; the chair's marked reply IS the closing artifact
// every persona is notified with; each persona's OQ #6-metered close summary
// still leases under the cap; total spend ≤ the cap; the committed history
// carries zero human turns; and the channel is re-convenable.
func TestAutonomousAcceptance_FullCycle(t *testing.T) {
	router, w, disp, store, ch, reader := acceptanceHarness(t, 3, 50_000)
	ctx := context.Background()

	// §B — convene: exactly one convene-marked forced turn, directed to the
	// convener, synthetically sent, carrying the operator subject.
	convener, err := router.ConveneChannel(ctx, ch)
	require.NoError(t, err)
	require.Equal(t, "nova-sparrow", convener)
	var convenes []recordedDispatch
	for _, c := range disp.snapshot() {
		if c.env.Convene {
			convenes = append(convenes, c)
		}
	}
	require.Len(t, convenes, 1, "convening dispatches exactly one opener directive")
	assert.Equal(t, "nova-sparrow", convenes[0].env.Recipient.ParticipantID)
	assert.Equal(t, ConveneDispatchSenderID, convenes[0].msg.SenderID)
	assert.Contains(t, convenes[0].msg.Content, "Should we adopt a monorepo?")

	// §B — the opening turn's lease predates the interaction's first commit,
	// so it resolves UNCAPPED (the documented posture; the Layer-0 depth cap
	// is its net). It is untracked, so it must not count toward the cap below.
	opening := acquireLease(t, w, "nova-sparrow", "", 600, 200)
	require.NotNil(t, opening.GetGrant(), "the opening turn's lease is documented-uncapped (§B)")
	publishAs(t, router, ch, "nova-sparrow",
		"Convening: should we adopt a monorepo? First agenda item: build tooling cost.", "")
	openID, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked, "the opening turn mints a fresh committed interaction")

	// Discussion rounds 2..3, each a leased member reply echoing the open id.
	// The budget snapshot was taken at the opening commit, so these leases
	// resolve the channel cap through the wired resolver.
	require.NotNil(t, acquireLease(t, w, "ember-owl", openID, 800, 200).GetGrant())
	publishAs(t, router, ch, "ember-owl", "Tooling cost cuts both ways: one CI graph, but a bigger blast radius.", openID)
	assert.Zero(t, closedCount(t, reader, structuralTrigger), "no close before the bound")

	require.NotNil(t, acquireLease(t, w, "nova-sparrow", openID, 800, 200).GetGrant())
	publishAs(t, router, ch, "nova-sparrow", "Converging: tooling favours the monorepo.", openID) // round 3 == max_rounds

	// §D — the bound arms the synthesis close: no close yet, one goal-carrying
	// synthesis forced turn to the chair, claiming the closing interaction.
	assert.Zero(t, closedCount(t, reader, structuralTrigger),
		"a chaired bound arms the synthesis turn instead of closing inline")
	turns := disp.synthesisTurns()
	require.Len(t, turns, 1, "exactly one synthesis forced turn")
	assert.Equal(t, "iron-fox", turns[0].env.Recipient.ParticipantID)
	assert.Equal(t, SynthesisDispatchSenderID, turns[0].msg.SenderID)
	assert.Contains(t, turns[0].msg.Content, "A synthesized recommendation.")
	assert.Equal(t, openID, readInteractionID(turns[0].msg.Metadata))

	// The chair's synthesis turn is itself a leased call — the first of the
	// `1 + N` close-path calls the reserve exists for, sized to exactly one
	// per-call reserve unit (in + out == the published per-call sizing).
	require.NotNil(t, acquireLease(t, w, "iron-fox", openID,
		wallet.DefaultSynthesisCallReserveTokens-1_024, 1_024).GetGrant(),
		"the chair synthesis turn's lease must be honoured")
	chairReply(t, router, ch, "iron-fox", openID,
		"Synthesis: adopt the monorepo; the tooling consolidation outweighs the migration cost.")
	router.WaitForPendingFanout()

	// Close-on-reply: the synthesis reply closed the interaction and the
	// notification fan carried THE SYNTHESIS to every persona (sole delivery).
	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger))
	assert.Equal(t, int64(1), synthesisTurnCount(t, reader, "closed_on_reply"))
	_, _, tracked = router.openInteractionEscalationState(ch)
	assert.False(t, tracked, "the id is retired — the channel is re-convenable")
	notified := map[string]bool{}
	for _, c := range disp.closeNotifications() {
		notified[c.env.Recipient.ParticipantID] = true
		assert.Contains(t, c.msg.Content, "Synthesis: adopt the monorepo",
			"the close notification carries the §D artifact")
		assert.Equal(t, structuralTrigger, c.env.InteractionCloseTrigger)
		assert.False(t, c.env.InteractionCloseRedelivery, "the synthesis is sole-delivery")
	}
	for _, member := range acceptanceRoster {
		assert.Truef(t, notified[member], "every persona is notified of the close: %s", member)
	}

	// OQ #6 — the per-persona close summaries lease under the SAME interaction
	// id. The budget snapshot must survive the close (deferred eviction) so
	// those summaries are genuinely METERED, not silently uncapped.
	budget, capped := router.ResolveInteractionBudgetForInteraction(openID)
	require.True(t, capped, "the budget snapshot survives the close — the OQ #6 metering seam")
	require.Equal(t, int64(50_000), budget)
	for _, member := range acceptanceRoster {
		require.NotNilf(t, acquireLease(t, w, member, openID, 2_000, 1_024).GetGrant(),
			"persona %s's metered close summary must be leased — the N of `1 + N`", member)
	}

	// The mandatory-cap contract: everything the interaction spent — discussion
	// rounds, the chair turn, every summary — stays within the cap.
	spend := w.InteractionSpend(openID)
	assert.Positive(t, spend, "the metered calls accrued to the interaction")
	assert.LessOrEqual(t, spend, int64(50_000), "total interaction spend ≤ the mandatory cap")

	// Zero human turns: every committed message was authored by a roster
	// persona (the synthetic convene/synthesis directives are dispatches, not
	// history rows).
	history, err := store.GetHistory(ctx, ch, 100, time.Now().Add(time.Hour))
	require.NoError(t, err)
	require.NotEmpty(t, history)
	roster := map[string]bool{}
	for _, member := range acceptanceRoster {
		roster[member] = true
	}
	for _, m := range history {
		assert.Truef(t, roster[m.SenderID], "zero human turns — unexpected sender %q", m.SenderID)
	}

	// Re-convenable: the retired id no longer blocks a fresh convening.
	_, err = router.ConveneChannel(ctx, ch)
	require.NoError(t, err, "after the bounded close the channel convenes again")
}

// adversarialDispatcher simulates the RFC 0052 no-runaway roster: EVERY
// ordinary stimulus draws a reply from its recipient (the "everyone always
// wants to talk" worst case), the convener answers the convene directive with
// the opening turn, and the chair answers the synthesis directive with the
// marked closing artifact — each as an async Publish echoing the
// dispatched-under id, the production reply shape. Close notifications are
// ingest-only (never replied to). The reply cap is a test safety valve far
// above the bound: if the bounded close failed, the assertions fail on the
// dispatch count rather than the loop running away.
type adversarialDispatcher struct {
	router *ChannelRouter
	wg     sync.WaitGroup

	mu      sync.Mutex
	calls   []recordedDispatch
	replies int
}

const adversarialReplyCap = 64

func (d *adversarialDispatcher) Dispatch(_ context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	d.mu.Lock()
	d.calls = append(d.calls, recordedDispatch{env: env, msg: msg})
	overCap := d.replies >= adversarialReplyCap
	if !overCap && !env.InteractionCloseNotification {
		d.replies++
	}
	d.mu.Unlock()
	if overCap || env.InteractionCloseNotification {
		return nil // ingest-only: a close notification never draws a reply.
	}

	recipient := env.Recipient.ParticipantID
	claim := readInteractionID(msg.Metadata)
	reply := ChannelMessage{ID: uuid.NewString(), ChannelID: msg.ChannelID, SenderID: recipient}
	switch {
	case env.Convene:
		reply.Content = "Opening the floor: let us work the topic."
	case env.SynthesisTurn:
		reply.Content = "Synthesis: the roster converged."
		reply.Metadata = map[string]any{
			interactionIDMetadataKey:  claim,
			synthesisReplyMetadataKey: true,
		}
	default:
		reply.Content = "I have more to add."
		if claim != "" {
			reply.Metadata = map[string]any{interactionIDMetadataKey: claim}
		}
	}
	d.wg.Add(1)
	go func() {
		defer d.wg.Done()
		// A straggler reply into the armed window / closed ledger is absorbed
		// by design; its error (none expected) would only mean "no reply".
		_ = d.router.Publish(context.Background(), reply, "")
	}()
	return nil
}

func (d *adversarialDispatcher) snapshot() []recordedDispatch {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]recordedDispatch, len(d.calls))
	copy(out, d.calls)
	return out
}

// TestAutonomousAcceptance_NoRunawayUnderAdversarialRoster — the RFC 0052
// §Security no-runaway leg on the self-amplifying concurrent path: with three
// personas who reply to EVERYTHING, the discussion still terminates at
// `max_rounds` with exactly one close, one synthesis turn, a bounded dispatch
// count, and no traffic fanning into the terminated discussion.
func TestAutonomousAcceptance_NoRunawayUnderAdversarialRoster(t *testing.T) {
	const maxRounds = 4
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	closed, err := mp.Meter("test").Int64Counter("channel.conversation.interaction_closed")
	require.NoError(t, err)
	store := newTestStore(t, SQLiteOptions{})
	disp := &adversarialDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), &RouterMetrics{InteractionClosed: closed})
	disp.router = router
	// Join the async publishers even when an assertion fails first (FailNow
	// skips the inline join below): registered after newTestStore's cleanup,
	// so it runs BEFORE the store closes (LIFO) and no publisher goroutine
	// leaks into later tests racing a closed store.
	t.Cleanup(disp.wg.Wait)
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"nova-sparrow": RespondAlways,
			"ember-owl":    RespondAlways,
			"iron-fox":     RespondAlways,
		}, acceptanceRoster...)
	// Floor control OFF — the concurrent path, where every dispatched reply
	// re-enters Publish and fans again: the self-amplification §Security names.
	router.SetAutonomous(ch, AutonomousConfig{
		Enabled: true, MaxRounds: maxRounds, Convener: "nova-sparrow",
		Topic: "An adversarial roster.", Goal: "Converge anyway.",
	})
	router.SetEscalationChair(ch, "iron-fox")

	// One convene seeds the whole run: the dispatcher plays every persona.
	_, err = router.ConveneChannel(context.Background(), ch)
	require.NoError(t, err)

	// The loop must terminate by itself: bound → synthesis turn → the chair's
	// marked reply closes.
	require.Eventually(t, func() bool {
		return closedCount(t, reader, structuralTrigger) == 1
	}, 5*time.Second, 5*time.Millisecond, "the adversarial loop terminates at the bound")
	disp.wg.Wait()
	router.WaitForPendingFanout()

	var ordinary, synthesisTurns, closeNotes int
	for _, c := range disp.snapshot() {
		switch {
		case c.env.SynthesisTurn:
			synthesisTurns++
		case c.env.InteractionCloseNotification:
			closeNotes++
		case c.env.Convene:
		default:
			ordinary++
		}
	}
	// Each live message fans to at most the other personas (len(roster)−1),
	// and only tally-live messages fan (the bounding stimulus and every
	// straggler are withheld), so the dispatch count — the LLM-cost proxy —
	// is bounded by the round bound, not the roster's appetite.
	assert.LessOrEqual(t, ordinary, (len(acceptanceRoster)-1)*maxRounds, "turns stay bounded under adversarial pressure")
	assert.GreaterOrEqual(t, ordinary, 2, "the discussion genuinely ran before terminating")
	assert.Equal(t, 1, synthesisTurns, "exactly one synthesis turn")
	assert.Equal(t, len(acceptanceRoster), closeNotes,
		"the close notification fans to the FULL roster — the round-triggering sender included, so every persona authors its summary")
	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger), "exactly one close — no reopen loop")

	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.False(t, tracked, "the interaction is retired")
	assert.Empty(t, router.ChannelActivity(ch), "no persona is stranded 'thinking' after the close")
}

// TestAutonomousAcceptance_CloseByBudgetHonoursRosterScaledReserve — the
// close-by-budget leg (RFC 0052 §D / OQ #6, on a ≥2-persona roster): the SOFT
// threshold (cap − the `1 + N` reserve) trips the close while the hard cap
// still has reserve headroom, so the chair synthesis turn AND every persona's
// metered close summary lease are honoured — where a discussion-sized lease at
// the same moment is already fail-closed DENIED. This is the regression the
// fail-closed wallet would otherwise cause: without the reserve, the close
// artifacts would be the calls the cap swallows.
func TestAutonomousAcceptance_CloseByBudgetHonoursRosterScaledReserve(t *testing.T) {
	const cap = int64(60_000)
	// channelSize 3 → reserve (1 + 3×2)×3500 = 24 500 (unclamped; the v0.3.15
	// re-key sizing — each member closes one record per speaker it heard, so
	// the fan draws up to N×(N−1) metered summaries), soft = 35 500.
	soft := wallet.SynthesisSoftBudgetTokens(cap, len(acceptanceRoster))
	require.Equal(t, int64(35_500), soft, "the leg's arithmetic rides the published reserve sizing")
	router, w, disp, _, ch, reader := acceptanceHarness(t, 100 /* out of reach */, cap)

	// Convene + opening turn (uncapped, §B), minting the capped interaction.
	_, err := router.ConveneChannel(context.Background(), ch)
	require.NoError(t, err)
	publishAs(t, router, ch, "nova-sparrow", "Convening on the topic.", "")
	openID, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked)

	// The discussion spends exactly up to the soft threshold: two leased
	// member turns splitting the soft budget between them. Both grant — the
	// hard cap is still a full reserve away.
	first, second := soft/2, soft-soft/2
	require.NotNil(t, acquireLease(t, w, "ember-owl", openID, first-1_000, 1_000).GetGrant())
	publishAs(t, router, ch, "ember-owl", "A long, expensive contribution.", openID)
	assert.Zero(t, closedCount(t, reader, costTrigger), "below the soft threshold the discussion runs")
	require.NotNil(t, acquireLease(t, w, "nova-sparrow", openID, second-1_000, 1_000).GetGrant())
	require.Equal(t, soft, w.InteractionSpend(openID), "running spend sits exactly at the soft threshold")
	publishAs(t, router, ch, "nova-sparrow", "Another long contribution.", openID)

	// The tail saw spend ≥ soft: cost trigger → the synthesis close is ARMED
	// (no inline close), the chair turn dispatched.
	assert.Zero(t, closedCount(t, reader, costTrigger), "the chaired budget bound arms, not closes")
	require.Len(t, disp.synthesisTurns(), 1, "the soft threshold dispatched the chair synthesis turn")

	// The contrast that makes the reserve load-bearing: at this very moment a
	// further discussion-sized lease — sized just over the reserve headroom
	// the hard cap has left — is fail-closed DENIED…
	reserve := cap - soft
	deniedResp := acquireLease(t, w, "ember-owl", openID, reserve, 1_000)
	denied := deniedResp.GetDenied()
	require.NotNil(t, denied, "a discussion-sized lease is denied once the close has fired")
	assert.Equal(t, walletpb.LeaseDeniedReason_LEASE_DENIED_REASON_INTERACTION_BUDGET_EXHAUSTED, denied.GetReason())

	// …while EVERY close-path call still fits in the held-back reserve:
	// the chair synthesis turn first, sized to exactly one per-call unit…
	require.NotNil(t, acquireLease(t, w, "iron-fox", openID,
		wallet.DefaultSynthesisCallReserveTokens-1_024, 1_024).GetGrant(),
		"the reserve funds the chair synthesis turn on a budget-exhausted close")
	chairReply(t, router, ch, "iron-fox", openID, "Synthesis: converged under budget pressure.")
	router.WaitForPendingFanout()
	assert.Equal(t, int64(1), closedCount(t, reader, costTrigger), "the synthesis reply closes with the truthful cost trigger")
	assert.Zero(t, closedCount(t, reader, structuralTrigger))
	for _, c := range disp.closeNotifications() {
		assert.Contains(t, c.msg.Content, "Synthesis: converged", "the artifact survives the budget close")
		assert.Equal(t, costTrigger, c.env.InteractionCloseTrigger,
			"the truthful cost cause rides the notification — the OQ #6 metering key")
	}

	// …then the re-key's full summary fan — each member closes one record per
	// speaker it heard (N−1 = 2 in this roster), and EVERY record's metered
	// summary leases: N×(N−1) = 6 calls the pre-re-key `1 + N` reserve could
	// not have funded at this cap.
	for _, member := range acceptanceRoster {
		for record := 0; record < len(acceptanceRoster)-1; record++ {
			require.NotNilf(t, acquireLease(t, w, member, openID, 2_000, 1_024).GetGrant(),
				"persona %s's close summary for record %d must survive the "+
					"budget-exhausted close", member, record)
		}
	}

	spend := w.InteractionSpend(openID)
	assert.GreaterOrEqual(t, spend, soft, "the close path drew from the reserve")
	assert.LessOrEqual(t, spend, cap, "every lease — discussion and close path — stayed within the hard cap")
}
