package channels

import "go.opentelemetry.io/otel/metric"

// router_metrics.go holds the [RouterMetrics] OTEL-handle struct. Split out of
// router.go so that file stays under the 500-line review cap (same precedent as
// reply_budget.go / router_salience.go); the struct is the router's whole metric
// surface and changes on its own cadence as governance layers land.

// RouterMetrics is the subset of orchestrator OTEL handles the router
// needs. Defined locally (rather than imported from the metrics package)
// so the channels package does not take a dependency on the orchestrator-
// wide instrument struct — that would invert the dependency direction
// (channels is consumed *by* server, not the other way around).
//
// Nil-safe: a nil RouterMetrics value disables metric emission so unit
// tests and minimal deployments can run without OTEL wiring.
type RouterMetrics struct {
	// MessagesDelivered counts each per-subscriber dispatch attempt with
	// labels `channel_type` and `status` (`ok` | `error`). One increment
	// per recipient, not per publish. Sender filtering happens before the
	// counter fires, so the count reflects effective delivery attempts.
	MessagesDelivered metric.Int64Counter
	MessagesPublished metric.Int64Counter // pairs with MessagesDelivered (ISSUE-0013)
	// MessagesCascadeCapped counts per-recipient fanout dispatches
	// suppressed by the cascade-depth cap (RFC 0011 amendment
	// 'Cascade-depth wire propagation'). One increment per suppressed
	// recipient — directly comparable to MessagesDelivered. Labelled by
	// `channel_type`; `channel_id` lives on the structured log line.
	MessagesCascadeCapped metric.Int64Counter
	// FloorTurn counts each completed RFC 0030 Layer 2.5 floor-control
	// speaker turn, labelled by `channel_type` and `outcome`
	// (`replied`|`timeout`). The `timeout` share is the stalled-floor-holder
	// rate that calibrates the per-turn timeout default (amendment D2).
	FloorTurn metric.Int64Counter
	// FloorRoundDuration records the wall-clock duration of a serialized
	// floor round in milliseconds, labelled by `channel_type` — the
	// serialization latency trade made observable (the trigger to revisit
	// the no-cap decision, amendment D4).
	FloorRoundDuration metric.Float64Histogram
	// GovernanceDrop counts each publish dropped by an RFC 0030 deterministic
	// governance layer (v0.3.8), labelled by `channel_type` and `layer`
	// (`reply_budget` in this PR; `cost`/`depth`/`end_vote` join in PR 5). The
	// per-drop attribution (channel, interaction, participant) is on the Warn line.
	GovernanceDrop metric.Int64Counter
	// InteractionClosed counts each interaction closed by a governance layer
	// (RFC 0030 Layer 4, v0.3.8), labelled by `channel_type` and `trigger`
	// (`end_votes` in this PR; `idle`/`structural`/`cost` join as PR 5 wires the
	// other close paths). The per-close attribution (channel, interaction, the
	// vote count) is on the structured log line.
	InteractionClosed metric.Int64Counter
}
