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
//
// SECURITY (exemption trust boundary): the exemption decision in
// [ChannelRouter.enforceReplyBudget] reads the sender's `participant_type` from
// the publish metadata bag. Both REST producers now stamp it server-side — the
// chat handler for the human path, and the raw publish path
// (`POST /channels/{id}/messages`) from the agent registry (ISSUE-0119) — and
// on the publish path a REGISTERED sender's resolution OVERRIDES any claim, so
// an agent of this deployment can no longer self-assert `participant_type:
// "user"` to buy the human exemption (pinned by
// `TestChannelPublish_RegisteredAgentCannotClaimUser`).
//
// The residual gap is the sender the registry cannot see: an UNREGISTERED id
// keeps its caller-asserted claim (the bridge/external-agent case), so a
// caller publishing under an unregistered sender_id can still assert `user`
// and be treated as exempt (the contract pinned by
// `TestReplyBudget_HumanPrincipalExempt`). This is harmless while the layer is
// inert (no producer writes `interaction_id`, so `enforceReplyBudget` never
// reaches the exemption check on real traffic — see interaction_id.go), but
// BEFORE the layer becomes load-bearing (the producer lands in PR 4+) that
// remaining case must derive the principal from the authenticated identity
// (RFC 0039 §F) rather than the bag. Tracked as a hardening follow-up in the
// RFC 0030 PR plan.
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
// meaningful value, not a "use the default" sentinel. Driven at startup by
// [ChannelRouter.ResolveReplyBudgets] (with the per-channel resolved value) and
// at runtime by [ChannelRouter.ApplyDefaultReplyBudget] when a group channel is
// created through `POST /api/v1/channels`. Because zero is meaningful, the
// runtime path CANNOT inherit the fleet default via Set(_, 0) the way the
// salience cap does — it must be handed the resolved default, which is why
// ApplyDefaultReplyBudget exists rather than a bare SetReplyBudget(_, 0) call.
// The mutex makes the runtime call safe concurrently with traffic.
func (r *ChannelRouter) SetReplyBudget(channelID string, k int) {
	if k < 0 {
		k = 0
	}
	r.replyBudgetMu.Lock()
	defer r.replyBudgetMu.Unlock()
	r.replyBudgets[channelID] = k
}

// ApplyDefaultReplyBudget stamps the fleet-wide
// `default_max_replies_per_participant` (captured at
// [ChannelRouter.ResolveReplyBudgets]) onto a freshly-created group channel —
// the reply-budget sibling of the `SetSalienceMaxChannelMembers(_, 0)` /
// `SetFloorControl` calls in the channel-create handler. It is a distinct
// method because reply-budget zero is uncapped-as-a-value rather than a
// "use the default" sentinel: a runtime channel cannot inherit the default by
// passing 0, so the handler delegates the lookup here instead of duplicating
// the fleet-default field. No-op-safe before ResolveReplyBudgets has run (the
// captured default is then 0 = uncapped, the opt-in default).
func (r *ChannelRouter) ApplyDefaultReplyBudget(channelID string) {
	r.replyBudgetMu.Lock()
	d := r.defaultReplyBudget
	r.replyBudgetMu.Unlock()
	r.SetReplyBudget(channelID, d)
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
// store.
//
// On admission it returns a `release` closure and a nil error. `release` is
// non-nil ONLY when a reply slot was actually reserved (a capped, tracked,
// non-exempt publish); the caller MUST invoke it if the subsequent
// [ChannelStore.PublishMessage] fails, so a store-rejected publish (oversized
// content, non-member, …) does not consume the sender's allowance. The §F
// counter must track messages that entered channel history, not publish
// attempts — incrementing pre-persistence with no rollback would lock out a
// well-behaved participant after K rejected attempts that never persisted.
// `release` is nil (a no-op for the caller) when the publish is admitted
// without reserving a slot:
//
//   - the channel is uncapped (K<=0) — the opt-in default;
//   - the publish has no `interaction_id` — nothing to scope a counter to, so
//     the layer stays at its uncapped default (the untracked / pre-v0.3.8 case);
//   - the sender is an exempt principal (a human, per governance.exempt_principals).
//
// The check-and-reserve is atomic under replyBudgetMu so two concurrent
// publishes from the same participant cannot both slip past the boundary; the
// reservation keeps that atomicity (the slot is held the instant the gate
// admits) while still reconciling to actual persistence on the error path.
func (r *ChannelRouter) enforceReplyBudget(ctx context.Context, msg ChannelMessage, ct ChannelType) (func(), error) {
	interactionID := readInteractionID(msg.Metadata)
	if interactionID == "" {
		return nil, nil // untracked — no interaction to scope the budget to.
	}
	// RFC 0030 Layer 4 end-of-interaction votes are exempt from the Layer 2
	// reply budget. A vote is a terminal meta-signal (deduped to one per
	// participant in processEndVote), not a content reply, and gating it behind
	// the budget would let Layer 2 STARVE Layer 4: a participant who has spent
	// their reply allowance could never cast the vote that converges the
	// interaction, so a budget-saturated brainstorm could never reach the quorum
	// and never close on its own. The two layers compose on the close path
	// (processEndVote → DiscardInteractionReplyBudget); they must not collide on
	// the admission path. The vote is still persisted into history — it is only
	// exempt from consuming/being-rejected-by a reply slot.
	//
	// This exemption is bounded so it cannot become a reply-budget BYPASS: a
	// participant who flags every publish as a vote to dodge the cap is caught
	// downstream by processEndVote, which suppresses the fanout of a redundant
	// in-window duplicate vote (it is deduped to a no-op for the quorum). So the
	// exemption buys exactly one fanned-out terminal signal past the cap (the
	// participant's first vote), not unbounded amplification.
	if readEndInteractionVote(msg.Metadata) {
		return nil, nil
	}
	participantType := readParticipantType(msg.Metadata)

	r.replyBudgetMu.Lock()
	k := r.replyBudgets[msg.ChannelID]
	// `participantType` is caller-asserted off the publish bag; the exemption it
	// drives is only as trustworthy as its source — see the SECURITY note on
	// [exemptPrincipalParticipantType] before this gate becomes load-bearing.
	if k <= 0 || r.isExemptParticipantType(participantType) {
		r.replyBudgetMu.Unlock()
		return nil, nil
	}
	counts := r.replyCounts[interactionID]
	if counts != nil && counts[msg.SenderID] >= k {
		r.replyBudgetMu.Unlock()
		r.recordReplyBudgetDrop(ctx, msg, ct, interactionID, k)
		return nil, fmt.Errorf("%w: participant %q reached %d replies in interaction %q",
			ErrParticipantBudgetExhausted, msg.SenderID, k, interactionID)
	}
	if counts == nil {
		counts = make(map[string]int, 1)
		r.replyCounts[interactionID] = counts
	}
	counts[msg.SenderID]++
	r.replyBudgetMu.Unlock()

	// Slot reserved but not yet durable. The caller releases it iff the
	// persist fails, so the counter only ever reflects messages in history.
	sender := msg.SenderID
	return func() { r.releaseReplyReservation(interactionID, sender) }, nil
}

// releaseReplyReservation rolls back the reply-slot reservation taken by
// [ChannelRouter.enforceReplyBudget] when the subsequent persist failed,
// keeping the §F counter equal to what entered channel history. Atomic under
// replyBudgetMu and idempotent: a missing interaction (already discarded on
// close) or a sender already at zero is a no-op. Prunes the entry on the way
// down so a released reservation leaves no residue.
func (r *ChannelRouter) releaseReplyReservation(interactionID, sender string) {
	r.replyBudgetMu.Lock()
	defer r.replyBudgetMu.Unlock()
	counts := r.replyCounts[interactionID]
	if counts == nil || counts[sender] == 0 {
		return
	}
	counts[sender]--
	if counts[sender] == 0 {
		delete(counts, sender)
	}
	if len(counts) == 0 {
		delete(r.replyCounts, interactionID)
	}
}

// publishWithReplyBudget wraps the store commit in the Layer 2 reservation: it
// runs the [ChannelRouter.enforceReplyBudget] gate, persists via
// [ChannelStore.PublishMessage], and releases the reservation if the persist
// fails. Keeping the reserve/persist/release triple together here (rather than
// inlined in [ChannelRouter.Publish]) keeps router.go under the file-size cap
// and makes the "counter tracks history, not attempts" invariant local: a
// throttled (K+1)th publish never reaches the store, and a store-rejected
// publish (oversized content, non-member, …) never consumes a slot.
func (r *ChannelRouter) publishWithReplyBudget(ctx context.Context, msg ChannelMessage, ct ChannelType) error {
	release, err := r.enforceReplyBudget(ctx, msg, ct)
	if err != nil {
		return err
	}
	if err := r.store.PublishMessage(ctx, msg); err != nil {
		if release != nil {
			release()
		}
		return err
	}
	return nil
}

// DiscardInteractionReplyBudget drops the per-participant reply counters for a
// closed interaction (the §F reset semantics: "Counters live on the Interaction
// and are discarded on close"). The seam the RFC 0020 structural close / Layer 4
// end-vote path (PR 4) calls when an interaction ends; idempotent — discarding
// an unknown interaction is a no-op. Bounds `replyCounts` so a long-lived
// orchestrator does not accumulate a counter map per interaction forever.
//
// ORDERING (discharged): this is the ONLY seam that prunes a live
// interaction's entry (a failed-persist release prunes only its own
// reservation), so the producer was forbidden from landing before the close
// paths wired it. Both now do: the Layer 4 quorum close fires it inline
// (processEndVote), and the resolver's lazy idle rotation fires it
// one-generation-deferred for every retired id (interaction_resolver.go,
// producer plan IP4/IP7). The attacker-influenceable-token half of the
// original hazard is gone outright: the resolver overrides inbound claims
// (IP2), so only router-minted uuids ever key this map.
//
// Also called on the post-close suppression path in [ChannelRouter.processEndVote]:
// a post-close non-vote reply lazily re-creates this interaction's counter via
// enforceReplyBudget (which has no view of closedInteractions), and the close-time
// discard never runs again — so the post-close path re-discards it to keep that
// re-created entry from leaking. Idempotent makes that re-call safe.
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
		zap.String("layer", governanceLayerReplyBudget),
	)
	if r.metrics != nil && r.metrics.GovernanceDrop != nil {
		r.metrics.GovernanceDrop.Add(ctx, 1, metric.WithAttributes(
			attribute.String("channel_type", string(ct)),
			attribute.String("layer", governanceLayerReplyBudget),
		))
	}
	annotateGovernanceDropSpan(ctx, governanceLayerReplyBudget)
}

// recordReplyBudgetRemainingAtClose records, for each participant tracked in a
// closing interaction's reply counters, the leftover Layer 2 allowance
// (K - replies_used) on the `reply_budget_remaining` histogram (§L). It is the
// Layer 4 → Layer 2 composition seam: the end-vote close (end_vote.go) calls it
// just BEFORE [ChannelRouter.DiscardInteractionReplyBudget] prunes the counters,
// so the histogram observes the final per-participant state of the interaction.
//
// No-op (and zero hot-path cost) on the inert/default path: when no metric handle
// is wired, the channel is uncapped (K<=0), or the interaction was untracked
// (no reply counters). Only participants who actually replied are tracked, so the
// sample is per-replying-participant — a member who never spoke has the full
// allowance and is not counted. The counts are snapshotted under replyBudgetMu so
// the Record calls happen outside the lock.
func (r *ChannelRouter) recordReplyBudgetRemainingAtClose(ctx context.Context, channelID, interactionID string, ct ChannelType) {
	if r.metrics == nil || r.metrics.ReplyBudgetRemaining == nil {
		return
	}
	r.replyBudgetMu.Lock()
	k := r.replyBudgets[channelID]
	var remaining []float64
	if k > 0 {
		if counts := r.replyCounts[interactionID]; counts != nil {
			remaining = make([]float64, 0, len(counts))
			for _, used := range counts {
				rem := k - used
				if rem < 0 {
					rem = 0 // a clamp: counts never exceed K, but never emit a negative headroom.
				}
				remaining = append(remaining, float64(rem))
			}
		}
	}
	r.replyBudgetMu.Unlock()

	for _, rem := range remaining {
		r.metrics.ReplyBudgetRemaining.Record(ctx, rem,
			metric.WithAttributes(attribute.String("channel_type", string(ct))))
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
	// Capture the fleet default so a runtime-created channel can inherit it via
	// [ChannelRouter.ApplyDefaultReplyBudget] (zero is meaningful, so it cannot
	// ride a Set(_, 0) sentinel the way the salience cap does).
	r.replyBudgetMu.Lock()
	r.defaultReplyBudget = fleetDefault
	r.replyBudgetMu.Unlock()
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
