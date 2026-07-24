package channels

// interaction_idle.go — the RFC 0030 idle-rotation KNOBS and telemetry: the
// per-channel idle-window setter/getters, the startup resolver that seeds
// them from `config/channels.yaml`, and the idle-close record emitter. Split
// out of interaction_resolver.go when the PR #718 review's fresh-mint disarm
// fix pushed that file past the 500-line review cap (the
// interaction_close_latch.go precedent) — the resolver file keeps the hot-path
// resolve/settle lifecycle; this file holds the config plumbing that changes
// on the knob cadence (the router_reasoning.go / router_autonomous.go split
// rationale). The maps live on [ChannelRouter] in router.go, guarded by the
// same interactionMu the resolver holds when it reads them
// ([ChannelRouter.idleWindowLocked]).

import (
	"context"
	"time"

	"go.uber.org/zap"
)

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

// InteractionIdleTimeoutFor reports the channel's resolved idle window in whole
// seconds and whether an explicit per-channel entry exists (`set` false → the
// fleet default applies). Exposed for ops introspection and the RFC 0050
// `GET …/config` effective-value read, mirroring [ChannelRouter.FloorControlFor];
// the hot path reads [ChannelRouter.idleWindowLocked]. An explicit per-channel 0
// (idle rotation off) reads back as seconds 0 / set true — distinct from an
// absent entry that resolves to the same 0 only if the fleet default is 0.
func (r *ChannelRouter) InteractionIdleTimeoutFor(channelID string) (seconds int, set bool) {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	w, ok := r.interactionIdleTimeouts[channelID]
	if !ok {
		return int(r.defaultInteractionIdleTimeout / time.Second), false
	}
	return int(w / time.Second), true
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
	r.recordInteractionClosedMetric(ctx, ct, idleTrigger, interactionID)
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
		secs := decl.ResolveInteractionIdleTimeoutSeconds(cfg.DefaultInteractionIdleTimeoutSeconds)
		r.SetInteractionIdleTimeout(decl.CanonicalID(), secs)
	}
	// ISSUE-0095: surface the resolved window map once at startup so a wrong
	// resolved window (a 0/rotation-off where one wasn't intended, or the
	// fleet default landing where a per-channel override was expected) is
	// visible without a repro. Store-resident channels not declared here fall
	// back to default_window at read time ([ChannelRouter.idleWindowLocked]).
	//
	// The map is read BACK from the router, not re-derived from `secs`: a
	// re-derivation would drift from [ChannelRouter.SetInteractionIdleTimeout]'s
	// semantics — notably its `seconds < 0` delete sentinel, which resolves the
	// channel to default_window while a raw stringify would misreport "-1s".
	// The diagnostic must show the window the resolver will actually use.
	r.interactionMu.Lock()
	def := r.defaultInteractionIdleTimeout
	windows := make(map[string]string, len(cfg.Channels))
	for _, decl := range cfg.Channels {
		windows[decl.CanonicalID()] = r.idleWindowLocked(decl.CanonicalID()).String()
	}
	r.interactionMu.Unlock()
	r.logger.Info("channels: interaction idle windows resolved",
		zap.Duration("default_window", def),
		zap.Any("windows", windows),
	)
	return nil
}
