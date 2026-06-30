package channels

// convene.go — RFC 0052 §B self-convening, orchestrator half (v0.3.11 PR 3).
//
// Convening = "author the seed turn under a fresh interaction id." An
// autonomous channel ([RFC 0052 §B](../../docs/rfcs/0052-autonomous-agent-channels.md))
// opens with NO human message: [ChannelRouter.ConveneChannel] dispatches a
// directed CONVENE forced turn to the channel's configured
// `autonomous.convener` — reusing the shipped dispatch seam ([dispatchTo],
// [markerConvene]), exactly as the chair-stall escalation reuses it for the
// chair — and the convener persona authors the opening turn from which the
// existing `InboundEventWake` chain carries the discussion. There is NO new
// transport, NO new wake type, and NO new store table: the convene marker is
// an additive field on `ChannelMessageEvent` (the sibling of
// `chair_escalation`), and the convener's authored opening turn flows back
// through the ordinary publish path.
//
// The opening turn resolves UNCAPPED and under a FRESH interaction id by
// construction, not by special-casing here: the wallet snapshots the
// per-interaction cap at an interaction's first commit
// ([internal/wallet/interaction_budget.go]), so the lease that *produces* the
// opener predates its own snapshot; and [ChannelRouter.resolveInteractionID]
// mints a fresh id for the first message of an idle channel. The always-on
// RFC 0030 Layer-0 depth cap bounds that first call, and a standing channel's
// §E aggregate bound (PR 7) bounds the count of openers. Re-convening a
// channel that already has an OPEN interaction (the standing case) would join
// that interaction rather than start fresh — forcing a fresh interaction per
// convening is PR 7's job; PR 3 convenes a one-shot brainstorm on an idle
// channel.
//
// `channel.go` (at the 500-line review cap) is untouched: the convene publish
// logic lives here, mirroring how `router_autonomous.go` carved off the RFC
// 0052 registry.

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/google/uuid"
)

// ConveneDispatchSenderID is the synthetic sender id stamped on the convene
// directive the orchestrator dispatches to the convener. It is deliberately
// NOT a roster member: the receiver gate's self-sender defence
// (`agents/response_gate.py`) suppresses a self-delivery before any LLM, so
// were the convener its own sender the opening turn would be silenced — the
// same reason the chair escalation withholds a self-authored stimulus. The
// directive is transient control (dispatched, never persisted via
// `PublishMessage`), so this id is never validated as a member and never
// reaches the store; the convener's authored opening turn carries the
// convener as its real sender.
const ConveneDispatchSenderID = "orchestrator"

// ErrChannelNotArmed — [ChannelRouter.ConveneChannel] against a channel whose
// resolved `autonomous.enabled` is false. The channel exists but is not in a
// convene-able state; the REST layer maps it to 409 Conflict (a precondition
// on the channel's current state, distinct from the 400s the config-validate
// sentinels carry).
var ErrChannelNotArmed = errors.New("channels: channel is not autonomous-enabled")

// ConveneChannel opens an autonomous channel by dispatching the convene forced
// turn to its configured convener. It returns the convener's agent id on a
// successful dispatch (the surface the CLI/REST/web ack with).
//
// The convener's membership + disposition are re-validated here as
// defence-in-depth even though the apply path
// ([ChannelRouter.validateAutonomousConvener]) already enforced them at config
// time: an unattended channel is the runaway-class failure the safety contract
// exists to catch, and a member can leave between arming and convening
// (`RemoveMember` does not touch the resolved block), so a drifted/observer
// convener must fail the convene loudly rather than dispatch into a silent
// gate suppression.
func (r *ChannelRouter) ConveneChannel(ctx context.Context, channelID string) (string, error) {
	a := r.AutonomousFor(channelID)
	if !a.Enabled {
		return "", fmt.Errorf("channels: convene %s: %w", channelID, ErrChannelNotArmed)
	}
	convener := a.Convener
	if convener == "" {
		// An armed channel without a convener cannot have passed config
		// validation; treat a drifted/forced state as the convener error.
		return "", fmt.Errorf("channels: convene %s: %w: no convener configured", channelID, ErrInvalidAutonomousConvener)
	}

	members, err := r.store.GetMembers(ctx, channelID)
	if err != nil {
		return "", fmt.Errorf("channels: convene %s: load members: %w", channelID, err)
	}
	var convenerMember *Member
	for i := range members {
		if members[i].ParticipantID == convener {
			convenerMember = &members[i]
			break
		}
	}
	if convenerMember == nil {
		return "", fmt.Errorf("channels: convene %s: %w: %q is not a member; the convener authors the opening turn",
			channelID, ErrInvalidAutonomousConvener, convener)
	}
	if convenerMember.RespondPolicy.Normalize() == RespondNever {
		return "", fmt.Errorf("channels: convene %s: %w: %q is an observer (respond: never) and can never author the opening turn",
			channelID, ErrInvalidAutonomousConvener, convener)
	}

	// The seed directive: operator topic/agenda/goal assembled from the
	// resolved block. The convener wraps it in the RFC 0009 `<external_data>`
	// envelope before injection (agents/persona_runtime/convener.py) — it is
	// operator config, a distinct trust class, the one genuinely new injection
	// surface this RFC opens.
	msg := ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: channelID,
		SenderID:  ConveneDispatchSenderID,
		Content:   composeConveneDirective(a),
	}

	// Mark the convener active so the RFC 0048 console (and the web "Convene"
	// affordance's convening indicator) shows the opener being composed; the
	// failed-dispatch branch clears it, mirroring the chair escalation — no
	// reply can ever clear a mark whose dispatch never landed.
	r.markActivity(channelID, []string{convener})
	if err := r.dispatchTo(ctx, msg, ChannelTypeGroup, "", *convenerMember, len(members), nil, markerConvene); err != nil {
		r.clearActivity(channelID, convener)
		return "", fmt.Errorf("channels: convene %s: dispatch to convener %q: %w", channelID, convener, err)
	}
	return convener, nil
}

// composeConveneDirective assembles the operator topic/agenda/goal into the
// directive the convener opens on. Empty sections are omitted (topic and goal
// are optional free-text; an empty agenda is a single-topic discussion). The
// result is wrapped in the RFC 0009 envelope receiver-side, so this is plain
// assembly with no escaping — the trust boundary is the envelope, not this
// string.
func composeConveneDirective(a AutonomousConfig) string {
	var b strings.Builder
	if topic := strings.TrimSpace(a.Topic); topic != "" {
		fmt.Fprintf(&b, "Topic: %s\n", topic)
	}
	if len(a.Agenda) > 0 {
		b.WriteString("\nAgenda:\n")
		for i, item := range a.Agenda {
			fmt.Fprintf(&b, "%d. %s\n", i+1, item)
		}
	}
	if goal := strings.TrimSpace(a.Goal); goal != "" {
		fmt.Fprintf(&b, "\nGoal: %s\n", goal)
	}
	return strings.TrimSpace(b.String())
}
