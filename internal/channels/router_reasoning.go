package channels

import (
	"context"
	"fmt"

	"go.uber.org/zap"
)

// router_reasoning.go holds the RFC 0051 (v0.3.10) per-channel reasoning-block
// registry — the setter/getter the router exposes for the resolved
// reasoning-before-posting config and the startup resolver that seeds it from
// `config/channels.yaml`. Split out of router.go (at the 500-line review cap),
// mirroring router_salience.go's split of the Tier B channel-size cap.
//
// The `reasoningMu` mutex + `reasoning` map are declared on [ChannelRouter] in
// router.go (a struct field must live with the type); only the methods live here.
//
// PR 4 makes the resolved value router-held so the RFC 0050 GET surface can report
// each channel's effective `reasoning.{mode,model,depth,revise}` with provenance.
// The agent-side seam's consumption of `mode`/`model` (the dispatch-envelope
// wiring to the Python persona runtime) rides the go-live, not this dark backend.

// SetReasoning stamps the resolved RFC 0051 reasoning block for `channelID`. The
// value is normalized first ([ReasoningConfig.normalized]) so a partially-set
// config (e.g. `mode: bid` only) is stored as a complete rung. Wired two ways like
// [ChannelRouter.SetSalienceMaxChannelMembers]: at startup via
// [ChannelRouter.ResolveReasoning] and at runtime through the RFC 0050 apply path
// ([ChannelRouter.applyOverridesToRouter]). The mutex makes the runtime call safe
// concurrently with traffic.
func (r *ChannelRouter) SetReasoning(channelID string, rc ReasoningConfig) {
	rc = rc.normalized()
	r.reasoningMu.Lock()
	defer r.reasoningMu.Unlock()
	r.reasoning[channelID] = rc
}

// ReasoningFor returns the resolved RFC 0051 reasoning block for `channelID`. A
// channel with no resolved entry falls back to [DefaultReasoningConfig] — the same
// off / fast / shallow / 0 rung an un-configured channel ships with — so the read
// is always a complete, sensible value.
func (r *ChannelRouter) ReasoningFor(channelID string) ReasoningConfig {
	r.reasoningMu.Lock()
	defer r.reasoningMu.Unlock()
	if rc, ok := r.reasoning[channelID]; ok {
		return rc
	}
	return DefaultReasoningConfig()
}

// ResolveReasoning applies the RFC 0051 reasoning block to every group channel
// known at startup, the per-channel sibling of [ChannelRouter.ResolveSalienceCaps].
// Each config-declared channel uses its resolved (load-normalized) `reasoning`
// block; every other group channel present in the store — e.g. a runtime-created
// channel that survived a restart — picks up the default rung. A `model: quality`
// channel is logged (warn) as it is stamped, surfacing the discouraged-economics
// note for YAML-declared blocks the same way the runtime apply path does.
//
// DM and thread channels are skipped: the deliberation runs only on open-floor
// group traffic. Call once after [ChannelRouter.ReconcileConfig]; idempotent.
func (r *ChannelRouter) ResolveReasoning(ctx context.Context, cfg *Config) error {
	configured := make(map[string]bool)
	if cfg != nil {
		for _, decl := range cfg.Channels {
			id := decl.CanonicalID()
			configured[id] = true
			if decl.Reasoning.Model == ReasoningModelQuality {
				r.logger.Warn("channels: reasoning.model=quality defeats the cheap-pass economics (RFC 0051 §F); prefer fast",
					zap.String("channel_id", id))
			}
			r.SetReasoning(id, decl.Reasoning)
		}
	}
	all, err := r.store.ListChannels(ctx, 0, "")
	if err != nil {
		return fmt.Errorf("channels: resolve reasoning: list channels: %w", err)
	}
	for _, ch := range all {
		if ch.Type != ChannelTypeGroup || configured[ch.ID] {
			continue
		}
		// PR 6 go-live: a store-only group channel (e.g. runtime-created, no YAML
		// block) picks up the GOVERNED default — `bid` if it has a salience-gated
		// member, else `off`. So a channel becomes `bid`-by-default the moment it
		// is governed, without an explicit reasoning block (RFC 0051 §G / OQ 2).
		r.SetReasoning(ch.ID, governedReasoningBase(r.channelGoverned(ctx, ch.ID)))
	}
	return nil
}
