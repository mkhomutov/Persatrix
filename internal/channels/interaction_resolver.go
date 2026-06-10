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
// commit (milliseconds) drain long before the seams fire — ≥ one idle window
// later on the idle path; generational (the channel's next rotation OR next
// vote-close), not time-bounded, when quorum closes chain — and keeps the
// `closedInteractions` tombstone alive across a Layer 4 close so the landed
// post-close self-heal keeps working in the interim. In a channel that never
// rotates (thread, or explicit 0 window) the next vote-close is the ONLY
// discharge point, so the most recent closed id's tombstone persists there —
// at most one per channel, the deliberate bounded residue. At most one
// pending retiree per channel, and a rejected publish never retains an entry
// ([ChannelRouter.settleInteraction] deletes a never-committed one), so the
// table holds ≤ 2 ids per channel WITH PERSISTED HISTORY — bounded by real
// channels, not by caller-supplied channel ids, and not a leak.
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
//
// `idCommitted` records whether the current `id` has at least one PERSISTED
// publish, and `lastActivity` is the time of the channel's last persisted
// publish — both written by [ChannelRouter.settleInteraction], never by the
// resolve itself, so a rejected publish (non-member, throttled, store error)
// is invisible to the idle clock. A minted-but-uncommitted id is tentative:
// it never idle-rotates (rotating it would emit `interaction_closed` for an
// interaction containing zero messages) and is adopted by the next committed
// publish instead.
type openInteraction struct {
	id           string
	idCommitted  bool
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
//
// The returned settle hook is the resolver's half of the reply-reservation
// pattern ([ChannelRouter.enforceReplyBudget]'s release): the caller invokes
// it exactly once, with the persist outcome, and only THAT advances the idle
// clock or retains a fresh entry — see [ChannelRouter.settleInteraction].
// The split is deliberate: minting and rotation stay visible at resolve time
// because concurrent publishes must agree on the open id and a rotated id was
// past its window regardless of this publish's fate (the rotation is lazy
// either way — only its trigger moves from "next commit" to "next attempt"),
// while everything that asserts "a publish happened" reconciles to
// persistence. Without the split, a rejected publish leaks an entry keyed by
// the caller-supplied channel id (the unauthenticated REST path reaches here
// before the store's membership check — unbounded attacker-influenceable
// growth), and a throttled participant's in-window retries hold its own
// exhausted interaction open forever, so the idle rotation that would reset
// its budget never fires.
func (r *ChannelRouter) resolveInteractionID(ctx context.Context, channelID string, ct ChannelType, inbound string) (string, func(persisted bool)) {
	now := r.interactionNow()

	r.interactionMu.Lock()
	entry := r.openInteractions[channelID]
	if entry == nil {
		entry = &openInteraction{}
		r.openInteractions[channelID] = entry
	}
	window := r.idleWindowLocked(channelID)
	var rotated, discard string
	// Only a committed id can idle out: an uncommitted mint has no messages,
	// so "rotating" it would close an interaction that never existed on the
	// record (and its stale lastActivity predates the mint anyway).
	if entry.id != "" && entry.idCommitted && ct != ChannelTypeThread && window > 0 && now.Sub(entry.lastActivity) > window {
		rotated = entry.id
		discard = entry.retired // the previous retiree's deferred seams fire now
		entry.retired = rotated
		entry.id = ""
	}
	if entry.id == "" {
		entry.id = uuid.NewString()
		entry.idCommitted = false
	}
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
	return resolved, func(persisted bool) { r.settleInteraction(channelID, resolved, now, persisted) }
}

// settleInteraction reconciles the resolver table to the persist outcome of
// one publish (the hook [ChannelRouter.resolveInteractionID] returned).
//
// Persisted: the resolved id becomes committed and the idle clock advances to
// the resolve-time `now` — the ONLY writer of `lastActivity`, so the window
// measures channel history, not attempts (the reply budget's "counter tracks
// history, not attempts" invariant, applied to the clock). A nil entry here
// means a concurrent rejected publish that shared this tentative mint settled
// first and deleted it; recreate it so the persisted row's stamped id stays
// the channel's open interaction.
//
// Rejected: drop the entry iff this publish's tentative state is ALL it holds
// — same id, never committed, no pending retiree (a retiree implies committed
// history whose deferred discard must still fire). That makes the table's
// bound real: entries exist only for channels with at least one persisted
// publish, never for arbitrary caller-supplied channel ids.
//
// Orphaned commit: a persisted id that is no longer the entry's open id was
// stranded by an interleaving — a sibling rejected publish deleted the shared
// tentative mint and a third publish reminted before this settle ran. The
// orphan's row is already channel history, so its committed governance state
// (the reply-budget reservation, a possible vote or tombstone) must still
// reach a discard seam: park it as the pending retiree when the slot is free,
// giving it the same next-rotation/next-close discharge as any retiree. An
// OCCUPIED slot is never clobbered — the occupant's one-generation deferral
// protects a real commit racing a rotation/close (IP4), and displacing it
// early would reopen that race. The skip's residue (a settle whose persist
// spanned an entire rotation cycle leaves one untracked counter map) is
// accepted: router-minted, requires a publish in flight for a full idle
// window, and not reachable at attacker-chosen rate.
func (r *ChannelRouter) settleInteraction(channelID, resolved string, now time.Time, persisted bool) {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	entry := r.openInteractions[channelID]
	if !persisted {
		if entry != nil && entry.id == resolved && !entry.idCommitted && entry.retired == "" {
			delete(r.openInteractions, channelID)
		}
		return
	}
	if entry == nil {
		entry = &openInteraction{id: resolved}
		r.openInteractions[channelID] = entry
	}
	if entry.id == resolved {
		entry.idCommitted = true
	} else if entry.retired == "" {
		// `resolved` was orphaned mid-flight (see doc): park it so the next
		// rotation/close discharges its governance state. A non-empty slot
		// already holds either `resolved` itself (the racing-commit case —
		// nothing to do) or an earlier retiree whose deferral must win.
		entry.retired = resolved
	}
	if now.After(entry.lastActivity) {
		entry.lastActivity = now
	}
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
