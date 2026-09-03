package channels

// bounded_close_reserve_test.go — ISSUE-0082 residuals PR 4b (v0.3.15): the
// close-path reserve re-size and its clamp signal, at the seam where the router
// actually consults them.
//
// Two things are pinned here that the wallet's own tests cannot see, because
// both are about which NUMBER the router hands the wallet:
//
//   - the soft threshold is derived from the CLOSE-RECORD count, not the member
//     count. The `(principal, speaker, scope)` re-key made the effective close
//     roster grow past the member count, so a `channelSize` basis would fire the
//     close at a spend the reserve cannot cover — the "close leases denied" hole
//     bounded_close.go's own comment has warned about since 4b-ii; and
//   - the half-cap clamp is no longer silent. Its failure — denied summary
//     leases committing the RFC 0020 janitor's unavailable placeholder, which
//     nothing retries — leaves no other trace, so the counter and the Warn line
//     ARE the observability, and a test is the only thing that keeps them wired.

import (
	"context"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/wallet"
)

// reserveHarness is [autonomousCloseHarness] — the SAME three-seat room every
// other close test is written against, so `channelSize` is 3 and the arithmetic
// below is legible — with the PR 4b clamp counter wired alongside
// interaction_closed, so a test can assert the close AND its signal from one
// collection. Sharing the stage rather than restating it is deliberate: the
// record arithmetic here is derived from that roster, and a second copy of it
// could drift out from under these numbers while both files stayed green.
func reserveHarness(t *testing.T, maxRounds int) (*ChannelRouter, string, *sdkmetric.ManualReader) {
	t.Helper()
	router, _, ch, reader := autonomousCloseHarness(t, true, maxRounds, zap.NewNop(), addReserveClampCounter(t))
	return router, ch, reader
}

// reserveHarnessWithLogs is [reserveHarness] plus a capturing logger, for the
// one property the counter cannot show: the Warn is emitted once per (channel,
// configuration), not once per close.
func reserveHarnessWithLogs(t *testing.T, maxRounds int) (*ChannelRouter, string, *sdkmetric.ManualReader, *observer.ObservedLogs) {
	t.Helper()
	core, logs := observer.New(zap.WarnLevel)
	router, _, ch, reader := autonomousCloseHarness(t, true, maxRounds, zap.New(core), addReserveClampCounter(t))
	return router, ch, reader, logs
}

// addReserveClampCounter registers `synthesis_reserve_clamped` on the harness's
// own meter provider, so both counters land in one collection.
func addReserveClampCounter(t *testing.T) func(metric.Meter, *RouterMetrics) {
	t.Helper()
	return func(m metric.Meter, rm *RouterMetrics) {
		clamped, err := m.Int64Counter("channel.conversation.synthesis_reserve_clamped")
		require.NoError(t, err)
		rm.SynthesisReserveClamped = clamped
	}
}

// clampWarnCount counts the clamp Warn lines the router emitted.
func clampWarnCount(logs *observer.ObservedLogs) int {
	return logs.FilterMessageSnippet("close-path reserve clamped").Len()
}

// reserveClampedCount reads `channel.conversation.synthesis_reserve_clamped` for
// one (channel_type, trigger) pair — the [interactionClosedCount] sibling.
func reserveClampedCount(t *testing.T, reader *sdkmetric.ManualReader, trigger string) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.conversation.synthesis_reserve_clamped" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			require.Truef(t, ok, "synthesis_reserve_clamped: expected Sum[int64], got %T", m.Data)
			for _, dp := range sum.DataPoints {
				ct, _ := dp.Attributes.Value("channel_type")
				tg, _ := dp.Attributes.Value("trigger")
				if ct.AsString() == "group" && tg.AsString() == trigger {
					return dp.Value
				}
			}
		}
	}
	return 0
}

// TestBoundedClose_SoftThresholdUsesTheCloseRecordBasis is the re-size's
// behavioural pin, written as the one spend that separates the two bases: a
// spend that HAS crossed the record-count soft threshold but has NOT crossed the
// old member-count one. Under the old basis this publish dispatches normally and
// the discussion runs on against a reserve that cannot fund its own close; under
// the new basis it closes. Asserting a reserve NUMBER instead would pass against
// either basis as long as the arithmetic was self-consistent.
func TestBoundedClose_SoftThresholdUsesTheCloseRecordBasis(t *testing.T) {
	const channelSize = 3 // operator + two personas
	const budget = int64(200_000)

	records := wallet.CloseRecordUpperBound(channelSize)
	softRecords := wallet.SynthesisSoftBudgetTokens(budget, records)
	softRoster := wallet.SynthesisSoftBudgetTokens(budget, channelSize)
	require.False(t, wallet.SynthesisReserveClamped(budget, records),
		"fixture must separate the two bases, not collapse both onto the clamp")
	require.Less(t, softRecords, softRoster,
		"fixture is only meaningful while the record count exceeds the member count")

	// Between the two thresholds: over the record-count one, under the old one.
	spend := (softRecords + softRoster) / 2

	router, ch, reader := reserveHarness(t, 100) // round bound out of reach
	router.SetInteractionBudgetTokens(ch, budget)
	router.SetInteractionSpender(fakeSpender{v: spend})

	tick(t, router, ch)

	assert.Equal(t, int64(1), closedCount(t, reader, costTrigger),
		"a spend past the CLOSE-RECORD soft threshold must close: the re-key made the "+
			"close issue one summary per (principal, speaker) record per persona, so a "+
			"member-count threshold would fire the close too late for the reserve to cover it")
}

// TestBoundedClose_ClampedReserveIsReported: on a cap too small to fund the
// room's close, the close still fires — the clamp protects the discussion, not
// the close — but it no longer fires silently.
func TestBoundedClose_ClampedReserveIsReported(t *testing.T) {
	const budget = int64(60_000) // half of it cannot fund 21 close-path calls
	require.True(t, wallet.SynthesisReserveClamped(budget, wallet.CloseRecordUpperBound(3)),
		"fixture must actually clamp")

	router, ch, reader := reserveHarness(t, 100)
	router.SetInteractionBudgetTokens(ch, budget)
	router.SetInteractionSpender(fakeSpender{v: budget})

	tick(t, router, ch)

	require.Equal(t, int64(1), closedCount(t, reader, costTrigger))
	assert.Equal(t, int64(1), reserveClampedCount(t, reader, costTrigger),
		"an under-funded close must be visible: its late per-record summaries degrade to "+
			"the janitor's unavailable placeholder, which nothing retries")
}

// TestBoundedClose_ClampedReserveIsReportedOnStructuralClose: the signal is not
// a cost-close detail. A `max_rounds` close runs the same close path against the
// same clamped reserve, so gating the report on the cost trigger would hide half
// the failures — including every unattended channel that converges before it
// spends anything.
func TestBoundedClose_ClampedReserveIsReportedOnStructuralClose(t *testing.T) {
	const budget = int64(60_000)

	router, ch, reader := reserveHarness(t, 2)
	router.SetInteractionBudgetTokens(ch, budget)
	router.SetInteractionSpender(fakeSpender{v: 0}) // nothing spent: cost cannot fire

	tick(t, router, ch)
	require.Zero(t, reserveClampedCount(t, reader, structuralTrigger),
		"no close yet, so nothing to report")

	tick(t, router, ch) // the max_rounds round

	require.Equal(t, int64(1), closedCount(t, reader, structuralTrigger))
	assert.Equal(t, int64(1), reserveClampedCount(t, reader, structuralTrigger),
		"a structural close against a clamped reserve is the same under-funded close")
	assert.Zero(t, reserveClampedCount(t, reader, costTrigger))
}

// TestBoundedClose_UnclampedReserveIsSilent is the negative that keeps the
// counter worth reading: a cap that genuinely funds the room's close must not
// tick it. A signal that fired on every capped close would be indistinguishable
// from no signal at all, which is the state PR 4b is fixing.
func TestBoundedClose_UnclampedReserveIsSilent(t *testing.T) {
	const budget = int64(1_000_000)
	require.False(t, wallet.SynthesisReserveClamped(budget, wallet.CloseRecordUpperBound(3)),
		"fixture must NOT clamp")

	router, ch, reader := reserveHarness(t, 100)
	router.SetInteractionBudgetTokens(ch, budget)
	router.SetInteractionSpender(fakeSpender{v: budget})

	tick(t, router, ch)

	require.Equal(t, int64(1), closedCount(t, reader, costTrigger))
	assert.Zero(t, reserveClampedCount(t, reader, costTrigger))
	assert.Zero(t, reserveClampedCount(t, reader, structuralTrigger))
}

// TestBoundedClose_ClampIsSilentWhenTheCloseIsRefused — the negative the other
// three could not see, because all of them vary the reserve ARITHMETIC while
// the close always fires. This one holds the arithmetic fixed (clamped, both
// times) and varies the close OUTCOME: the bound is crossed on a stale
// fanout-head snapshot and the action-point re-check then refuses it.
//
// A crossing is not a close. The counter's contract is one increment per close
// that FIRED, and the ISSUE-0138 calibration divides it by `interaction_closed`
// — so a signal on a refused bound does not just log a spurious Warn, it makes
// that ratio unreadable. The refusal cases are the reachable ones (an RFC 0050
// disable or a `max_rounds` raise landing inside a floor round that can span
// minutes), and each would repeat on every subsequent tail while the crossing
// stands. The tombstone-CAS loser and the sibling that loses the arm CAS are
// the same shape, unreachable deterministically from here — all four are
// excluded at once by reporting from [ChannelRouter.boundedClose]'s winning
// branch rather than from the trigger.
func TestBoundedClose_ClampIsSilentWhenTheCloseIsRefused(t *testing.T) {
	const budget = int64(60_000) // clamped for channelSize 3, as above

	for _, tc := range []struct {
		name  string
		fresh AutonomousConfig
	}{{
		// The operator took manual control mid-round: no close, and no report
		// of one.
		name:  "disabled",
		fresh: AutonomousConfig{Enabled: false},
	}, {
		// The operator extended the discussion mid-round: the crossing is
		// re-covered by the raised bound, the tally survives, nothing closes.
		name:  "max_rounds raised",
		fresh: AutonomousConfig{Enabled: true, MaxRounds: 5, Convener: "ember-owl"},
	}} {
		t.Run(tc.name, func(t *testing.T) {
			router, ch, reader := reserveHarness(t, 2)
			router.SetInteractionBudgetTokens(ch, budget)
			router.SetInteractionSpender(fakeSpender{v: 0}) // structural, not cost
			require.True(t, wallet.SynthesisReserveClamped(budget, wallet.CloseRecordUpperBound(3)),
				"fixture must actually clamp, or the test proves nothing")

			tick(t, router, ch) // round 1 on the config the head snapshot captures
			stale := router.AutonomousFor(ch)
			require.Equal(t, 2, stale.MaxRounds)
			members, err := router.store.GetMembers(context.Background(), ch)
			require.NoError(t, err)

			// The config change lands mid-round; the tail runs on the stale
			// snapshot and crosses the bound, then re-reads at the action point.
			router.SetAutonomous(ch, tc.fresh)
			closed, staleVerdict := router.maybeBoundedClose(context.Background(),
				ChannelMessage{ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "continue"},
				ChannelTypeGroup, members, len(members), false, nil, stale)

			require.False(t, closed, "the fresh config refuses the crossed bound")
			require.False(t, staleVerdict, "…and withholds nothing — the message is live traffic")
			require.Zero(t, closedCount(t, reader, structuralTrigger), "no close fired")
			assert.Zero(t, reserveClampedCount(t, reader, structuralTrigger),
				"a crossed bound that closed nothing must report no clamped close — the "+
					"counter is read as a rate against interaction_closed (ISSUE-0138)")
			assert.Zero(t, reserveClampedCount(t, reader, costTrigger))
		})
	}
}

// TestBoundedClose_ClampedReserveIsReportedOnTheTimeoutClose: the signal follows
// the close through an ARM. On a chaired channel the bound does not close
// inline — it dispatches the synthesis turn and waits — so a report emitted at
// the trigger would fire for an arm that a mid-arm abandon can still leave
// open. Reporting from `boundedClose` means the timeout net's fallback close
// carries it instead, off the arm-time room the reserve was carved from.
func TestBoundedClose_ClampedReserveIsReportedOnTheTimeoutClose(t *testing.T) {
	const budget = int64(60_000)

	router, ch, reader := reserveHarness(t, 2)
	router.SetEscalationChair(ch, "iron-fox") // chaired: the bound ARMS, not closes
	router.SetInteractionBudgetTokens(ch, budget)
	router.SetInteractionSpender(fakeSpender{v: 0})
	router.synthesisTimeout = 5 * time.Millisecond

	tick(t, router, ch)
	tick(t, router, ch) // the max_rounds round: arms the synthesis close

	// Wait on the CLAMP counter, not on interaction_closed. The close runs on
	// the timer goroutine and bumps interaction_closed one statement BEFORE the
	// clamp report, so a poll that collects between the two would satisfy a
	// wait keyed on the close and then read a zero clamp — a real, if narrow,
	// flake. Waiting on the assertion's own subject removes the window; the
	// close count is then asserted after the fact, where it cannot race.
	require.Eventually(t, func() bool {
		return reserveClampedCount(t, reader, structuralTrigger) == 1
	}, 2*time.Second, 2*time.Millisecond,
		"the clamp rides the close that actually fired, not the bound that armed it")
	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger),
		"the timeout net closed without the artifact")
}

// TestBoundedClose_NoWalletNeverReportsAClamp: a fleet with no wallet draws no
// close-path lease, so no summary can be denied and there is nothing to report
// — even though the channel carries a cap.
//
// The combination is not hypothetical. `interaction_budget_tokens` is CHANNEL
// config (RFC 0050) and MANDATORY on an autonomous channel, while the wallet is
// wired only when the deployment has cost config at all — cmd/orchestrator
// skips SetInteractionSpender when `walletSvc == nil`. Without the gate the
// documented no-cost-config deployment running a bundled autonomous blueprint
// would report a clamped close on EVERY close, at a rate against
// `interaction_closed` of exactly 1.0, which is the one number ISSUE-0138 reads.
func TestBoundedClose_NoWalletNeverReportsAClamp(t *testing.T) {
	const budget = int64(60_000) // the clamped cap of the tests above
	require.True(t, wallet.SynthesisReserveClamped(budget, wallet.CloseRecordUpperBound(3)),
		"fixture must be arithmetically clamped, or it proves nothing about the wallet gate")

	router, ch, reader := reserveHarness(t, 2)
	router.SetInteractionBudgetTokens(ch, budget)
	// No SetInteractionSpender: this fleet has no wallet.

	tick(t, router, ch)
	tick(t, router, ch)

	require.Equal(t, int64(1), closedCount(t, reader, structuralTrigger),
		"max_rounds still bounds the close with no wallet — only the soft-budget trigger is inert")
	assert.Zero(t, reserveClampedCount(t, reader, structuralTrigger),
		"a fleet that takes no lease cannot have one denied, so the clamp signal must stay silent")
	assert.Zero(t, reserveClampedCount(t, reader, costTrigger))
}

// TestBoundedClose_ClampWarnsOncePerConfiguration — the counter and the log line
// have deliberately different cadences, and this is the half the counter cannot
// show. The clamp is a function of the room size and the cap alone, so a
// per-close Warn on a permanently clamped channel repeats verbatim forever,
// which is how an operator learns to filter this package and loses its genuine
// per-close warnings with it. The counter still fires every close (its
// ISSUE-0138 rate depends on that); the Warn fires once per configuration, and
// re-arms when the operator changes one.
func TestBoundedClose_ClampWarnsOncePerConfiguration(t *testing.T) {
	const budget = int64(60_000)

	router, ch, reader, logs := reserveHarnessWithLogs(t, 2)
	router.SetInteractionBudgetTokens(ch, budget)
	router.SetInteractionSpender(fakeSpender{v: 0})

	// Two full interactions: the close retires the id, so the next tick pair
	// mints a fresh one and closes it against the same clamped configuration.
	for range 2 {
		tick(t, router, ch)
		tick(t, router, ch)
	}
	require.Equal(t, int64(2), closedCount(t, reader, structuralTrigger), "two closes fired")
	assert.Equal(t, int64(2), reserveClampedCount(t, reader, structuralTrigger),
		"the COUNTER is per close — ISSUE-0138 reads it as a rate against interaction_closed")
	assert.Equal(t, 1, clampWarnCount(logs),
		"the WARN is per configuration: the second close adds nothing an operator has not read")

	// An operator edit to the cap is a new configuration, and warns again —
	// otherwise a change that made things worse would land silently.
	router.SetInteractionBudgetTokens(ch, budget/2)
	tick(t, router, ch)
	tick(t, router, ch)

	require.Equal(t, int64(3), closedCount(t, reader, structuralTrigger))
	assert.Equal(t, 2, clampWarnCount(logs),
		"a cap edit re-arms the warn — the line is worth reading when the configuration changes")
}

// TestBoundedClose_UncappedChannelNeverReportsAClamp: an uncapped interaction
// carves no reserve, so it can clamp nothing. The structural terminator still
// closes it, and the signal must stay quiet — warning there would point an
// operator at a cap they never set.
func TestBoundedClose_UncappedChannelNeverReportsAClamp(t *testing.T) {
	router, ch, reader := reserveHarness(t, 2)
	// No SetInteractionBudgetTokens: the interaction is uncapped.
	router.SetInteractionSpender(fakeSpender{v: 10_000_000})

	tick(t, router, ch)
	tick(t, router, ch)

	require.Equal(t, int64(1), closedCount(t, reader, structuralTrigger))
	assert.Zero(t, reserveClampedCount(t, reader, structuralTrigger))
	assert.Zero(t, reserveClampedCount(t, reader, costTrigger))
}

// TestOrchestratorDispatchSenders_MatchTheReserveSpeakerAllowance is the
// cross-package pin behind [wallet.ControlSenderSpeakers].
//
// The reserve multiplier's speaker axis is `channelSize + ControlSenderSpeakers`
// because the record key's speaker half is the ingested event's `sender_id`, and
// the orchestrator authors forced-turn directives under SYNTHETIC senders that
// hold no seat — so each opens a close record of its own on the persona it was
// directed at, and the room-wide fan closes and meters it. The wallet cannot
// import this package (that is the cycle direction), so the count lives there as
// a constant and its justification lives here as a scan.
//
// A THIRD synthetic sender must raise that constant. This test is what says so:
// it is structural, like [TestRestamp_IsTheOnlyPrincipalStampInThisPackage], so
// a new sender cannot be added by omission — the alternative, a hand-written
// list inside the test, would stay green for exactly the change it exists to
// catch. Under-counting here under-sizes every reserve in the fleet, and the
// shortfall is the silent kind (a denied close-path lease commits the RFC 0020
// janitor's unavailable placeholder, which nothing retries).
func TestOrchestratorDispatchSenders_MatchTheReserveSpeakerAllowance(t *testing.T) {
	// The sentinel prefix: `:` is forbidden in a participant id, which is what
	// makes these ids un-impersonable AND un-seatable (convene.go).
	const orchestratorSenderPrefix = "orchestrator:"

	entries, err := os.ReadDir(".")
	require.NoError(t, err)
	fset := token.NewFileSet()
	var senders []string
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		file, pErr := parser.ParseFile(fset, filepath.Join(".", name), nil, 0)
		require.NoError(t, pErr, "parsing %s", name)
		ast.Inspect(file, func(n ast.Node) bool {
			lit, ok := n.(*ast.BasicLit)
			if !ok || lit.Kind != token.STRING {
				return true
			}
			v, uErr := strconv.Unquote(lit.Value)
			if uErr != nil || !strings.HasPrefix(v, orchestratorSenderPrefix) {
				return true
			}
			senders = append(senders, v)
			return true
		})
	}
	slices.Sort(senders)
	senders = slices.Compact(senders)

	assert.Equal(t, []string{ConveneDispatchSenderID, SynthesisDispatchSenderID}, senders,
		"the reviewed set of synthetic control senders; each keys close records of its own")
	assert.Equal(t, len(senders), wallet.ControlSenderSpeakers,
		"wallet.ControlSenderSpeakers is the speaker-axis allowance for exactly these senders "+
			"— adding a third without raising it under-sizes the close reserve silently")
	for _, id := range senders {
		assert.Error(t, ValidateParticipantID(id),
			"%s must not be a valid participant id: the allowance assumes it holds no seat, "+
				"so a seated sender would be double-counted", id)
	}
}
