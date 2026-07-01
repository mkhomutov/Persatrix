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
// PRODUCER STATUS: both producers are live. The `interaction_id` producer
// (producer plan PR 1) — the router's resolver (interaction_resolver.go)
// stamps every publish, so this layer is load-bearing on the id side: every
// tracked publish advances the open interaction, and a quorum close both
// fires the discard seams and notifies the resolver via
// [ChannelRouter.markInteractionClosed] (IP8) so the next publish mints
// fresh. The VOTE producer (producer plan PR 2) — the Python
// `END_INTERACTION_VOTE` action publishes a real channel message with the
// `end_interaction_vote` flag (agents/end_vote_action.py; the key literal is
// pinned by the cross-language drift test), so quorums form on real traffic
// and the semantic terminator is the normal close, with idle rotation and
// the depth cap as the backstops.

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
// SUPPRESS fanout in three cases, so neither a converged conversation nor a
// vote-flag abuser draws new replies:
//   - the closing publish (the quorum was just reached);
//   - any later publish to an already-closed interaction;
//   - a redundant in-window duplicate vote — deduped to a no-op for the quorum,
//     so re-fanning it out is pure amplification and (because votes are exempt
//     from the Layer 2 reply budget) would let a participant flood the channel
//     past their reply cap. A first vote and a stale (out-of-window) re-vote are
//     real signals and still fan out.
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
		// Already closed — keep suppressing fanout for late traffic. That
		// suppression IS a Layer 4 governance drop (the conversation has
		// terminated and this publish is barred from cascading), so attribute it
		// like every other layer's drop. Since the producer's IP8 hook landed,
		// ordinary post-close traffic cannot reach here via Publish — the close
		// notified the resolver, so the channel's next publish mints fresh — and
		// this path catches only a commit that resolved the closing id just
		// before the quorum landed (the racing-commit window the tombstone
		// exists to cover) or a caller bypassing the resolver. The metric's
		// steady state is therefore ~zero; a sustained nonzero rate on
		// governance_drop{layer=end_vote} outside vote-spam is a resolver-bypass
		// signal, not converged-conversation noise. No Warn here: a racing
		// commit is expected, not anomalous (unlike a duplicate vote), so it is
		// metered and traced but not logged. Fired outside the lock.
		r.recordGovernanceDropEndVote(ctx, ct)
		// Lifecycle: a post-close NON-vote reply ran enforceReplyBudget BEFORE this
		// hook in Publish, which lazily RE-CREATES replyCounts[interactionID] (it
		// gates on interaction_id, not on closedInteractions). The close-time
		// DiscardInteractionReplyBudget already fired and never runs again for this
		// interaction, so without re-pruning here that re-created counter map would
		// leak for the orchestrator's lifetime — the per-interaction growth the
		// discard seam exists to bound. Re-discard it: the interaction is terminated,
		// so its reply budget is moot (final headroom was already recorded at close)
		// and post-close fanout is suppressed regardless. Idempotent and a no-op when
		// nothing was re-created (e.g. a post-close vote — votes never reserve a slot).
		r.DiscardInteractionReplyBudget(interactionID)
		return true
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

	// Vote-volume telemetry (§L): every vote action against a LIVE interaction
	// counts once — first vote, stale re-vote, and deduped in-window re-vote alike
	// — so end_vote_emitted measures how many votes were cast, paired with
	// interaction_closed (how many reached quorum) on the convergence dashboard. A
	// vote arriving AFTER the interaction has closed returned early above (it is a
	// post-close governance drop, not a fresh vote toward any quorum), so it is
	// deliberately not counted here. Fired outside the lock.
	if isVote && r.metrics != nil && r.metrics.EndVoteEmitted != nil {
		r.metrics.EndVoteEmitted.Add(ctx, 1, metric.WithAttributes(
			attribute.String("channel_type", string(ct)),
		))
	}
	if spam {
		r.recordEndVoteSpam(ctx, msg, ct, interactionID)
	}
	if closed {
		r.recordInteractionClosed(ctx, msg, ct, interactionID, recent)
		// Layer 4 → Layer 2 composition seam: record each tracked participant's
		// leftover reply allowance BEFORE discarding the counters, so the
		// reply_budget_remaining histogram observes the interaction's final state.
		r.recordReplyBudgetRemainingAtClose(ctx, msg.ChannelID, interactionID, ct)
		// Wire the §F reset seam: the interaction is closing, so its per-
		// participant reply counters are discarded (the seam reply_budget.go
		// reserved for the Layer 4 close path).
		r.DiscardInteractionReplyBudget(interactionID)
		// Producer IP8: notify the resolver so the channel's NEXT publish mints
		// a fresh interaction — the quorum ends one conversation, not the
		// channel. The closed id parks as the pending retiree; its tombstone
		// (added above) survives until the deferred discard, suppressing and
		// self-healing any commit that raced this close.
		r.markInteractionClosed(msg.ChannelID, interactionID, endVotesTrigger)
		// End-vote-close-propagation amendment (CP1/CP5): the closing vote's
		// fanout is suppressed (the caller's early return this `true` buys),
		// so the close must be DELIVERED, not inferred — fan the closing
		// message to every dispatch-served non-sender member as the marked
		// close notification, fire-and-forget, so each agent-local tracker
		// closes the scope now with the truthful `end_votes` cause instead
		// of burying the converged discussion as "went idle" an idle window
		// later.
		r.notifyInteractionClose(ctx, msg, ct, true) // exclude the voter: its own vote closed its tracker.
		return true
	}
	// Suppress fanout of a redundant in-window duplicate vote. An end-vote is
	// exempt from the Layer 2 reply budget (enforceReplyBudget) so a budget-
	// exhausted participant can still cast the terminating signal — but without
	// this, that exemption is a budget BYPASS: a participant could flag every
	// publish as a vote and flood the channel with fanned-out messages past their
	// cap. The vote is already deduped to a no-op for the quorum (the stamp was
	// just overwritten) and logged as spam for audit, so re-fanning it out adds
	// only N-way amplification with no signal. A participant's FIRST vote (spam
	// false) still fans out — others must see the terminal signal — and a STALE
	// re-vote (legitimate re-engagement, spam false by the in-window check above)
	// fans out as a fresh signal; only the redundant live duplicate is dropped
	// from fanout. (It is still persisted as a real message — history growth is
	// bounded by the per-channel message cap, not the reply budget.)
	return spam
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
func (r *ChannelRouter) recordEndVoteSpam(ctx context.Context, msg ChannelMessage, ct ChannelType, interactionID string) {
	r.logger.Warn("channels: duplicate end-of-interaction vote (deduped; possible vote-spam)",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", interactionID),
		zap.String("participant_id", msg.SenderID),
		zap.String("layer", governanceLayerEndVote),
	)
	// A redundant in-window duplicate vote has its fanout suppressed
	// (processEndVote returns true), so it IS a Layer 4 governance drop:
	// attribute it on the shared counter + trace span so vote-spam is visible on
	// the same governance-drop dashboard as the other layers (§B/§L).
	r.recordGovernanceDropEndVote(ctx, ct)
}

// recordGovernanceDropEndVote attributes a single Layer 4 fanout suppression on
// the shared `governance_drop{layer=end_vote}` counter and stamps the publish
// trace span. It is the one emission point for the two ways Layer 4 drops a
// publish — a redundant in-window duplicate vote ([ChannelRouter.recordEndVoteSpam])
// and any publish to an already-closed interaction (the post-close path in
// [ChannelRouter.processEndVote]) — so both land on the identical dashboard /
// trace query as the other layers. Nil-safe and cheap on a span-less/unsampled
// publish (see annotateGovernanceDropSpan).
func (r *ChannelRouter) recordGovernanceDropEndVote(ctx context.Context, ct ChannelType) {
	if r.metrics != nil && r.metrics.GovernanceDrop != nil {
		r.metrics.GovernanceDrop.Add(ctx, 1, metric.WithAttributes(
			attribute.String("channel_type", string(ct)),
			attribute.String("layer", governanceLayerEndVote),
		))
	}
	annotateGovernanceDropSpan(ctx, governanceLayerEndVote)
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
// ORDERING (discharged): the producer wires this seam via the resolver's
// one-generation-deferred discards (interaction_resolver.go, producer plan
// IP4/IP7) — a retired id has both maps pruned at its channel's next rotation
// OR next vote-close, whichever comes first ([ChannelRouter.markInteractionClosed]
// discharges the previous retiree too). In a channel that never rotates
// (thread, or an explicit 0 idle window) the next vote-close is the only
// discharge point, so the most recent closed id's tombstone persists there
// until one arrives — a deliberate bounded residue of at most one per
// channel (pinned by TestInteractionResolver_NeverRotatingChannelBoundsTombstones),
// not unbounded growth. The deferral is the design, not a gap: the close site
// deliberately cannot prune itself (the tombstone must outlive any commit
// racing the close), and IP2's claim-override gives "suppress late traffic" a
// natural endpoint, which is what makes the tombstone prunable at all. Only
// router-minted uuids key these maps now.
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
