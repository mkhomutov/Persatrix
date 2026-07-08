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
// §E aggregate bound (PR 7) bounds the count of openers. PR 3 convenes a
// one-shot brainstorm on an IDLE channel: [ChannelRouter.ConveneChannel]
// refuses a channel that already has an open committed interaction
// ([ErrChannelAlreadyConvening]) rather than silently joining it — a loud
// refusal is the safe interim posture until PR 7's force-fresh + aggregate
// bound land. (This guards the standing/already-live case; the narrow
// idle-race where two convenes both land before the convener's first reply
// commits an interaction is still not fully bounded: PR 7b's convening counter
// caps the COUNT fail-closed — two racing convenes cannot exceed max_convenings
// — but does NOT stop both openers dispatching into one folded interaction; that
// two-openers race is the force-fresh slice's, still deferred.)
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
// directive the orchestrator dispatches to the convener. It MUST be a value no
// roster member can ever hold, because the receiver gate's self-sender defence
// (`agents/response_gate.py`) suppresses a self-delivery before any LLM: were
// the directive's sender equal to the convener's own agent id, the opening turn
// would be silenced — and, unlike every other suppression the convene path
// guards against (observer / drifted / unarmed), this one is INVISIBLE to the
// orchestrator (the dispatch *to* the convener succeeds; only the receiver gate
// refuses), so it would 202-then-do-nothing on an unattended channel — the
// exact silent-runaway class this RFC's safety contract exists to catch.
//
// The earlier value `"orchestrator"` was a latent hazard: it satisfies
// [participantIDPattern], so an operator who named an agent `orchestrator` and
// made it the convener would hit precisely that silent suppression. The `:` is
// reserved by the canonical-address grammar and forbidden in participant ids
// ([ValidateParticipantID]), so this sentinel can NEVER equal a real agent id —
// the collision is impossible by construction (pinned by
// TestConveneDispatchSenderID_IsNotAValidParticipantID). The directive is
// transient control (dispatched, never persisted via `PublishMessage`), so the
// `:` never reaches the store's participant-id validation; the convener's
// authored opening turn carries the convener as its real sender.
const ConveneDispatchSenderID = "orchestrator:convene"

// ErrChannelNotArmed — [ChannelRouter.ConveneChannel] against a channel whose
// resolved `autonomous.enabled` is false. The channel exists but is not in a
// convene-able state; the REST layer maps it to 409 Conflict (a precondition
// on the channel's current state, distinct from the 400s the config-validate
// sentinels carry).
var ErrChannelNotArmed = errors.New("channels: channel is not autonomous-enabled")

// ErrChannelAlreadyConvening — [ChannelRouter.ConveneChannel] against a channel
// that already has an open committed interaction (a live discussion). Convening
// is "open an IDLE channel"; re-convening a live one would silently join the
// running interaction (the pre-PR-7 join semantics), so PR 3 refuses it loudly
// instead. The REST layer maps it to 409 Conflict — a precondition on the
// channel's current state, the sibling of [ErrChannelNotArmed].
var ErrChannelAlreadyConvening = errors.New("channels: channel already has an open interaction")

// ErrAutonomousNoAudience — [ChannelRouter.ConveneChannel] against an armed
// channel with no member, OTHER than the convener, that would answer the
// opening turn (every other member is an observer or only responds when
// mentioned, or the convener is the lone member). The convener's opener is an
// OPEN-FLOOR turn by construction (prompts/runtime/safety/convener-opening.md
// mandates "address the room as a whole, not one named member"), and the
// response gate (agents/response_gate.py) admits only an `always` (participant)
// member to an open-floor, unmentioned message — a `when_mentioned` member
// stays silent (`not_mentioned`) until someone @-mentions it, and with no
// `always` member to do the mentioning the opener simply lands in a silent
// room. So a roster whose only non-convener floor-capable members are
// `when_mentioned` is the same dead-on-arrival convene as an observer-only one:
// a guaranteed-futile convene that burns an uncapped opener for nothing.
// Counting `when_mentioned` members as audience (the earlier `!= RespondNever`
// test) let exactly that case through. Roster drift, like the drifted convener,
// so the REST layer maps it to 409 Conflict.
var ErrAutonomousNoAudience = errors.New("channels: autonomous channel has no open-floor responder besides the convener")

// ErrAutonomousNoTopic — [ChannelRouter.ConveneChannel] against an armed channel
// whose resolved block carries no topic, agenda, OR goal, so
// [composeConveneDirective] assembles an EMPTY directive. The convener would be
// framed to "open the discussion on the topic below" with nothing below — an
// empty `<external_data>` envelope — and would author a degenerate opener on no
// subject, an uncapped LLM turn on an unattended channel. PR 1 config validation
// requires a convener but not a subject, so this is the convene-time guard for
// that gap; it is the content sibling of the no-audience precondition, so the
// REST layer maps it to 409 Conflict. (Enforcing it at config-validation would
// fail faster but would retroactively invalidate channels armed under the looser
// PR 1 contract, so the non-breaking convene-time refusal is the PR 3 posture.)
var ErrAutonomousNoTopic = errors.New("channels: autonomous channel has no topic, agenda, or goal to convene on")

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
// convener — or a roster drained of its audience — must fail the convene loudly
// rather than dispatch into a silent gate suppression or an empty room.
//
// The disposition checks map to HTTP as: missing channel → 404, unarmed → 409,
// already-convening → 409, no audience → 409, no subject → 409, every convener
// problem → 400.
func (r *ChannelRouter) ConveneChannel(ctx context.Context, channelID string) (string, error) {
	// Existence first, so a fat-fingered/deleted channel reports 404 (the
	// `AutonomousFor` read below resolves the disabled DEFAULT for an unknown
	// channel, which would otherwise mis-report a missing channel as "not
	// armed" / 409 and diverge from GET/PATCH …/config, which both 404).
	if _, err := r.store.GetChannel(ctx, channelID); err != nil {
		return "", fmt.Errorf("channels: convene %s: load channel: %w", channelID, err)
	}

	a := r.AutonomousFor(channelID)
	if !a.Enabled {
		return "", fmt.Errorf("channels: convene %s: %w", channelID, ErrChannelNotArmed)
	}

	// One-shot-on-idle: refuse a channel with a live discussion rather than
	// silently joining its open interaction (a pure state precondition, checked
	// before the convener/roster validation so a live channel short-circuits
	// regardless of convener drift). See the file header for the PR 7 deferral.
	if _, _, tracked := r.openInteractionEscalationState(channelID); tracked {
		return "", fmt.Errorf("channels: convene %s: %w", channelID, ErrChannelAlreadyConvening)
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
	// The convener membership + observer rule is shared with the config-apply
	// path ([classifyConvenerMember]) so the two enforcement points cannot
	// drift: a drifted (non-member) or observer convener fails the convene
	// loudly here, mapping to 400 via ErrInvalidAutonomousConvener.
	convenerMember, err := classifyConvenerMember(members, convener)
	if err != nil {
		return "", fmt.Errorf("channels: convene %s: %w", channelID, err)
	}
	// The open-floor audience is a convene-only precondition (config-validate
	// requires a convener but not a live audience). Count the members OTHER than
	// the convener that answer the convener's OPEN-FLOOR opener: only an
	// `always` (participant) member does — a `when_mentioned` member stays
	// silent until @-mentioned (the gate's `not_mentioned` suppress) and the
	// opener names no one, so it can never draw one in. A `when_mentioned`-only
	// roster lands the opener in a silent room, the same dead-on-arrival convene
	// as an observer-only one (the earlier `!= RespondNever` test let it through).
	audience := 0
	for i := range members {
		if members[i].ParticipantID == convener {
			continue
		}
		if members[i].RespondPolicy.Normalize() == RespondAlways {
			audience++
		}
	}
	if audience == 0 {
		return "", fmt.Errorf("channels: convene %s: %w: only the convener %q answers an open-floor opener; every other member is an observer or only responds when mentioned, so the opening turn would land in a silent room",
			channelID, ErrAutonomousNoAudience, convener)
	}

	// The escalation chair authors the §D goal-directed synthesis turn on close.
	// PR 4a's mandatory-chair gate validated that an armed channel DECLARES a
	// chair, but a member can leave (or the chair be cleared) between arming and
	// convening without touching the resolved block — so re-validate it here as
	// defence-in-depth, the chair mirror of the drifted-convener guard above
	// ([classifyEscalationChairMember] shares the close-time dispatchable-chair
	// rule). A drifted/observer chair would run the discussion to its bound and
	// close with NO synthesis artifact ([ChannelRouter.maybeArmSynthesisClose] →
	// `synthesisUnavailable`) — the "close with an artifact missing" §D declares a
	// failure — so refuse the convene loudly rather than dispatch into it.
	if _, err := classifyEscalationChairMember(members, r.escalationChairFor(channelID)); err != nil {
		return "", fmt.Errorf("channels: convene %s: %w", channelID, err)
	}

	// The seed directive: operator topic/agenda/goal assembled from the
	// resolved block. The convener wraps it in the RFC 0009 `<external_data>`
	// envelope before injection (agents/persona_runtime/convener.py) — it is
	// operator config, a distinct trust class, the one genuinely new injection
	// surface this RFC opens. Composed once here so the empty-directive guard
	// below and the dispatched message read the same value.
	directive := composeConveneDirective(a)
	if directive == "" {
		// No topic, agenda, or goal — the convener would open on nothing (an
		// empty `<external_data>` envelope). Refuse rather than burn an
		// uncapped opener on a degenerate prompt. See [ErrAutonomousNoTopic].
		return "", fmt.Errorf("channels: convene %s: %w", channelID, ErrAutonomousNoTopic)
	}
	msg := ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: channelID,
		SenderID:  ConveneDispatchSenderID,
		Content:   directive,
	}

	// The RFC 0052 §E aggregate convening ceiling — the LAST precondition, checked
	// after every validation passes and reserved atomically right before the
	// dispatch, so only a convening whose opener actually DISPATCHES consumes a
	// slot ([reserveConvening]/[releaseConvening], convening_counter.go). The count
	// tracks opener-dispatches, fail-closed — it may refuse early (the idle race
	// can burn a slot per opener while they fold into one interaction) but never
	// exceeds max_convenings; see convening_counter.go's scope-limits note.
	// A standing channel re-opens a fresh, separately-capped interaction each fire,
	// so `autonomous.max_convenings` is what bounds the recurring total the
	// per-interaction cap cannot; PR 7a required it be declared, this enforces it.
	// A non-standing / no-count-bound channel resolves `MaxConvenings == 0` →
	// unbounded (the count is still tracked for the readout).
	if !r.reserveConvening(channelID, a.MaxConvenings) {
		return "", fmt.Errorf("channels: convene %s: %w: reached max_convenings=%d",
			channelID, ErrAutonomousConveningBoundReached, a.MaxConvenings)
	}

	// Mark the convener active so the RFC 0048 console (and the web "Convene"
	// affordance's convening indicator) shows the opener being composed; the
	// failed-dispatch branch clears it, mirroring the chair escalation — no
	// reply can ever clear a mark whose dispatch never landed.
	r.markActivity(channelID, []string{convener})
	if err := r.dispatchTo(ctx, msg, ChannelTypeGroup, "", *convenerMember, len(members), nil, dispatchControl{marker: markerConvene}); err != nil {
		// The opener never landed — return the reserved slot so a flapping
		// convener endpoint cannot silently exhaust the aggregate budget.
		r.releaseConvening(channelID)
		r.clearActivity(channelID, convener)
		return "", fmt.Errorf("channels: convene %s: dispatch to convener %q: %w", channelID, convener, err)
	}
	return convener, nil
}

// maxConveneDirectiveBytes is a hard wire-safety ceiling on the assembled
// directive. The convener-side prompt bound (`_CONVENE_DIRECTIVE_MAX_CHARS` in
// agents/persona_runtime/convener.py) owns the prompt-level trim; this larger
// ceiling exists only so a pathological operator free-text — the `topic`/`goal`
// fields carry NO maxLength (PR 1 added none) — cannot dispatch a multi-MB
// directive over the gRPC fanout. Sized far above any realistic
// topic + 64-item agenda + goal, so a well-formed directive is never clipped
// here; the Python bound does the user-visible truncation.
const maxConveneDirectiveBytes = 64 * 1024

// clampDirectiveBytes hard-trims an assembled operator directive to the
// [maxConveneDirectiveBytes] wire ceiling, rune-safely: ToValidUTF8 drops the
// partial rune a byte-slice cut can leave, so the dispatched proto3 string
// stays valid UTF-8. Shared by the convene opener ([composeConveneDirective])
// and the §D synthesis directive ([composeSynthesisDirective]) so the one
// ceiling and its one rune-safety rationale are not re-spelled per seam
// (PR #718 review). The Python prompt bound owns the user-visible trim; this
// only stops a pathological multi-MB dispatch of the maxLength-less
// `topic`/`goal` free-text.
func clampDirectiveBytes(out string) string {
	if len(out) > maxConveneDirectiveBytes {
		out = strings.ToValidUTF8(out[:maxConveneDirectiveBytes], "")
	}
	return out
}

// composeConveneDirective assembles the operator topic/agenda/goal into the
// directive the convener opens on. Empty sections are omitted (topic and goal
// are optional free-text; an empty agenda is a single-topic discussion). The
// result is wrapped in the RFC 0009 envelope receiver-side, so this is plain
// assembly with no escaping — the trust boundary is the envelope, not this
// string. Returns "" iff every section is empty (the [ErrAutonomousNoTopic]
// case the caller refuses).
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
	return clampDirectiveBytes(strings.TrimSpace(b.String()))
}
