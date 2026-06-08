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
	// `layer` (`reply_budget` in PR 3; `cost`/`depth`/`end_vote` join as PR 5
	// wires the full composition surface). Feeds the governance-drop dashboard
	// that makes "who got throttled and by which layer" observable (§L).
	if i.ChannelConversationGovernanceDrop, err = m.Int64Counter(
		"channel.conversation.governance_drop",
		metric.WithUnit("{message}"),
		metric.WithDescription(
			"Channel publishes dropped by an RFC 0030 deterministic governance layer, labelled by channel_type and layer.",
		),
	); err != nil {
		return fmt.Errorf("create channel.conversation.governance_drop: %w", err)
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
