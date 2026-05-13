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
