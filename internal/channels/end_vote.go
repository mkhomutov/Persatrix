package channels

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

// end_vote.go holds the RFC 0030 Layer 4 (v0.3.8) end-of-interaction signal:
// the deterministic, auditable layer that lets a conversation terminate on its
// own (§H). An agent emits an `END_INTERACTION_VOTE` action when it judges its
// contribution complete; the orchestrator accumulates votes per interaction,
// and when K distinct participants have voted within W consecutive turns it
// closes the interaction — suppressing fanout so no new replies are drawn. The
// vote is the explicit-action option (§OQ-4, resolved for auditability), not a
// metadata bag of implicit no-action signals. Split out of router.go so that
// file stays under the 500-line review cap, mirroring reply_budget.go; the
// `endVoteMu` mutex + its maps are declared on [ChannelRouter] in router.go.
//
// PRODUCER CAVEAT (mirrors reply_budget.go): no orchestrator- or agent-side
// producer writes `interaction_id` onto publish metadata yet, and the Python
// `END_INTERACTION_VOTE` action is recognised but not yet wired to publish the
// vote flag — so `readEndInteractionVote`/`readInteractionID` return their
// empty values on real traffic and this layer is *inert in production*. It is
// wired and tested ahead of the producer, not yet load-bearing. The producer
// MUST land together with the `interaction_id` producer so a closing interaction
// also drives the RFC 0020 structural close (see ORDERING on
// [ChannelRouter.DiscardInteractionEndVotes]).

// endVoteMetadataKey is the wire-level publish-metadata flag a producer sets to
// mark a publish as an RFC 0030 Layer 4 end-of-interaction vote. Centralised so
// a future rename is one edit, mirroring `interactionIDMetadataKey`.
const endVoteMetadataKey = "end_interaction_vote"

// endVotesTrigger is the `trigger` attribute value on the
// `channel.conversation.interaction_closed` metric (and the structured log)
// when an interaction closes because the end-vote quorum was reached. PR 5
// adds the other triggers (`idle`/`structural`/`cost`).
const endVotesTrigger = "end_votes"

// interactionEndVotes is the per-interaction Layer 4 accumulator. `turn` counts
// every tracked publish in the interaction since the FIRST vote was cast (it is
// created lazily on that first vote — an interaction that never votes never
// allocates one, bounding growth). `votes` maps a participant to the turn its
// most recent vote landed on; re-voting overwrites the stamp, so a participant
// counts once (the §H per-(participant, interaction) dedupe). The window check
// is relative — only the *difference* between a vote's turn and the current
// turn matters — so counting from the first vote rather than the interaction's
// true start is exact for "votes within W consecutive turns".
type interactionEndVotes struct {
	turn  int
	votes map[string]int
}

// readEndInteractionVote reports whether a publish metadata bag flags the
// message as an end-of-interaction vote. Tolerant like the other wire readers
// (readInteractionID / readCascadeDepth): absent or non-bool is "not a vote".
func readEndInteractionVote(metadata map[string]any) bool {
	if metadata == nil {
		return false
	}
	v, ok := metadata[endVoteMetadataKey].(bool)
	return ok && v
}

// SetEndVoteParams resolves the RFC 0030 Layer 4 quorum (K) and recency window
// (W) for `channelID`. Non-positive values fall back to the
// [DefaultEndVoteThreshold]/[DefaultEndVoteWindow] defaults so the layer always
// reads a populated K/W (a zero threshold or window is meaningless, unlike a
// zero reply budget). Driven at startup by [ChannelRouter.ResolveEndVotes]; the
// mutex makes a runtime call safe concurrently with traffic.
func (r *ChannelRouter) SetEndVoteParams(channelID string, k, w int) {
	if k <= 0 {
		k = DefaultEndVoteThreshold
	}
	if w <= 0 {
		w = DefaultEndVoteWindow
	}
	r.endVoteMu.Lock()
	defer r.endVoteMu.Unlock()
	r.endVoteThresholds[channelID] = k
	r.endVoteWindows[channelID] = w
}

// EndVoteParamsFor returns the resolved Layer 4 quorum (K) and recency window
// (W) for `channelID`, falling back to the defaults for an unresolved channel.
// Exposed for tests and ops introspection; the hot path reads the same maps
// under the lock in [ChannelRouter.processEndVote].
func (r *ChannelRouter) EndVoteParamsFor(channelID string) (k, w int) {
	r.endVoteMu.Lock()
	defer r.endVoteMu.Unlock()
	return r.resolvedEndVoteParamsLocked(channelID)
}

// resolvedEndVoteParamsLocked returns the K/W for a channel, defaulting an
// absent entry. Caller holds endVoteMu.
func (r *ChannelRouter) resolvedEndVoteParamsLocked(channelID string) (k, w int) {
	k = r.endVoteThresholds[channelID]
	if k <= 0 {
		k = DefaultEndVoteThreshold
	}
	w = r.endVoteWindows[channelID]
	if w <= 0 {
		w = DefaultEndVoteWindow
	}
	return k, w
}

// processEndVote is the publish-time RFC 0030 Layer 4 hook. It advances the
// interaction's turn counter, records an end-vote (if this publish carries one),
// and closes the interaction when K distinct participants have voted within W
// consecutive turns. It returns true to tell [ChannelRouter.Publish] to
// SUPPRESS fanout — for the closing publish and for any later publish to an
// already-closed interaction — so a converged conversation stops drawing new
// replies.
//
// It is a no-op (returns false) for untracked traffic (no `interaction_id`) and
// for any tracked publish to an interaction that has not yet seen a vote — so
// the accumulator is only allocated once an interaction actually starts voting,
// and v0.3.7 behaviour is preserved when no producer emits votes.
func (r *ChannelRouter) processEndVote(ctx context.Context, msg ChannelMessage, ct ChannelType) bool {
	interactionID := readInteractionID(msg.Metadata)
	if interactionID == "" {
		return false // untracked — nothing to scope a quorum to.
	}
	isVote := readEndInteractionVote(msg.Metadata)

	r.endVoteMu.Lock()
	if _, done := r.closedInteractions[interactionID]; done {
		r.endVoteMu.Unlock()
		return true // already closed — keep suppressing fanout for late traffic.
	}
	state := r.endVotes[interactionID]
	if state == nil {
		if !isVote {
			r.endVoteMu.Unlock()
			return false // no votes yet and this is not one — nothing to track.
		}
		state = &interactionEndVotes{votes: make(map[string]int, 1)}
		r.endVotes[interactionID] = state
	}
	state.turn++
	k, w := r.resolvedEndVoteParamsLocked(msg.ChannelID)
	spam := false
	if isVote {
		// Spam only if the participant already has a LIVE (in-window) vote. A
		// re-vote cast after the prior one fell out of the recency window
		// (state.turn-prev >= w) is legitimate re-engagement, not vote tampering
		// (§H), so it must not be logged as spam — otherwise a participant who
		// votes early, waits out the window, and votes again pollutes the very
		// audit signal this layer exists to provide. Re-voting overwrites the
		// stamp either way, so the per-(participant, interaction) dedupe holds.
		prev, existed := state.votes[msg.SenderID]
		spam = existed && state.turn-prev < w
		state.votes[msg.SenderID] = state.turn
	}
	// Count distinct participants with a live (in-window) vote. There is
	// deliberately NO principal exemption here, unlike the Layer 2 reply budget
	// (which exempts `governance.exempt_principals` → `user`): the budget is a
	// throttle a human steering the conversation should bypass, whereas an
	// end-vote is an explicit terminal signal — if a human bothers to vote that
	// the interaction is done, that intent counts toward the quorum. The vote is
	// an agent action (END_INTERACTION_VOTE) in practice, so a human voter is the
	// rare deliberate case, not an accident of ordinary traffic.
	recent := 0
	for _, voteTurn := range state.votes {
		if state.turn-voteTurn < w {
			recent++
		}
	}
	closed := isVote && recent >= k
	if closed {
		r.closedInteractions[interactionID] = struct{}{}
		delete(r.endVotes, interactionID)
	}
	r.endVoteMu.Unlock()

	if spam {
		r.recordEndVoteSpam(msg, interactionID)
	}
	if closed {
		r.recordInteractionClosed(ctx, msg, ct, interactionID, recent)
		// Wire the §F reset seam: the interaction is closing, so its per-
		// participant reply counters are discarded (the seam reply_budget.go
		// reserved for the Layer 4 close path).
		r.DiscardInteractionReplyBudget(interactionID)
		return true
	}
	return false
}

// recordInteractionClosed fires the structured close log + the
// `interaction_closed{trigger=end_votes}` counter when the Layer 4 quorum is
// reached. The log carries the full attribution (channel, interaction, the
// triggering voter, the vote count) so an operator can audit who closed the
// conversation; the counter (nil-safe) feeds the convergence dashboard (§L).
func (r *ChannelRouter) recordInteractionClosed(ctx context.Context, msg ChannelMessage, ct ChannelType, interactionID string, votes int) {
	r.logger.Info("channels: interaction closed by end-of-interaction votes",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", interactionID),
		zap.String("participant_id", msg.SenderID),
		zap.Int("votes", votes),
		zap.String("trigger", endVotesTrigger),
	)
	if r.metrics != nil && r.metrics.InteractionClosed != nil {
		r.metrics.InteractionClosed.Add(ctx, 1, metric.WithAttributes(
			attribute.String("channel_type", string(ct)),
			attribute.String("trigger", endVotesTrigger),
		))
	}
}

// recordEndVoteSpam logs a duplicate end-vote so an adversarial vote-spam
// pattern is visible in audit (§H vote tampering). The duplicate is already
// deduped in the accumulator (it counts once); this only surfaces the rate. Only
// a LIVE (in-window) re-vote is logged — a re-vote after the prior one went
// stale is legitimate re-engagement (see the spam check in processEndVote), so a
// participant voting every turn is what collapses to one logged spam per re-vote.
func (r *ChannelRouter) recordEndVoteSpam(msg ChannelMessage, interactionID string) {
	r.logger.Warn("channels: duplicate end-of-interaction vote (deduped; possible vote-spam)",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", interactionID),
		zap.String("participant_id", msg.SenderID),
		zap.String("layer", "end_vote"),
	)
}

// DiscardInteractionEndVotes drops the Layer 4 accumulator and the closed-marker
// for an interaction (the RFC 0020 close / reset seam, sibling of
// [ChannelRouter.DiscardInteractionReplyBudget]). Idempotent — discarding an
// unknown interaction is a no-op.
//
// ORDERING (forward dependency, mirrors reply_budget.go): this seam prunes BOTH
// per-interaction maps, and both grow without it:
//   - `closedInteractions`: a vote-triggered close frees the accumulator but
//     leaves a marker so late traffic stays suppressed — one entry per closed
//     interaction, pruned only here.
//   - `endVotes`: an interaction that casts a vote but never reaches quorum
//     keeps its accumulator (votes go stale but are never removed; close is the
//     only inline free) — one entry per voting-but-not-closing interaction,
//     pruned only here.
//
// So the `interaction_id` + end-vote producer MUST wire this into the close path
// before it is enabled on real traffic — otherwise each distinct id (a 128-byte,
// attacker-influenceable token, see interaction_id.go) leaks an entry for the
// orchestrator's lifetime. Inert today (no producer), so unbounded growth cannot
// occur yet; the constraint binds when the producer lands.
func (r *ChannelRouter) DiscardInteractionEndVotes(interactionID string) {
	if interactionID == "" {
		return
	}
	r.endVoteMu.Lock()
	delete(r.endVotes, interactionID)
	delete(r.closedInteractions, interactionID)
	r.endVoteMu.Unlock()
}

// ResolveEndVotes applies the RFC 0030 Layer 4 (v0.3.8) end-vote quorum/window
// to every config-declared channel at startup, the sibling of
// [ChannelRouter.ResolveReplyBudgets]. Each declared channel uses its
// normalized `end_vote_threshold` / `end_vote_window` (K=2 / W=3 when omitted —
// LoadConfig normalizes zero to the default before this runs); store-resident
// channels not in config fall back to the defaults at read time
// ([ChannelRouter.EndVoteParamsFor]), so — unlike the reply budget — there is no
// store enumeration to do here. Idempotent; call once after ReconcileConfig.
func (r *ChannelRouter) ResolveEndVotes(_ context.Context, cfg *Config) error {
	if cfg == nil {
		return nil
	}
	for _, decl := range cfg.Channels {
		r.SetEndVoteParams(decl.CanonicalID(), decl.EndVoteThreshold, decl.EndVoteWindow)
	}
	return nil
}
