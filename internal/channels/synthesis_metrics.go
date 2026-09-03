package channels

// synthesis_metrics.go — the RFC 0052 §D close-path instruments and the outcome
// vocabulary they are labelled with, split out of synthesis_close.go at the
// 500-line cap (ISSUE-0082 residuals PR 4b, which added the second counter). The
// two belong together and away from the lifecycle: the constants ARE the
// counter's label set, so a new outcome and its emitter land in one file, and
// the close-on-reply ordering next door stays readable as ordering.

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/wallet"
)

// Synthesis-turn lifecycle outcomes on `channel.conversation.synthesis_turn
// {channel_type, outcome}`. `dispatched` fires once per armed close;
// `chair_missing`/`dispatch_error` label the degraded-to-immediate-close
// branches (the shutdown-drain refusal degrades the same way but is
// deliberately unmetered: nothing was dispatched, and the close still counts
// on interaction_closed{…}); exactly one of
// `closed_on_reply`/`closed_on_timeout` follows a `dispatched` (a racing
// end-vote close can orphan the arm — its close is counted on
// interaction_closed{end_votes} — and a mid-arm abandon (the RFC 0050
// disable, the timeout's max_rounds-raise re-check) leaves the interaction
// open and counts nothing; in both shapes neither closed_on_* fires).
const (
	synthesisTurnDispatched      = "dispatched"
	synthesisTurnChairMissing    = "chair_missing"
	synthesisTurnDispatchError   = "dispatch_error"
	synthesisTurnClosedOnReply   = "closed_on_reply"
	synthesisTurnClosedOnTimeout = "closed_on_timeout"
)

// recordSynthesisTurn emits `channel.conversation.synthesis_turn{channel_type,
// outcome}` — see the outcome constants for the lifecycle contract. Nil-safe
// like every other channel instrument.
func (r *ChannelRouter) recordSynthesisTurn(ctx context.Context, ct ChannelType, outcome string) {
	if r.metrics == nil || r.metrics.SynthesisTurn == nil {
		return
	}
	r.metrics.SynthesisTurn.Add(ctx, 1, metric.WithAttributes(
		attribute.String("channel_type", string(ct)),
		attribute.String("outcome", outcome),
	))
}

// reportSynthesisReserveClamp counts and warns when the bounded close that just
// WON its tombstone fired against a half-cap-clamped close-path reserve
// (ISSUE-0082 residuals PR 4b). Called from [ChannelRouter.boundedClose] — the
// one funnel all four bounded-close entry paths pass through — and deliberately
// NOT from the trigger that crossed the bound: a crossing is not a close. The
// tail's fresh-config re-check can refuse it (an RFC 0050 disable, a mid-round
// `max_rounds` raise), the arm CAS can lose it to a sibling fanout, and the
// tombstone CAS can lose it to a racing closer — and each of those used to
// increment this counter for a close that never happened, which matters because
// the ISSUE-0138 calibration reads the counter's RATE AGAINST
// `interaction_closed`. Firing it beside that same counter's bump makes the
// ratio true by construction.
//
// A clamped reserve means the operator's per-interaction cap cannot fund this
// room's close: the close path exhausts the (halved) reserve mid-fan and the
// records whose summary lease is then denied commit the RFC 0020 janitor's
// `"[interaction summary unavailable]"` placeholder, which nothing retries. Both
// triggers report — a `max_rounds` close runs the same close path against the
// same clamped reserve, so gating on `cost` would hide half the failures.
//
// Silent on an uncapped interaction (nothing to carve, so nothing to clamp), on
// an unclamped one (the raw sizing was held back in full), and on a fleet with
// NO WALLET. That last gate is the [ChannelRouter.recordInteractionClosedMetric]
// posture applied here: `r.spend` nil means no wallet was wired
// ([ChannelRouter.SetInteractionSpender] is skipped in cmd/orchestrator when
// `walletSvc == nil`), so no close-path lease is ever drawn and none can be
// denied — while `interaction_budget_tokens` is set by CHANNEL CONFIG, which is
// independent of the wallet and MANDATORY on an autonomous channel. Without the
// gate, the documented no-cost-config deployment running a bundled autonomous
// blueprint reports a clamped close on every close, at a rate against
// `interaction_closed` of exactly 1.0, for a failure it cannot have.
//
// The COUNTER fires on every qualifying close — that is the ISSUE-0138 rate's
// contract. The WARN does not: the clamp is a function of the room size and the
// cap alone, so a clamped channel would otherwise log the same line on every
// close for the life of the deployment, which is how an operator learns to
// filter the channel and loses this package's genuine per-close warnings with
// it. [ChannelRouter.claimClampWarn] reduces it to once per (channel,
// configuration), and a cap or roster edit re-arms it.
func (r *ChannelRouter) reportSynthesisReserveClamp(ctx context.Context, channelID, interactionID string, ct ChannelType, trigger string, channelSize int) {
	if r.spend == nil {
		return
	}
	budget, capped := r.ResolveInteractionBudgetForInteraction(interactionID)
	if !capped || budget <= 0 {
		return
	}
	closeRecords := wallet.CloseRecordUpperBound(channelSize)
	// ONE evaluation of the sizing rule for both halves of the signal — the
	// reserve the Warn names and the verdict that gates it ([wallet.SynthesisReserve]).
	reserve, clamped := wallet.SynthesisReserve(budget, closeRecords)
	if !clamped {
		return
	}
	r.recordSynthesisReserveClamped(ctx, ct, trigger)
	if !r.claimClampWarn(channelID, channelSize, budget) {
		return
	}
	r.logger.Warn("channels: close-path reserve clamped to half the interaction cap; late close summaries may degrade to the unavailable placeholder (ISSUE-0138)",
		zap.String("channel_id", channelID),
		zap.String("interaction_id", interactionID),
		zap.String("trigger", trigger),
		zap.Int("channel_size", channelSize),
		zap.Int("close_records", closeRecords),
		zap.Int64("interaction_budget_tokens", budget),
		zap.Int64("reserve_tokens", reserve),
		zap.Bool("warn_once_per_config", true))
}

// clampWarnKey is the CONFIGURATION a clamp Warn describes: the room the reserve
// was carved for and the cap it was carved from. Everything else on the Warn
// line — the interaction id, the trigger — varies per close and is exactly what
// must NOT re-arm it.
type clampWarnKey struct {
	channelSize int
	budget      int64
}

// claimClampWarn reports whether THIS close should emit the clamp Warn: true the
// first time a channel is seen in a given (room size, cap) configuration, false
// for every subsequent close under the same one. An operator edit to either —
// the cap raised, a member added or removed — is a new key and warns again,
// which is the whole point: the line is worth reading when the configuration
// changes and noise when it does not.
//
// Per PROCESS, not persisted: a restart re-warns once, which is the right
// posture for a startup-visible config problem.
func (r *ChannelRouter) claimClampWarn(channelID string, channelSize int, budget int64) bool {
	key := clampWarnKey{channelSize: channelSize, budget: budget}
	r.clampWarnMu.Lock()
	defer r.clampWarnMu.Unlock()
	if seen, ok := r.clampWarned[channelID]; ok && seen == key {
		return false
	}
	r.clampWarned[channelID] = key
	return true
}

// recordSynthesisReserveClamped emits
// `channel.conversation.synthesis_reserve_clamped{channel_type, trigger}` — the
// ISSUE-0082 residuals PR 4b signal for a close firing against a half-cap-clamped
// close-path reserve ([RouterMetrics.SynthesisReserveClamped]). The predicate and
// the Warn beside it live in [ChannelRouter.reportSynthesisReserveClamp]; this is
// the bare emit. Nil-safe like every other channel instrument.
func (r *ChannelRouter) recordSynthesisReserveClamped(ctx context.Context, ct ChannelType, trigger string) {
	if r.metrics == nil || r.metrics.SynthesisReserveClamped == nil {
		return
	}
	r.metrics.SynthesisReserveClamped.Add(ctx, 1, metric.WithAttributes(
		attribute.String("channel_type", string(ct)),
		attribute.String("trigger", trigger),
	))
}
