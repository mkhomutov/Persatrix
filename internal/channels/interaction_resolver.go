package channels

// interaction_resolver.go — the RFC 0030 interaction-id producer
// (docs/rfcs/0030-interaction-id-producer-pr-plan.md, PR 1). The resolver is
// the orchestrator-side authority for "which interaction is this publish part
// of" (IP1): [ChannelRouter.publishCommit] calls it next to the cascade-depth
// clamp and stamps the resolved id onto the message metadata, where it
// persists and rides the existing fanout lift to
// `ChannelMessageEvent.interaction_id`. Inbound claims never key governance
// state (IP2) — the resolution replaces them, so the per-interaction maps
// (`replyCounts`, `endVotes`, `closedInteractions`) are only ever keyed by
// router-minted uuids.
//
// Scope (IP3): one open interaction per channel (deliberately per channel,
// not RFC 0020 §G's per-agent — the governance layers compose only on a
// shared key). `group`/`dm` rotate lazily on the publish path once the idle
// window passes; `thread` channels never rotate (the thread IS the
// interaction).
//
// Rotation defers the discard seams one generation (IP4): retiring an id
// emits `interaction_closed{trigger=idle}` immediately, but its
// `DiscardInteractionReplyBudget`/`DiscardInteractionEndVotes` fire at the
// channel's NEXT rotation. The one-generation grace closes a commit-path
// race — `publishCommit` runs on each caller's goroutine with per-hook leaf
// mutexes, so a concurrent commit that resolved the old id just before
// rotation can bump `replyCounts[old]` after an immediate discard, recreating
// the lifetime entry the seam exists to prune. Deferring lets every in-flight
// commit (milliseconds) drain long before the seams fire (≥ one idle window
// later), and keeps the `closedInteractions` tombstone alive across a Layer 4
// close so the landed post-close self-heal keeps working in the interim. At
// most one pending retiree per channel → the table is ≤ 2×`max_channels`
// ids — bounded state, not a leak.
//
// The Layer 4 quorum close notifies the resolver via
// [ChannelRouter.markInteractionClosed] (IP8) so the next publish mints fresh
// per RFC 0020 §C never-reopen — without it, the resolver would keep stamping
// the closed id for up to a full idle window and every publish in it would be
// post-close-suppressed: a quorum would silence the channel instead of ending
// one conversation.
//
// Restart (IP5): the table is in-memory; a restart loses it and the next
// publish mints fresh — RFC 0020 §C inheritance. The maps keyed by the lost
// ids died with the process too, so nothing leaks.

import (
	"context"
	"time"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

// idleTrigger is the `trigger` attribute value on the
// `channel.conversation.interaction_closed` counter for a lazy idle rotation —
// the sibling of [endVotesTrigger] on the same instrument (§L; the
// governance-layers plan reserved `idle`/`structural`/`cost`).
const idleTrigger = "idle"

// openInteraction is one channel's resolver entry: the open interaction id,
// its idle clock, and the one pending retiree whose discard seams fire at the
// next rotation (IP4). A vote-close empties `id` (next resolve mints fresh)
// while parking the closed id in `retired` so its tombstone outlives any
// racing commit.
type openInteraction struct {
	id           string
	lastActivity time.Time
	retired      string
}

// resolveInteractionID returns the open interaction id for `channelID`,
// minting or rotating as needed, and is the ONLY writer of the governance
// interaction key space (IP2). `inbound` is the publisher's claim, used solely
// for the divergence debug log. Seam firing and telemetry run outside
// interactionMu — the discard seams take their own leaf mutexes, and holding
// two governance mutexes at once would mint a lock-ordering edge no other
// path has.
func (r *ChannelRouter) resolveInteractionID(ctx context.Context, channelID string, ct ChannelType, inbound string) string {
	now := r.interactionNow()

	r.interactionMu.Lock()
	entry := r.openInteractions[channelID]
	if entry == nil {
		entry = &openInteraction{}
		r.openInteractions[channelID] = entry
	}
	window := r.idleWindowLocked(channelID)
	var rotated, discard string
	if entry.id != "" && ct != ChannelTypeThread && window > 0 && now.Sub(entry.lastActivity) > window {
		rotated = entry.id
		discard = entry.retired // the previous retiree's deferred seams fire now
		entry.retired = rotated
		entry.id = ""
	}
	if entry.id == "" {
		entry.id = uuid.NewString()
	}
	entry.lastActivity = now
	resolved := entry.id
	r.interactionMu.Unlock()

	if discard != "" {
		r.DiscardInteractionReplyBudget(discard)
		r.DiscardInteractionEndVotes(discard)
	}
	if rotated != "" {
		r.recordInteractionClosedIdle(ctx, channelID, ct, rotated)
	}
	if inbound != "" && inbound != resolved {
		// IP2: the claim is overridden, not honoured. Debug, not warn — the
		// common producer of a stale claim is an agent echoing the id of the
		// interaction it was *dispatched* in after a rotation, which is
		// expected traffic, not an attack signature.
		r.logger.Debug("channels: inbound interaction_id claim overridden by resolver",
			zap.String("channel_id", channelID),
			zap.String("claimed", inbound),
			zap.String("resolved", resolved))
	}
	return resolved
}

// markInteractionClosed is the Layer 4 → resolver close notification (IP8):
// the quorum close site calls it so the channel's next publish mints fresh
// instead of stamping the closed id into post-close suppression for the rest
// of the idle window. The closed id parks as the pending retiree — its
// `closedInteractions` tombstone (left by the close) survives until the
// deferred discard, long enough to suppress and self-heal any commit racing
// the close. A stale call (the open id moved on already) is a no-op.
func (r *ChannelRouter) markInteractionClosed(channelID, interactionID string) {
	r.interactionMu.Lock()
	entry := r.openInteractions[channelID]
	var discard string
	if entry != nil && entry.id == interactionID {
		discard = entry.retired
		entry.retired = interactionID
		entry.id = ""
	}
	r.interactionMu.Unlock()

	if discard != "" {
		r.DiscardInteractionReplyBudget(discard)
		r.DiscardInteractionEndVotes(discard)
	}
}

// SetInteractionIdleTimeout resolves the per-channel idle window (seconds) for
// `channelID`. Zero disables idle rotation for the channel (the documented
// thread posture, usable anywhere); negative falls back to the fleet default
// at read time (the config validator rejects negatives upstream, so this is
// belt-and-braces, mirroring SetFloorControl's normalization).
func (r *ChannelRouter) SetInteractionIdleTimeout(channelID string, seconds int) {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	if seconds < 0 {
		delete(r.interactionIdleTimeouts, channelID)
		return
	}
	r.interactionIdleTimeouts[channelID] = time.Duration(seconds) * time.Second
}

// idleWindowLocked returns the channel's resolved idle window. Caller holds
// interactionMu. An absent entry falls back to the router's fleet default —
// store-resident channels not declared in config need no startup enumeration
// (the EndVoteParamsFor read-time-fallback pattern).
func (r *ChannelRouter) idleWindowLocked(channelID string) time.Duration {
	if w, ok := r.interactionIdleTimeouts[channelID]; ok {
		return w
	}
	return r.defaultInteractionIdleTimeout
}

// recordInteractionClosedIdle fires the structured close log + the
// `interaction_closed{trigger=idle}` counter for a lazy idle rotation — the
// sibling of [ChannelRouter.recordInteractionClosed] (trigger=end_votes).
// Lazy means the emission lags the semantic close by up to the gap to the
// channel's next publish (plan OQ 3); the timestamp of record is the emission.
func (r *ChannelRouter) recordInteractionClosedIdle(ctx context.Context, channelID string, ct ChannelType, interactionID string) {
	r.logger.Info("channels: interaction closed by idle rotation",
		zap.String("channel_id", channelID),
		zap.String("interaction_id", interactionID),
		zap.String("trigger", idleTrigger),
	)
	if r.metrics != nil && r.metrics.InteractionClosed != nil {
		r.metrics.InteractionClosed.Add(ctx, 1, metric.WithAttributes(
			attribute.String("channel_type", string(ct)),
			attribute.String("trigger", idleTrigger),
		))
	}
}

// ResolveInteractionIdleTimeouts applies the per-channel idle windows for
// every config-declared channel at startup, the sibling of
// [ChannelRouter.ResolveEndVotes]. Store-resident channels not in config fall
// back to the fleet default at read time ([ChannelRouter.idleWindowLocked]),
// so there is no store enumeration. Config-declared channels are always
// groups (`CanonicalID` prefixes `group:`), so the IP3 thread-warning case is
// unreachable from this path today — the type rule in
// [ChannelRouter.resolveInteractionID] is what actually protects threads.
// Idempotent; call once after ReconcileConfig.
func (r *ChannelRouter) ResolveInteractionIdleTimeouts(_ context.Context, cfg *Config) error {
	if cfg == nil {
		return nil
	}
	if cfg.DefaultInteractionIdleTimeoutSeconds != nil {
		// The fleet default also covers store-resident channels not declared
		// in config, via the idleWindowLocked read-time fallback.
		r.interactionMu.Lock()
		r.defaultInteractionIdleTimeout = time.Duration(*cfg.DefaultInteractionIdleTimeoutSeconds) * time.Second
		r.interactionMu.Unlock()
	}
	for _, decl := range cfg.Channels {
		r.SetInteractionIdleTimeout(decl.CanonicalID(),
			decl.ResolveInteractionIdleTimeoutSeconds(cfg.DefaultInteractionIdleTimeoutSeconds))
	}
	return nil
}
