package metrics

import (
	"fmt"

	"go.opentelemetry.io/otel/metric"
)

// registerChannelInstruments wires the RFC 0011 channels-subsystem
// counters onto i. Split out of [NewInstruments] so the channels
// counters live in one place rather than padding metrics.go past the
// 500-line review limit (same precedent as audit_instruments.go).
//
// Both counters share the `channel.messages` namespace per RFC 0019 §F.
// `channel.messages.delivered{channel_type, status}` is per-recipient;
// `channel.messages.published{channel_type}` is per-publish (post-commit,
// pre-fanout). The pair powers the delivered/published ratio dashboard
// described in docs/observability.md §11.3 (ISSUE-0013).
func registerChannelInstruments(m metric.Meter, i *Instruments) error {
	var err error
	if i.ChannelMessagesDelivered, err = m.Int64Counter(
		"channel.messages.delivered",
		metric.WithUnit("{message}"),
		metric.WithDescription(
			"Per-subscriber channel-router dispatch attempts, labelled by channel_type and status.",
		),
	); err != nil {
		return fmt.Errorf("create channel.messages.delivered: %w", err)
	}
	if i.ChannelMessagesPublished, err = m.Int64Counter(
		"channel.messages.published",
		metric.WithUnit("{message}"),
		metric.WithDescription("Channel-router accepted publishes, labelled by channel_type."),
	); err != nil {
		return fmt.Errorf("create channel.messages.published: %w", err)
	}
	if i.ChannelMessagesCascadeCapped, err = m.Int64Counter(
		"channel.messages.cascade_capped",
		metric.WithUnit("{message}"),
		metric.WithDescription(
			"Per-recipient channel-router fanout dispatches suppressed by the cascade-depth cap, labelled by channel_type. RFC 0011 amendment 'Cascade-depth wire propagation'.",
		),
	); err != nil {
		return fmt.Errorf("create channel.messages.cascade_capped: %w", err)
	}
	// RFC 0030 Layer 2.5 (floor control / speaker serialization). Two
	// instruments under the `channel.conversation` namespace make the
	// serialization's latency cost and timeout rate observable, so the
	// per-turn timeout default (amendment D2, 45s) and the no-cap decision
	// (D4) become data-driven rather than guesses.
	//
	// `floor_turn{channel_type, outcome}` (outcome ∈ {replied, timeout})
	// counts one per completed speaker turn; the timeout share is the
	// stalled-floor-holder rate that calibrates D2. `floor_round_duration`
	// is the per-round wall-clock (ms; RFC 0019 §F histogram unit) — the
	// serialization latency trade made visible, and the trigger to revisit
	// D4 if large-channel rounds become slow.
	if i.ChannelConversationFloorTurn, err = m.Int64Counter(
		"channel.conversation.floor_turn",
		metric.WithUnit("{turn}"),
		metric.WithDescription(
			"Completed RFC 0030 floor-control speaker turns, labelled by channel_type and outcome (replied|timeout).",
		),
	); err != nil {
		return fmt.Errorf("create channel.conversation.floor_turn: %w", err)
	}
	// Buckets span a sub-second single-speaker round through a multi-minute
	// round where several candidates each burn the full 45s turn timeout
	// (a round of N silent candidates blocks for up to N×timeout).
	if i.ChannelConversationFloorRoundDuration, err = m.Float64Histogram(
		"channel.conversation.floor_round_duration",
		metric.WithUnit("ms"),
		metric.WithDescription(
			"Wall-clock duration of a serialized RFC 0030 floor-control round, labelled by channel_type.",
		),
		metric.WithExplicitBucketBoundaries(
			50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 120000, 300000,
		),
	); err != nil {
		return fmt.Errorf("create channel.conversation.floor_round_duration: %w", err)
	}
	// RFC 0030 deterministic governance layers (v0.3.8). One increment per
	// publish dropped by a governance layer, labelled by `channel_type` and
	// `layer` (`reply_budget` in PR 3; `depth`/`end_vote` join as PR 5 wires the
	// channel-owned composition surface; the wallet-side `cost` label is reserved
	// and not yet emitted — it lands with the budget-stamping follow-up). Feeds the
	// governance-drop dashboard that makes "who got throttled and by which layer"
	// observable (§L).
	if i.ChannelConversationGovernanceDrop, err = m.Int64Counter(
		"channel.conversation.governance_drop",
		metric.WithUnit("{message}"),
		metric.WithDescription(
			"Channel publishes dropped by an RFC 0030 deterministic governance layer, labelled by channel_type and layer.",
		),
	); err != nil {
		return fmt.Errorf("create channel.conversation.governance_drop: %w", err)
	}
	// RFC 0030 Layer 4 (v0.3.8) end-of-interaction signal. One increment per
	// interaction closed by a governance layer, labelled by `channel_type` and
	// `trigger` (`end_votes` today; `idle`/`structural`/`cost` join as their close
	// paths are wired). Feeds the convergence dashboard that makes "how did this
	// conversation end" observable (§L).
	if i.ChannelConversationInteractionClosed, err = m.Int64Counter(
		"channel.conversation.interaction_closed",
		metric.WithUnit("{interaction}"),
		metric.WithDescription(
			"Interactions closed by an RFC 0030 governance layer, labelled by channel_type and trigger.",
		),
	); err != nil {
		return fmt.Errorf("create channel.conversation.interaction_closed: %w", err)
	}
	// Chair-stall-escalation amendment (RFC 0030 minimal Layer 5 slice,
	// v0.3.8). One increment per DETECTED floor-round stall, labelled by
	// `channel_type` and `outcome` (dispatched / no_chair / already_escalated
	// / dispatch_error / self_stimulus) — disposition after detection, so
	// operators see the stalls a chair could be configured for, not only
	// fired escalations.
	if i.ChannelConversationChairEscalation, err = m.Int64Counter(
		"channel.conversation.chair_escalation",
		metric.WithUnit("{stall}"),
		metric.WithDescription(
			"Detected floor-round stalls, labelled by channel_type and escalation outcome.",
		),
	); err != nil {
		return fmt.Errorf("create channel.conversation.chair_escalation: %w", err)
	}
	// End-vote-close-propagation amendment (RFC 0030 §H follow-up, v0.3.8).
	// One increment per per-recipient close-notification dispatch, labelled
	// by `channel_type` and `outcome` (dispatched / dispatch_error) — CP5's
	// entire observable surface for the fire-and-forget delivery of an
	// `end_votes` close to the room.
	if i.ChannelConversationCloseNotification, err = m.Int64Counter(
		"channel.conversation.close_notification",
		metric.WithUnit("{dispatch}"),
		metric.WithDescription(
			"Per-recipient end-vote close-notification dispatches, labelled by channel_type and outcome.",
		),
	); err != nil {
		return fmt.Errorf("create channel.conversation.close_notification: %w", err)
	}
	// RFC 0030 Layer 4 (v0.3.8) vote-volume counter. One increment per
	// end-of-interaction vote action, labelled by `channel_type`. Paired with
	// interaction_closed it shows how many votes were cast versus how many
	// interactions actually reached quorum and converged (§L).
	if i.ChannelConversationEndVoteEmitted, err = m.Int64Counter(
		"channel.conversation.end_vote_emitted",
		metric.WithUnit("{vote}"),
		metric.WithDescription(
			"RFC 0030 Layer 4 end-of-interaction vote actions, labelled by channel_type.",
		),
	); err != nil {
		return fmt.Errorf("create channel.conversation.end_vote_emitted: %w", err)
	}
	// RFC 0030 Layer 2 (v0.3.8) reply-budget headroom histogram. At interaction
	// close, each tracked participant's leftover allowance (K - replies_used) on a
	// capped channel is recorded, labelled by `channel_type`. A tail near zero
	// diagnoses a too-tight budget; a tail near K diagnoses a slack one (§L). The
	// bucket boundaries span a handful of leftover replies (typical small caps)
	// through a generous double-digit headroom.
	if i.ChannelConversationReplyBudgetRemaining, err = m.Float64Histogram(
		"channel.conversation.reply_budget_remaining",
		metric.WithUnit("{reply}"),
		metric.WithDescription(
			"Per-participant leftover RFC 0030 Layer 2 reply allowance at interaction close, labelled by channel_type.",
		),
		metric.WithExplicitBucketBoundaries(0, 1, 2, 3, 5, 8, 13, 21, 34),
	); err != nil {
		return fmt.Errorf("create channel.conversation.reply_budget_remaining: %w", err)
	}
	// RFC 0031 Phase 1: per-session write counter. Increments once per
	// CreateChannel / CreateChannelWithMembers / GetOrCreateDM /
	// PublishMessage on the channels store. Labelled by `session_id`.
	// Phase 1 cardinality is bounded by the operator-controlled
	// PERSATRIX_SESSION_ID value; Phase 3 CLI adds a `persatrix session
	// new` write path that further bounds the dimension.
	if i.SessionsWrites, err = m.Int64Counter(
		"sessions.writes",
		metric.WithUnit("{write}"),
		metric.WithDescription(
			"Channels-store write attempts attributed to a session_id (RFC 0031 §F).",
		),
	); err != nil {
		return fmt.Errorf("create sessions.writes: %w", err)
	}
	return nil
}
