package channels

import "context"

// DispatchEnvelope bundles the per-recipient inputs the dispatcher needs
// to render a [taskpb.ChannelMessageEvent] without the router exposing the
// raw proto type to the channels package boundary. The envelope is built
// once per-recipient inside [ChannelRouter.fanout]:
//
//   - `Recipient` carries the per-recipient `RespondPolicy` (and, since RFC
//     0030 Tier B, `SalienceGated`/`Threshold`) so the receiver-side response
//     gate + salience bid can decide pre-LLM (RFC 0011 PR 4b / RFC 0030).
//   - `ThreadParentSenderID` is pre-resolved once per publish in
//     [ChannelRouter.Publish] so a thread-heavy channel pays one
//     `GetMessage` lookup per publish, not one per recipient (RFC 0011
//     PR plan §PR 4 — "amortizes the lookup across fanout").
//   - `ChannelSize`/`SalienceMaxChannelMembers` are per-publish values the
//     router stamps once per fanout (identical across recipients).
//
// Adding fields here is an additive change to the dispatcher contract
// and does not require touching every test seam — every field defaults
// to its zero value when unset.
type DispatchEnvelope struct {
	// Recipient is the membership row of the agent receiving this
	// dispatch. The router has already filtered the sender and any
	// `RespondNever` entries upstream of [MessageDispatcher.Dispatch],
	// so `Recipient.RespondPolicy` is always one of [RespondAlways]
	// or [RespondWhenMentioned].
	Recipient Member

	// ThreadParentSenderID is the sender id of the message addressed
	// by [ChannelMessage.ThreadID], pre-resolved by the router. Empty
	// for non-thread events. The empty-string default for proto3
	// strings is preserved on the wire so receivers can branch on
	// `thread_id != "" && thread_parent_sender_id != ""` without a
	// secondary lookup.
	ThreadParentSenderID string

	// ChannelSize (RFC 0030 Tier B) is the channel's member count at publish
	// time, carried on `ChannelMessageEvent.channel_size` so the agent-side
	// seam can apply the TB6 channel-size cap. Per-publish (identical across
	// recipients), not per-recipient.
	ChannelSize int

	// SalienceMaxChannelMembers (RFC 0030 Tier B) is the channel's resolved
	// channel-size cap, carried on `ChannelMessageEvent.salience_max_channel_members`.
	// The router resolves it from config at fanout ([ChannelRouter.salienceMaxFor]);
	// zero falls back to the agent-side default. Per-publish, like ChannelSize.
	SalienceMaxChannelMembers int

	// ReasoningMode (RFC 0051 PR 6 go-live) is the channel's resolved
	// reasoning-before-posting rung — `off` | `bid` | `plan` — carried on
	// `ChannelMessageEvent.reasoning_mode` so the agent-side salience seam picks
	// the bid's verdict grammar. The router resolves it from the (already
	// flip-aware) [ChannelRouter.ReasoningFor] at fanout, so an inherit governed
	// channel is `bid` and an explicit `off` stays the kill switch. Channel-level
	// (identical across recipients), like ChannelSize; the empty string is the
	// pre-v0.3.10 / untracked case the agent maps to `off`.
	ReasoningMode string

	// ReasoningRevise (RFC 0051 PR 8, Phase 5a) is the channel's resolved reflexion
	// round count (`reasoning.revise`, 0..2), carried on
	// `ChannelMessageEvent.reasoning_revise` so the agent-side reflexion loop bounds
	// its critic→revise rounds after compose. Resolved from the same
	// [ChannelRouter.ReasoningFor] as ReasoningMode at fanout; channel-level. 0 (the
	// default / pre-Phase-5 case) is single-pass, and it is meaningful only paired
	// with ReasoningMode `plan` — the seam pins it to 0 off the plan path.
	ReasoningRevise int

	// ChairEscalation (the chair-stall-escalation amendment, CE3) marks this
	// dispatch as the orchestrator's forced turn to the channel's configured
	// escalation chair after a stalled floor round. Carried on
	// `ChannelMessageEvent.chair_escalation`; the receiver routes a marked
	// event down the directed lane (gate admit + Tier B bypass). Never set on
	// ordinary fanout — the zero value is every non-escalation dispatch.
	ChairEscalation bool

	// ChairEscalationResynthesize (ISSUE-0099) refines ChairEscalation: set
	// together with it on the SECOND forced turn — the one the orchestrator
	// re-dispatches after the chair's first forced-turn reply provably reached
	// no floor-capable member. Carried on
	// `ChannelMessageEvent.chair_escalation_resynthesize`; the receiver's
	// admission is unchanged (it keys on ChairEscalation), so this flips only
	// the persona framing to the synthesize-only variant. Never set without
	// ChairEscalation, and never on ordinary fanout. Set only by
	// [ChannelRouter.dispatchResynthesizeMisfire].
	ChairEscalationResynthesize bool

	// InteractionCloseNotification (the end-vote-close-propagation
	// amendment, CP2) marks this dispatch as the orchestrator's close
	// notification: the closing quorum vote re-dispatched to a
	// dispatch-served non-sender member after an `end_votes` close, whose
	// ordinary fanout is suppressed. Carried on
	// `ChannelMessageEvent.interaction_close_notification`; the receiver
	// treats a marked event as control, never stimulus (gate refusal
	// pre-LLM + immediate local-tracker close, CP3). Set only by
	// [ChannelRouter.notifyInteractionClose] — the zero value is every
	// ordinary dispatch.
	InteractionCloseNotification bool

	// Convene (RFC 0052 §B) marks this dispatch as the orchestrator's convene
	// forced turn to the channel's configured `autonomous.convener` — the
	// directed dispatch that opens an autonomous agent-only channel. Carried
	// on `ChannelMessageEvent.convene`; the receiver admits a marked event
	// down the same directed lane as `ChairEscalation` (gate admit + Tier B
	// bypass) and renders the convener framing, then authors the opening turn
	// from which the existing `InboundEventWake` chain carries the discussion.
	// Set only by [ChannelRouter.ConveneChannel]'s dispatch — false (the
	// proto3 default) on every ordinary fanout. Never set together with
	// ChairEscalation/InteractionCloseNotification (a convene is its own
	// directed lane, the same never-alias discipline [dispatchMarker] keeps).
	Convene bool

	// InteractionCloseRedelivery (RFC 0052 PR 4b-ii) marks a close
	// notification whose closing message was ALREADY delivered live via
	// ordinary fanout — the FLOOR-path bounded close, whose bounding stimulus
	// reached every member inside its round (the end-vote and concurrent-path
	// closes are sole-delivery). Carried on
	// `ChannelMessageEvent.close_notification_redelivery`; the receiver skips
	// the duplicate final-turn ingest and closes its scope directly. Set only
	// with InteractionCloseNotification, by
	// [ChannelRouter.notifyInteractionClose] — the zero value is every
	// sole-delivery notification and every ordinary dispatch.
	InteractionCloseRedelivery bool

	// InteractionCloseTrigger (RFC 0052 PR 4b-ii) is the truthful bounded-
	// close cause riding a close notification — "structural"
	// (`autonomous.max_rounds`) | "cost" (the wallet soft budget), the same §L
	// vocabulary as the OQ 5 retiree pair. Stamped ONLY for the RFC 0052
	// bounded close ([ChannelRouter.finalizeInteractionClose] maps every other
	// trigger to empty), so its presence doubles as the receiver's OQ #6
	// metering key: a non-empty value marks the closed interaction's RFC 0020
	// summary for a wallet lease against the mandatory cap. Carried on
	// `ChannelMessageEvent.close_notification_close_trigger`; empty is every
	// end-vote/idle notification and every ordinary dispatch.
	InteractionCloseTrigger string

	// SynthesisTurn (RFC 0052 §D, PR 4b-ii) marks this dispatch as the
	// orchestrator's synthesis forced turn to the channel's escalation chair —
	// the directed dispatch, sent when the bounded close trips, asking the
	// chair to author the goal-directed closing synthesis. Carried on
	// `ChannelMessageEvent.synthesis_turn`; the receiver admits a marked event
	// down the same directed lane as Convene (gate admit + Tier B bypass),
	// wraps the operator goal in the RFC 0009 envelope, and renders the
	// synthesis framing. The reply's echoed interaction-id claim is how the
	// orchestrator recognises the closing artifact (close-on-reply). Set only
	// by [ChannelRouter.maybeArmSynthesisClose]'s dispatch — never together
	// with the other control markers (the [dispatchMarker] never-alias
	// discipline).
	SynthesisTurn bool

	// Classification (RFC 0037 §B, v0.3.12 PR 2) is the dispatching
	// channel's §A confidentiality level, resolved from the `channels` row
	// per dispatch ([ChannelRouter.classificationFor]) and carried on
	// `ChannelMessageEvent.classification` so the persona runtime can run
	// the §D injection gate per turn without a channel-metadata roundtrip.
	// Channel-level (identical across a fanout's recipients), like
	// ChannelSize. Empty when the row read failed — the receiver resolves
	// empty to the `public` acting floor (§A rule (b)), so a resolve
	// failure can only UNDER-inject. Dark in PR 2: no receiver reads it
	// for gating until the PR 4 gate arms.
	Classification string

	// FloorMentions (RFC 0030 floor-capable-directedness amendment) is the
	// subset of the message's mentions naming floor-capable members —
	// resolved once per publish by [resolveFloorMentions] in
	// [ChannelRouter.fanout] and carried on
	// `ChannelMessageEvent.floor_mentions` as the receiver gate's Tier A
	// suppression basis. Per-publish (identical across recipients), like
	// ChannelSize. Nil/empty means "no floor-capable mention" — a real,
	// load-bearing value (the reclassified-to-open-floor case), which is why
	// the dispatcher pairs it with the unconditional
	// `floor_mentions_resolved` wire flag rather than letting receivers
	// infer producer support from emptiness.
	FloorMentions []string

	// ExpectsReply (ISSUE-0124 / ISSUE-0082 residual R-2) reports whether the
	// router ELECTED this recipient to take a turn, as opposed to delivering
	// the message to it for ingestion only. True for the members
	// [orderResponders] returns as responders, for the floor round's granted
	// speaker, and for the four orchestrator-authored FORCED turns (chair
	// escalation, its resynthesize refinement, convene, synthesis); false for
	// the ingestion-only recipients [ChannelRouter.dispatchConcurrent] also
	// delivers to and for the close-notification fan.
	//
	// SERVER-SIDE ONLY — deliberately not rendered onto the wire by
	// [GRPCMessageDispatcher.channelMessageToProto]. The receiver already
	// decides whether to answer, from `respond_policy` + `floor_mentions` +
	// its own salience bid; this is the ORCHESTRATOR's view of the same
	// question, and it exists so the causal-attribution write records only
	// stimuli that can actually produce a reply (principal_attribution.go).
	// Shipping it would invite a receiver to defer to it and make the two
	// answers drift.
	ExpectsReply bool
}

// MessageDispatcher is the gRPC seam through which the [ChannelRouter]
// fans a published message out to every subscriber other than the sender.
// (Moved here from router.go beside the envelope it consumes when the PR #718
// follow-up review's delivery-miss contract expansion pushed that file past
// the 500-line cap.)
//
// PR 2 of RFC 0011 ships only the dispatcher *interface* and a no-op
// implementation. The wire-side gRPC call to `ReceiveChannelMessage`
// (proto regen + servicer) lands in PR 3 + PR 4 — splitting the seam from
// its first concrete implementation keeps the PR diff under the 500-line
// soft cap and lets the router unit tests exercise the fanout topology
// without booting a fake gRPC server.
//
// Implementations MUST treat `Dispatch` as fire-and-forget: the publish
// path's HTTP response has already been written by the time fanout runs.
// Errors returned here are recorded via the
// `channel.messages.delivered{status="error"}` counter and logged at warn,
// but do not surface to the publisher.
type MessageDispatcher interface {
	// Dispatch delivers msg to env.Recipient. The router has already
	// filtered the sender out of the recipient list, dropped any
	// `RespondNever` members, and validated `channel_type` against the
	// `channel_id` prefix. Returns an error whenever the message did NOT
	// reach the recipient — an unknown/unhealthy target, a wire failure,
	// or a receiver ack that refused the event; the caller logs, counts,
	// and (on the floor path) records the miss in the bounded-close
	// undelivered ledger. A nil return MUST mean the recipient actually
	// received the event (PR #718 review — a tolerant nil corrupted the
	// close-notification redelivery accounting).
	Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error
}

// NoopDispatcher is the v0.3.0-PR-2 placeholder: it counts the calls and
// returns nil, so the router's fanout topology can be tested end-to-end
// without a wired gRPC client. Replaced in PR 4 by the real gRPC-backed
// dispatcher that resolves participantID → registry address and invokes
// `AgentService.ReceiveChannelMessage`.
type NoopDispatcher struct{}

// Dispatch implements [MessageDispatcher] by no-op.
func (NoopDispatcher) Dispatch(_ context.Context, _ DispatchEnvelope, _ ChannelMessage) error {
	return nil
}
