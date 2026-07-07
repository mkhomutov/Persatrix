package channels

// grpc_dispatcher_proto.go — [GRPCMessageDispatcher]'s in-process→wire
// translation ([ChannelMessage] + [DispatchEnvelope] →
// [taskpb.ChannelMessageEvent]). Split out of grpc_dispatcher.go when the
// PR #718 review's delivery-miss returns (an unregistered target and a
// refused ack now surface as Dispatch errors) pushed that file past the
// 500-line review cap (the synthesis_disarm.go precedent).
// grpc_dispatcher.go keeps the dispatch LIFECYCLE — registry resolve,
// session/epoch metadata, dial, RPC, ack; this file holds only how one
// accepted dispatch is rendered onto the wire.

import (
	"time"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
)

// channelMessageToProto translates the in-process [ChannelMessage] into the
// wire [taskpb.ChannelMessageEvent]. Timestamp is rendered as RFC 3339 to
// match the proto field's documented contract (see PR #246 deep review M4).
//
// `ChannelType` is re-derived from the channel id prefix here rather than
// added as a struct field — keeping [ChannelMessage] free of a denormalized
// type column matches what the SQLite store persists. The router has already
// validated the prefix once on the publish path, so an unknown prefix at
// dispatch time is a programmer error.
//
// PR #250 review (Medium #4): a translation-time Warn surfaces that
// programmer error at the sender's logs the moment it happens, rather
// than letting an empty `ChannelType` ride the wire to the receiver
// where the origin is opaque. The contract — empty string on unknown
// prefix — is preserved so the receiver's proto-bound validation still
// rejects the message.
//
// RFC 0011 PR 4b: `respond_policy` and `thread_parent_sender_id` are
// pulled from the [DispatchEnvelope] so the receiver's response gate
// can decide pre-LLM without a secondary REST roundtrip. The router
// guarantees `env.Recipient.RespondPolicy` is one of [RespondAlways] or
// [RespondWhenMentioned] — `respond: never` members are filtered out
// upstream of [MessageDispatcher.Dispatch].
func (d *GRPCMessageDispatcher) channelMessageToProto(msg ChannelMessage, env DispatchEnvelope) *taskpb.ChannelMessageEvent {
	ct, ctErr := channelTypeFromID(msg.ChannelID)
	if ctErr != nil {
		d.logger.Warn("channels: unknown channel_id prefix at dispatch translation; sending empty ChannelType (router prefix validation regression?)",
			zap.String("channel_id", msg.ChannelID),
			zap.String("message_id", msg.ID),
			zap.Error(ctErr),
		)
	}
	ts := msg.Timestamp
	if ts.IsZero() {
		ts = time.Now().UTC()
	}
	prevClose := readPreviousClose(msg.Metadata)
	return &taskpb.ChannelMessageEvent{
		MessageId:            msg.ID,
		ChannelId:            msg.ChannelID,
		ChannelType:          string(ct),
		SenderId:             msg.SenderID,
		Content:              msg.Content,
		Timestamp:            ts.UTC().Format(time.RFC3339Nano),
		ThreadId:             msg.ThreadID,
		Mentions:             msg.Mentions,
		RespondPolicy:        string(env.Recipient.RespondPolicy),
		ThreadParentSenderId: env.ThreadParentSenderID,
		// [RFC 0011 amendment 'Cascade-depth wire propagation']: the
		// router's Publish clamped `msg.Metadata["cascade_depth"]` to
		// `[0, maxCascadeDepth]` before persistence, so the int32
		// downcast cannot overflow on a misbehaving publisher. proto3
		// scalars zero-value to 0, which is exactly the chain-origin
		// semantic for a publish that omits the field.
		//
		// [RFC 0011 amendment 'Cascade-depth wire propagation']: ../../docs/rfcs/0011-amendment-cascade-depth-wire-propagation.md
		CascadeDepth: int32(readCascadeDepth(msg.Metadata)),
		// ISSUE-0068 / [RFC 0011 amendment 'Participant-type wire
		// propagation']: lift the publish-side `participant_type`
		// (set by the REST chat handler) onto the typed proto field so
		// the peer type survives this boundary. Empty for ordinary
		// agent-to-agent fanout — the agent resolves that to "agent".
		//
		// [RFC 0011 amendment 'Participant-type wire propagation']: ../../docs/rfcs/0011-amendment-participant-type-wire-propagation.md
		SenderParticipantType: readParticipantType(msg.Metadata),
		// RFC 0030 Tier B (v0.3.8): the per-recipient salience-bid inputs. The
		// bid-ness + threshold ride on the recipient's membership row
		// (resolved at config load / REST add); the channel size + cap are
		// per-publish values the router stamped on the envelope at fanout.
		// `Threshold` is `*float64` → the optional proto field: nil leaves it
		// absent (the agent reads "unset → bias-to-silence"), distinct from an
		// explicit 0.0.
		SalienceGated:             env.Recipient.SalienceGated,
		Threshold:                 env.Recipient.Threshold,
		ChannelSize:               int32(env.ChannelSize),
		SalienceMaxChannelMembers: int32(env.SalienceMaxChannelMembers),
		// RFC 0051 PR 6 go-live: the channel's resolved reasoning rung
		// (`off`|`bid`|`plan`), stamped by the router at fanout. Channel-level
		// (identical across recipients); the agent-side seam reads it to pick the
		// bid's verdict grammar. Empty (a pre-v0.3.10 envelope) maps to `off`.
		ReasoningMode: env.ReasoningMode,
		// RFC 0051 PR 8 (Phase 5a): the channel's resolved reflexion round count,
		// stamped by the router at fanout. Channel-level; the agent reflexion loop
		// reads it to bound critic→revise rounds. 0 (pre-Phase-5) is single-pass.
		ReasoningRevise: int32(env.ReasoningRevise),
		// RFC 0030 deterministic governance layers (v0.3.8), PR 1: lift any
		// publish-side `interaction_id` (the RFC 0020 Interaction — lifecycle
		// §B/§C, per-channel scope §G) onto the typed proto field so Layers
		// 1/2/4 can attribute spend, count replies, and accumulate end-votes
		// per interaction. The metadata value read here is the router's own
		// resolution — `publishCommit` stamped it before persistence
		// (interaction_resolver.go, the interaction-id producer plan IP1) —
		// so the field is non-empty on every routed publish; empty survives
		// only as the untracked defence posture for a dispatch path that
		// bypassed the resolver, and every layer then stays at its uncapped
		// default.
		//
		// [RFC 0030 governance layers]: ../../docs/rfcs/0030-governance-layers-pr-plan.md
		InteractionId: readInteractionID(msg.Metadata),
		// Producer plan OQ 5: the retired predecessor's id + close trigger
		// ("idle"/"end_votes"), stamped by `publishCommit` from the resolver's
		// own close record, so the agent-side rotation close can label the
		// boundary truthfully (idle_gap vs structural) instead of calling
		// every rotation "ended". Lifted as a validated PAIR
		// ([readPreviousClose]) — field 21's contract is "Set iff `= 20` is
		// set", so both are empty when no (valid) retiree is known (fresh
		// channel / post-restart re-mint / a bypassing producer's junk
		// claim) and the receiver keeps its legacy label, the documented
		// mixed-version posture.
		PreviousInteractionId:           prevClose.id,
		PreviousInteractionCloseTrigger: prevClose.trigger,
		// Floor-capable-directedness amendment (v0.3.8): the per-publish
		// suppression basis the router resolved at fanout, plus the
		// unconditional producer-presence flag. The flag is hardcoded true
		// rather than carried on the envelope because "resolved" is a
		// property of this orchestrator version, not of the data — an
		// envelope with a nil FloorMentions is a real "no floor-capable
		// mention" value (the reclassified-to-open-floor case), never an
		// unset one. Receivers seeing false (an old producer) fall back to
		// the raw-mentions basis.
		//
		// [Floor-capable-directedness amendment]: ../../docs/rfcs/0030-amendment-floor-capable-directedness.md
		FloorMentions:         env.FloorMentions,
		FloorMentionsResolved: true,
		// Chair-stall-escalation amendment (CE3): the forced-turn marker,
		// set only by [ChannelRouter.maybeEscalateStall]'s dispatch — false
		// (the proto3 default) on every ordinary fanout.
		ChairEscalation: env.ChairEscalation,
		// Chair-escalation resynthesize refinement (ISSUE-0099): the
		// synthesize-only framing selector, set with ChairEscalation only by
		// [ChannelRouter.dispatchResynthesizeMisfire] — false on every other
		// dispatch. Additive: the lift rides ChairEscalation, so this touches
		// only the framing.
		ChairEscalationResynthesize: env.ChairEscalationResynthesize,
		// End-vote-close-propagation amendment (CP2): the close-notification
		// marker, set only by [ChannelRouter.notifyInteractionClose]'s
		// dispatch — false (the proto3 default) on every ordinary fanout.
		InteractionCloseNotification: env.InteractionCloseNotification,
		// RFC 0052 §B: the convene forced-turn marker, set only by
		// [ChannelRouter.ConveneChannel]'s dispatch — false (the proto3
		// default) on every ordinary fanout.
		Convene: env.Convene,
		// RFC 0052 §D (PR 4b-ii): the close-notification redelivery marker +
		// the truthful bounded-close cause (set only by
		// [ChannelRouter.notifyInteractionClose]'s dispatch; the trigger only
		// for a bounded close) and the synthesis forced-turn marker (set only
		// by [ChannelRouter.maybeArmSynthesisClose]'s dispatch). Zero values
		// on every ordinary fanout — the additive mixed-version contract.
		CloseNotificationRedelivery:   env.InteractionCloseRedelivery,
		CloseNotificationCloseTrigger: env.InteractionCloseTrigger,
		SynthesisTurn:                 env.SynthesisTurn,
	}
}
