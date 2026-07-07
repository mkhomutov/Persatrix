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
	// governance layer (v0.3.8), labelled by `channel_type` and `layer`. The
	// channels package emits `depth` (cascade cap), `reply_budget` (Layer 2), and
	// `end_vote` (Layer 4 — both a redundant in-window duplicate vote and any
	// publish to an already-closed interaction, each suppressed from fanout);
	// `cost` (Layer 1) is wallet-side and lands with the budget-stamping wiring.
	// Where a drop is anomalous (reply-budget exhaustion, duplicate vote) the
	// per-drop attribution (channel, interaction, participant) is on a Warn line;
	// expected suppression (post-close traffic) is metered without a log. The
	// `conversation.governance.layer` span attribute correlates every drop in a
	// trace (governance.go).
	GovernanceDrop metric.Int64Counter
	// InteractionClosed counts each interaction closed by a governance layer
	// (RFC 0030 Layer 4, v0.3.8), labelled by `channel_type` and `trigger`
	// (`end_votes` today; `idle`/`structural`/`cost` join as their close paths are
	// wired). The per-close attribution (channel, interaction, the vote count) is
	// on the structured log line.
	InteractionClosed metric.Int64Counter
	// ChairEscalation counts chair-escalation lifecycle events, labelled by
	// `channel_type` and `outcome`. The stall dispositions — `{dispatched,
	// no_chair, already_escalated, dispatch_error, self_stimulus}` — are one
	// per DETECTED floor-round stall (the chair-stall-escalation amendment
	// §C 1), emitted after the disposition chain so an operator sees the stalls
	// a chair could be configured for, not only the escalations that fired.
	// `self_stimulus` (PR #609 deep review) is the withheld forced turn whose
	// stalled stimulus the chair itself authored. The remaining two outcomes —
	// `{resynthesized, resynthesize_error}` (ISSUE-0099) — fire at the
	// chair-reply publish seam, NOT the round tail, when the chair's forced-turn
	// reply provably reached nobody and one synthesize-only turn is re-forced
	// (or its re-dispatch failed). They are distinct lifecycle labels, kept off
	// dispatch_error precisely so summing the stall dispositions stays a stall
	// count.
	ChairEscalation metric.Int64Counter
	// ConvenerAdvance counts RFC 0052 §C anti-collapse cadence events (v0.3.11
	// PR 6), labelled by `channel_type` and `outcome ∈ {advance, reinvite,
	// dispatch_error}`. One increment per convener forced turn the fanout tail
	// dispatches on a stalled AUTONOMOUS floor round: `reinvite` re-poses an
	// under-discussed current agenda item (the best-effort liveness target),
	// `advance` moves to the next item (the per-item ration), `dispatch_error`
	// the drifted-convener/send failure that leaves the stall standing. An
	// agenda-exhausted stall dispatches NO convener turn (it falls through to the
	// chair escalation, counted on ChairEscalation instead), so the sum of these
	// is the convener's total keep-alive turns, agenda-length-bounded per the loop
	// guard. Scoped to `autonomous.enabled` — a human channel never increments it.
	ConvenerAdvance metric.Int64Counter
	// SynthesisTurn counts RFC 0052 §D synthesis-turn lifecycle events
	// (PR 4b-ii), labelled by `channel_type` and `outcome`. `dispatched` fires
	// once per armed close-on-reply; `chair_missing` / `dispatch_error` label
	// the branches that degrade to the immediate artifact-less close; exactly
	// one of `closed_on_reply` / `closed_on_timeout` follows a `dispatched`
	// (unless a racing end-vote close orphans the arm, which counts on
	// InteractionClosed{end_votes} instead). The reply-vs-timeout ratio is the
	// §D health signal: a rising timeout share means chairs are losing their
	// synthesis to the net (lease denial, gate drift) and the OQ #5
	// calibration needs a look.
	SynthesisTurn metric.Int64Counter
	// CloseNotification counts each per-recipient close-notification dispatch
	// (the end-vote-close-propagation amendment, CP5), labelled by
	// `channel_type` and `outcome ∈ {dispatched, dispatch_error}`. A member
	// excluded by contract (the closing sender, a `RespondNever` row) is not
	// a recipient and does not count; a missing entry where a close happened
	// therefore reads "nobody to notify", not "notification lost".
	CloseNotification metric.Int64Counter
	// EndVoteEmitted counts each RFC 0030 Layer 4 end-of-interaction vote action,
	// labelled by `channel_type` (§L). Pairs with InteractionClosed to make
	// "votes cast vs. interactions that actually converged" observable on the
	// convergence dashboard. Every vote increments it once — the first vote, a
	// stale re-vote, and a deduped in-window re-vote alike — so it measures vote
	// VOLUME, distinct from the quorum the close counter measures.
	EndVoteEmitted metric.Int64Counter
	// ReplyBudgetRemaining records, at interaction close, each tracked
	// participant's leftover Layer 2 reply allowance (`K - replies_used`) for a
	// capped channel, labelled by `channel_type` (§L). A tail concentrated near
	// zero says the budget is too tight (participants routinely hit the cap); a
	// fat tail near K says it is slack. Recorded on the close path before the
	// per-interaction counters are discarded, so it observes the final state.
	ReplyBudgetRemaining metric.Float64Histogram
}
