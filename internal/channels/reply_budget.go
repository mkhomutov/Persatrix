package channels

import (
	"context"
	"fmt"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

// reply_budget.go holds the RFC 0030 Layer 2 (v0.3.8) per-participant reply
// budget: the deterministic fair-turn-taking layer that bounds how many times
// one participant may publish in a single interaction (§F). Split out of
// router.go so that file stays under the 500-line review cap, mirroring how
// router_salience.go carved off the Tier B channel-size cap. The `replyBudgetMu`
// mutex + its maps are declared on [ChannelRouter] in router.go (a struct field
// must live with the type); only the methods move here.
//
// The layer is opt-in/uncapped by default: a channel with no resolved budget
// (K=0), or a publish with no `interaction_id` to scope a counter to, is never
// gated — so the layer is purely additive over v0.3.7 behaviour.

// exemptPrincipalParticipantType maps a `governance.exempt_principals` label to
// the wire participant type it exempts. Only `human` is recognised, mapping to
// the `user` participant type the REST chat handler stamps for a human peer
// (chat_handler.go). An unrecognised label has no mapping, so it silently fails
// open to "not exempt" — a typo cannot accidentally exempt agents.
func exemptPrincipalParticipantType(principal string) (string, bool) {
	if principal == "human" {
		return "user", true
	}
	return "", false
}

// SetReplyBudget resolves the RFC 0030 Layer 2 per-participant reply budget for
// `channelID`: a participant's (K+1)th publish in one interaction on this
// channel is rejected pre-persistence. `k <= 0` means uncapped (the opt-in
// default) and is stored verbatim — unlike the salience cap, zero is a
// meaningful value, not a "use the default" sentinel. Wired two ways like
// [ChannelRouter.SetSalienceMaxChannelMembers]: at startup via
// [ChannelRouter.ResolveReplyBudgets] and at runtime when a group channel is
// created through `POST /api/v1/channels`. The mutex makes the runtime call
// safe concurrently with traffic.
func (r *ChannelRouter) SetReplyBudget(channelID string, k int) {
	if k < 0 {
		k = 0
	}
	r.replyBudgetMu.Lock()
	defer r.replyBudgetMu.Unlock()
	r.replyBudgets[channelID] = k
}

// ReplyBudgetFor returns the resolved Layer 2 reply budget for `channelID`
// (0 = uncapped). Exposed for tests and ops introspection, mirroring
// [ChannelRouter.FloorControlFor]; the hot path reads the same map under the
// lock in [ChannelRouter.enforceReplyBudget].
func (r *ChannelRouter) ReplyBudgetFor(channelID string) int {
	r.replyBudgetMu.Lock()
	defer r.replyBudgetMu.Unlock()
	return r.replyBudgets[channelID]
}

// SetExemptPrincipals resolves `governance.exempt_principals` to the set of
// participant types exempt from the Layer 2 reply budget (GL4 / §OQ-7). Fleet-
// wide, not per-channel. An unrecognised label is dropped (fails open to "not
// exempt"). Replaces any prior set, so a reload reflects the new config.
func (r *ChannelRouter) SetExemptPrincipals(principals []string) {
	set := make(map[string]struct{}, len(principals))
	for _, p := range principals {
		if pt, ok := exemptPrincipalParticipantType(p); ok {
			set[pt] = struct{}{}
		}
	}
	r.replyBudgetMu.Lock()
	defer r.replyBudgetMu.Unlock()
	r.exemptParticipantTypes = set
}

// isExemptParticipantType reports whether a sender with the given wire
// participant type is exempt from the reply budget. Caller holds replyBudgetMu.
func (r *ChannelRouter) isExemptParticipantType(participantType string) bool {
	if participantType == "" {
		return false
	}
	_, ok := r.exemptParticipantTypes[participantType]
	return ok
}

// enforceReplyBudget is the publish-time RFC 0030 Layer 2 gate. It runs
// pre-persistence in [ChannelRouter.Publish]: when the channel has a resolved
// budget K>0 and the publish carries an `interaction_id`, it counts the
// sender's publishes in that interaction and rejects the (K+1)th with
// [ErrParticipantBudgetExhausted] — so the dropped message never reaches the
// store. Returns nil (admits the publish) when:
//
//   - the channel is uncapped (K<=0) — the opt-in default;
//   - the publish has no `interaction_id` — nothing to scope a counter to, so
//     the layer stays at its uncapped default (the untracked / pre-v0.3.8 case);
//   - the sender is an exempt principal (a human, per governance.exempt_principals).
//
// The check-and-increment is atomic under replyBudgetMu so two concurrent
// publishes from the same participant cannot both slip past the boundary.
func (r *ChannelRouter) enforceReplyBudget(ctx context.Context, msg ChannelMessage, ct ChannelType) error {
	interactionID := readInteractionID(msg.Metadata)
	if interactionID == "" {
		return nil // untracked — no interaction to scope the budget to.
	}
	participantType := readParticipantType(msg.Metadata)

	r.replyBudgetMu.Lock()
	k := r.replyBudgets[msg.ChannelID]
	if k <= 0 || r.isExemptParticipantType(participantType) {
		r.replyBudgetMu.Unlock()
		return nil
	}
	counts := r.replyCounts[interactionID]
	if counts != nil && counts[msg.SenderID] >= k {
		r.replyBudgetMu.Unlock()
		r.recordReplyBudgetDrop(ctx, msg, ct, interactionID, k)
		return fmt.Errorf("%w: participant %q reached %d replies in interaction %q",
			ErrParticipantBudgetExhausted, msg.SenderID, k, interactionID)
	}
	if counts == nil {
		counts = make(map[string]int, 1)
		r.replyCounts[interactionID] = counts
	}
	counts[msg.SenderID]++
	r.replyBudgetMu.Unlock()
	return nil
}

// DiscardInteractionReplyBudget drops the per-participant reply counters for a
// closed interaction (the §F reset semantics: "Counters live on the Interaction
// and are discarded on close"). The seam the RFC 0020 structural close / Layer 4
// end-vote path (PR 4) calls when an interaction ends; idempotent — discarding
// an unknown interaction is a no-op. Bounds `replyCounts` so a long-lived
// orchestrator does not accumulate a counter map per interaction forever.
func (r *ChannelRouter) DiscardInteractionReplyBudget(interactionID string) {
	if interactionID == "" {
		return
	}
	r.replyBudgetMu.Lock()
	delete(r.replyCounts, interactionID)
	r.replyBudgetMu.Unlock()
}

// recordReplyBudgetDrop fires the GL3 structured drop log + the
// `governance_drop{layer=reply_budget}` counter when the Layer 2 gate rejects a
// publish. The structured log carries the full attribution (channel,
// interaction, participant, the resolved cap) so an operator can see who was
// throttled and why; the counter (nil-safe) feeds the governance-drop
// dashboard. Mirrors [ChannelRouter.recordCascadeCap].
func (r *ChannelRouter) recordReplyBudgetDrop(ctx context.Context, msg ChannelMessage, ct ChannelType, interactionID string, k int) {
	r.logger.Warn("channels: participant reply budget exhausted",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", interactionID),
		zap.String("participant_id", msg.SenderID),
		zap.Int("max_replies_per_participant", k),
		zap.String("layer", "reply_budget"),
	)
	if r.metrics != nil && r.metrics.GovernanceDrop != nil {
		r.metrics.GovernanceDrop.Add(ctx, 1, metric.WithAttributes(
			attribute.String("channel_type", string(ct)),
			attribute.String("layer", "reply_budget"),
		))
	}
}

// ResolveReplyBudgets applies the RFC 0030 Layer 2 (v0.3.8) per-participant
// reply budget to every group channel known at startup, the per-channel sibling
// of [ChannelRouter.ResolveSalienceCaps], and resolves the fleet-wide
// `governance.exempt_principals` set once. Each config-declared channel uses its
// resolved `max_replies_per_participant_per_interaction` (channel-over-fleet
// precedence via [ChannelConfig.ResolveMaxRepliesPerParticipant]); every other
// group channel present in the store — e.g. a runtime-created channel that
// survived a restart — inherits the fleet default. A channel whose membership is
// all-`participant` (all-`always`) with an uncapped budget gets an advisory
// startup Warn: the pile-on guard is off, same shape as the unauthenticated-REST
// warning in cmd/orchestrator/channels.go. Advisory only — not a behaviour
// change.
//
// DM and thread channels are skipped: the reply budget governs open-floor group
// traffic. Call once after [ChannelRouter.ReconcileConfig]; idempotent.
func (r *ChannelRouter) ResolveReplyBudgets(ctx context.Context, cfg *Config) error {
	fleetDefault := 0
	if cfg != nil {
		fleetDefault = cfg.DefaultMaxRepliesPerParticipant
		r.SetExemptPrincipals(cfg.Governance.ExemptPrincipals)
	}
	configured := make(map[string]bool)
	if cfg != nil {
		for _, decl := range cfg.Channels {
			id := decl.CanonicalID()
			configured[id] = true
			resolved := decl.ResolveMaxRepliesPerParticipant(fleetDefault)
			r.SetReplyBudget(id, resolved)
			if resolved <= 0 && allParticipantMembership(decl.Members) {
				r.logger.Warn("channels: all-participant channel has an uncapped reply budget — Layer 2 pile-on guard is off; set max_replies_per_participant_per_interaction to bound per-participant turns",
					zap.String("channel_id", id),
					zap.String("rfc", "0030"),
					zap.String("layer", "reply_budget"),
				)
			}
		}
	}
	all, err := r.store.ListChannels(ctx, 0, "")
	if err != nil {
		return fmt.Errorf("channels: resolve reply budgets: list channels: %w", err)
	}
	for _, ch := range all {
		if ch.Type != ChannelTypeGroup || configured[ch.ID] {
			continue
		}
		r.SetReplyBudget(ch.ID, fleetDefault)
	}
	return nil
}

// allParticipantMembership reports whether every member is an open-floor
// participant (RespondAlways — the normalized form of `participant`/`chair`).
// An empty membership is not "all participant" (there is no one to pile on).
// Used only for the advisory startup Warn — the disposition vocabulary is
// already normalized to the legacy triple at config load.
func allParticipantMembership(members []MemberConfig) bool {
	if len(members) == 0 {
		return false
	}
	for _, m := range members {
		if m.RespondPolicy != RespondAlways {
			return false
		}
	}
	return true
}
