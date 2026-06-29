package channels

import (
	"context"
	"fmt"
)

// router_autonomous.go holds the RFC 0052 (v0.3.11) per-channel autonomous-block
// registry — the setter/getter the router exposes for the resolved autonomous
// config and the startup resolver that seeds it from `config/channels.yaml`. Split
// out of router.go (at the 500-line review cap), mirroring router_reasoning.go's
// split of the RFC 0051 reasoning block.
//
// The `autonomousMu` mutex + `autonomous` map are declared on [ChannelRouter] in
// router.go (a struct field must live with the type); only the methods live here.
//
// PR 1 makes the resolved value router-held so the RFC 0050 GET surface can report
// each channel's effective `autonomous.*` with provenance. NOTHING consumes the
// resolved value at runtime yet — the convene path is PR 3 — so this is a dark
// backend: the registry is populated and surfaced, never acted on.

// SetAutonomous stamps the resolved RFC 0052 autonomous block for `channelID`. The
// value is normalized first ([AutonomousConfig.normalized]) so a partially-set
// config (e.g. `enabled: true` only) is stored as a complete rung. Wired two ways
// like [ChannelRouter.SetReasoning]: at startup via [ChannelRouter.ResolveAutonomous]
// and at runtime through the RFC 0050 apply path
// ([ChannelRouter.applyOverridesToRouter]). The mutex makes the runtime call safe
// concurrently with traffic.
func (r *ChannelRouter) SetAutonomous(channelID string, a AutonomousConfig) {
	a = a.normalized()
	r.autonomousMu.Lock()
	defer r.autonomousMu.Unlock()
	r.autonomous[channelID] = a
}

// AutonomousFor returns the resolved RFC 0052 autonomous block for `channelID`. A
// channel with no resolved entry falls back to [DefaultAutonomousConfig] — the
// disabled default an un-configured channel ships with — so the read is always a
// complete, sensible value.
func (r *ChannelRouter) AutonomousFor(channelID string) AutonomousConfig {
	r.autonomousMu.Lock()
	defer r.autonomousMu.Unlock()
	if a, ok := r.autonomous[channelID]; ok {
		return a
	}
	return DefaultAutonomousConfig()
}

// ResolveAutonomous applies the RFC 0052 autonomous block to every group channel
// known at startup, the per-channel sibling of [ChannelRouter.ResolveReasoning].
// Each config-declared channel uses its resolved (load-normalized) `autonomous`
// block; every other group channel present in the store picks up the disabled
// default.
//
// DM and thread channels are skipped: autonomous convening is an open-floor group
// concept. Call once after [ChannelRouter.ReconcileConfig]; idempotent.
func (r *ChannelRouter) ResolveAutonomous(ctx context.Context, cfg *Config) error {
	configured := make(map[string]bool)
	if cfg != nil {
		for _, decl := range cfg.Channels {
			id := decl.CanonicalID()
			configured[id] = true
			r.SetAutonomous(id, decl.Autonomous)
		}
	}
	all, err := r.store.ListChannels(ctx, 0, "")
	if err != nil {
		return fmt.Errorf("channels: resolve autonomous: list channels: %w", err)
	}
	for _, ch := range all {
		if ch.Type != ChannelTypeGroup || configured[ch.ID] {
			continue
		}
		r.SetAutonomous(ch.ID, DefaultAutonomousConfig())
	}
	return nil
}

// validateAutonomousConvener enforces the cross-field convener rules that need the
// live roster (RFC 0052 OQ #1) against a runtime patch: when the merged autonomous
// block is armed, the convener must be a declared member of the channel AND not an
// `observer` (respond: never). It authors the opening turn, so a non-member is a
// guaranteed dispatch failure and an observer is suppressed by the receiver gate
// before any LLM — both failed loudly at apply instead. Mirrors
// [ChannelRouter.validateEscalationChair] (member + observer); runs before the write
// so a bad convener never persists.
//
// The non-membership convener rules (non-empty, distinct from the chair) and the
// mandatory cap are computable from the override struct and live in
// [ChannelConfigOverrides.validateAutonomous]; this method adds only the parts that
// need the live roster.
func (r *ChannelRouter) validateAutonomousConvener(ctx context.Context, channelID string, patch ChannelConfigOverrides) error {
	if !patch.Autonomous.effectiveEnabled() {
		return nil
	}
	convener := patch.Autonomous.effectiveConvener()
	if convener == "" {
		return nil // the empty-convener case is the struct-level rule's to report
	}
	members, err := r.store.GetMembers(ctx, channelID)
	if err != nil {
		return fmt.Errorf("channels: apply config %s: load members: %w", channelID, err)
	}
	for i := range members {
		if members[i].ParticipantID == convener {
			// An observer (legacy `never`) convener can never author the opening turn
			// — its receiver gate suppresses it — so reject it, mirroring the chair.
			if members[i].RespondPolicy.Normalize() == RespondNever {
				return fmt.Errorf("channels: apply config %s: %w: %q is an observer (respond: never) and can never author the opening turn",
					channelID, ErrInvalidAutonomousConvener, convener)
			}
			return nil
		}
	}
	return fmt.Errorf("channels: apply config %s: %w: %q is not a declared member; the convener authors the opening turn",
		channelID, ErrInvalidAutonomousConvener, convener)
}
