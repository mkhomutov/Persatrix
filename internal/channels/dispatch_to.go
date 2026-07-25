// dispatch_to.go — the single-recipient dispatch shared by every delivery
// path: the concurrent fanout, the serialized floor turn, and the
// orchestrator-authored control fans (chair escalation, close notification,
// convene, synthesis). Verbatim move from fanout.go when RFC 0037 PR 2's
// classification stamp pushed that file past the 500-line review cap (the
// ISSUE-0008 extraction pattern).
package channels

import (
	"context"
	"errors"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/registry"
)

// dispatchTo delivers `msg` to a single recipient with the per-recipient
// timeout and emits the `channel.messages.delivered` counter. Shared by the
// concurrent path ([dispatchConcurrent]), the serialized floor turn
// ([runFloorTurn]), the chair-escalation forced turn
// ([ChannelRouter.maybeEscalateStall]), and the close-notification fan
// ([ChannelRouter.notifyInteractionClose]) so all four honour the same
// deadline + metric contract.
//
// `marker` stamps at most one orchestrator-authored control marker on the
// envelope (see [dispatchMarker]) — [markerNone] on every ordinary
// dispatch. The dispatch error is returned so the escalation tail can
// label its `chair_escalation{outcome}` and the close fan its
// `close_notification{outcome}`; the fanout and floor-turn callers are
// fire-and-forget by contract and ignore it (the warn + the delivered
// counter's status=error emitted here are their entire failure surface).
func (r *ChannelRouter) dispatchTo(ctx context.Context, msg ChannelMessage, ct ChannelType, threadParentSenderID string, m Member, channelSize int, floorMentions []string, control dispatchControl) error {
	dispatchCtx, cancel := context.WithTimeout(ctx, channelFanoutPerRecipientTimeout)
	defer cancel()
	marker := control.marker
	// Resolve the channel's reasoning rung ONCE so Mode and Revise come from a
	// single locked snapshot: two separate ReasoningFor reads could be torn by a
	// concurrent SetReasoning (a runtime config apply) landing between them, and a
	// single read also drops the redundant mutex acquisition per recipient.
	reasoning := r.ReasoningFor(msg.ChannelID)
	// Both close-notification extras are gated once, on the whole payload, so
	// the envelope cannot honour one without the other or leak either off the
	// marker (the dispatchControl contract).
	closeTrigger, closeRedelivery := control.closeNotificationWireFields()
	err := r.dispatcher.Dispatch(dispatchCtx, DispatchEnvelope{
		Recipient:            m,
		ThreadParentSenderID: threadParentSenderID,
		// RFC 0037 §B (v0.3.12 PR 2): the channel's §A level off the row,
		// resolved beside ReasoningFor — see [ChannelRouter.classificationFor].
		Classification: r.classificationFor(dispatchCtx, msg.ChannelID),
		// RFC 0030 Tier B (v0.3.8): the per-publish channel-size + resolved cap
		// the agent-side seam reads for the TB6 channel-size gate. The
		// per-recipient bid signals (salience_gated/threshold) ride on
		// `m`/`Recipient`; these two are channel-wide.
		ChannelSize:               channelSize,
		SalienceMaxChannelMembers: r.salienceMaxFor(msg.ChannelID),
		// RFC 0051 PR 6 go-live: the channel's resolved reasoning rung, read off the
		// (flip-aware) router so a governed channel ships `bid` by default and an
		// explicit `off` stays the kill switch. Channel-wide, like ChannelSize.
		ReasoningMode: reasoning.Mode,
		// RFC 0051 PR 8 (Phase 5a): the channel's resolved reflexion round count,
		// off the same single resolve. Channel-wide; 0 = single-pass.
		ReasoningRevise: reasoning.Revise,
		// Floor-capable-directedness amendment (v0.3.8): per-publish, like
		// ChannelSize — resolved once in [ChannelRouter.fanout].
		FloorMentions:                floorMentions,
		ChairEscalation:              marker == markerChairEscalation || marker == markerChairEscalationResynthesize,
		ChairEscalationResynthesize:  marker == markerChairEscalationResynthesize,
		InteractionCloseNotification: marker == markerCloseNotification,
		Convene:                      marker == markerConvene,
		SynthesisTurn:                marker == markerSynthesisTurn,
		// The close-notification extras ride ONLY under their marker, gated as
		// one payload by closeNotificationWireFields (the dispatchControl
		// contract): a stray value on any other dispatch is structurally
		// unrepresentable on the wire.
		InteractionCloseTrigger:    closeTrigger,
		InteractionCloseRedelivery: closeRedelivery,
	}, msg)
	status := "ok"
	switch {
	case err == nil:
	case errors.Is(err, registry.ErrAgentNotFound):
		// A never-registered member (a human peer on a chat-surface DM, a
		// mistyped channels.yaml membership) is the documented best-effort
		// miss, not a delivery failure: the error return must stand — the
		// undelivered ledger keys the close-notification redelivery marker
		// on it — but a standing member misses on EVERY message, so
		// counting it as status="error" (plus a second warn on top of the
		// dispatcher's) turns a healthy channel into a permanent error
		// signal. Distinct status, and the dispatcher's single warn (with
		// the read-via-history context) is the whole log surface.
		status = "unregistered"
	default:
		status = "error"
		r.logger.Warn("channels: dispatch failed",
			zap.String("channel_id", msg.ChannelID),
			zap.String("recipient", m.ParticipantID),
			zap.Error(err))
	}
	if r.metrics != nil && r.metrics.MessagesDelivered != nil {
		r.metrics.MessagesDelivered.Add(ctx, 1, metric.WithAttributes(
			attribute.String("channel_type", string(ct)),
			attribute.String("status", status),
		))
	}
	return err
}
