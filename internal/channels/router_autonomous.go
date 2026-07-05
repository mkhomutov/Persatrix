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
	r.autonomous[channelID] = a
	r.autonomousMu.Unlock()
	if !a.Enabled {
		// RFC 0052 PR 4b-ii (PR #718 review): a block disabled mid-arm MUST
		// also drop any armed synthesis close. Every arm seam — the reply claim,
		// the traffic withhold, the timeout net's own re-check — gates on
		// `autonomous.enabled`, so once disabled the chair's synthesis reply
		// re-fans as an ordinary stimulus and reopens the discussion, while the
		// orphaned timeout net (whose identity CAS still matches) closes that
		// now-live conversation ~2 minutes later, racing the replies it just
		// permitted. Disarming here abandons the pending close and leaves the
		// interaction open under the operator's manual control — the point of
		// disabling. Done OUTSIDE autonomousMu so the resolve's
		// autonomousMu→interactionMu lock order is never inverted (this is the
		// single chokepoint both startup ResolveAutonomous and the RFC 0050
		// applyOverridesToRouter path flow through). A no-op at startup (nothing
		// is armed yet) and on any still-enabled apply.
		r.disarmChannelSynthesis(channelID)
	}
}

// DisarmChannelSynthesis is the exported form of [ChannelRouter.disarmChannelSynthesis]
// for callers outside the package whose action retires a channel out from under any
// armed synthesis close — currently the channel-delete HTTP handler (PR #718 review:
// deleting a channel mid-arm left the timeout net orphaned, firing ~120s later against
// a channel the store no longer has). Same disarm [SetAutonomous]'s disable branch
// above uses, exported rather than duplicated. Safe to call on a channel with no armed
// close (nil-tolerant) and safe to call redundantly (each disarm is idempotent past the
// first).
func (r *ChannelRouter) DisarmChannelSynthesis(channelID string) {
	r.disarmChannelSynthesis(channelID)
}

// AutonomousFor returns the resolved RFC 0052 autonomous block for `channelID`. A
// channel with no resolved entry falls back to [DefaultAutonomousConfig] — the
// disabled default an un-configured channel ships with — so the read is always a
// complete, sensible value. A read lock, unlike the sibling knob getters
// (PR #716 review): the bounded close put this read on the per-publish hot
// path of EVERY channel — the resolver's latch gate (resolveInteractionID)
// and the fanout's per-publish snapshot (resolved once in
// [ChannelRouter.fanout] and threaded to the floor head check and the tail
// trigger) — where a plain Mutex would serialize all publishes on one
// router-global lock just to read a value written at startup and on the rare
// RFC 0050 apply.
func (r *ChannelRouter) AutonomousFor(channelID string) AutonomousConfig {
	r.autonomousMu.RLock()
	defer r.autonomousMu.RUnlock()
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

// validateAutonomousChannelType rejects arming a non-group (DM/thread) channel.
// Autonomous convening is an open-floor GROUP concept — [ChannelRouter.ResolveAutonomous]
// seeds only group channels and the convene path (PR 3) dispatches an open-floor seed
// turn — so an armed DM/thread is un-convenable by construction and the validation
// otherwise accepts it (the dark-backend footgun this guard closes). The load path
// needs no mirror: config declares only group channels. Runs only when the merged
// block is armed and before the write, so a non-group arm never persists.
func (r *ChannelRouter) validateAutonomousChannelType(ctx context.Context, channelID string, patch ChannelConfigOverrides) error {
	if !patch.Autonomous.effectiveEnabled() {
		return nil
	}
	ch, err := r.store.GetChannel(ctx, channelID)
	if err != nil {
		return fmt.Errorf("channels: apply config %s: load channel: %w", channelID, err)
	}
	if ch.Type != ChannelTypeGroup {
		return fmt.Errorf("channels: apply config %s: %w: %q is a %s channel (autonomous convening is an open-floor group concept)",
			channelID, ErrAutonomousNotGroup, channelID, ch.Type)
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
//
// DRIFT LOCKOUT (deliberate, escalation-chair-symmetric). On a STORE-CANONICAL
// channel (revision > 0) the merge base is the stored blob, which still carries the
// convener even after that member leaves the roster (RemoveMember does not touch the
// config blob). So once a convener drifts out of membership, this rule rejects EVERY
// subsequent edit — even an unrelated one — until the operator fixes it. That is the
// safety contract surfacing a broken armed channel loudly, exactly as
// [ChannelRouter.validateEscalationChair] does for the chair; it is NOT softened
// convener-only, because diverging from the chair would create a worse asymmetry.
// It is always RECOVERABLE: disarming (`autonomous.enabled:false`), clearing the
// block (`autonomous:null`), or re-pointing the convener short-circuits the rule via
// [AutonomousOverrides.effectiveEnabled] / a valid member. The revision-0 first edit
// instead DROPS a drifted block ([Server.autonomousBaseline]) so an unrelated first
// edit is never blocked; the two paths differ because a revision-0 block is not yet
// canonical. (Pinned by TestChannelConfig_AutonomousConvenerDriftLocksThenRecovers.)
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
	if _, err := classifyConvenerMember(members, convener); err != nil {
		return fmt.Errorf("channels: apply config %s: %w", channelID, err)
	}
	return nil
}

// memberByID returns a pointer to the member with ParticipantID `id`, or nil
// when `id` names no member of `members`. The result aliases the slice element
// (callers pass it to [ChannelRouter.dispatchTo] by value), so it is valid only
// while `members` is — every caller uses it within the same resolve. This
// single-sources the by-id lookup the chair-escalation, config-apply,
// convene-classify, and §D synthesis-close paths otherwise repeated inline.
func memberByID(members []Member, id string) *Member {
	for i := range members {
		if members[i].ParticipantID == id {
			return &members[i]
		}
	}
	return nil
}

// classifyConvenerMember locates `convener` in the channel's live roster and
// reports why it cannot author the opening turn, or returns the resolved
// *Member (a pointer into `members`) when it can. The error wraps
// [ErrInvalidAutonomousConvener] with the shared operator-facing reason and NO
// op-prefix — each caller adds its own "channels: <op> %s:" context via %w, so
// the apply path keeps its "apply config" wording and the convene path its
// "convene" wording while the convener disposition rule itself lives in exactly
// one place. Single-sourcing it across [ChannelRouter.validateAutonomousConvener]
// (config-apply) and [ChannelRouter.ConveneChannel] (convene) is what stops the
// two enforcement points drifting — e.g. a future `addressed`-convener allowance
// or a new disposition must change only here. (The load path's mirror,
// [validateConvenerMembership], reads the distinct config-level member slice and
// stays separate by type.)
func classifyConvenerMember(members []Member, convener string) (*Member, error) {
	convenerMember := memberByID(members, convener)
	if convenerMember == nil {
		return nil, fmt.Errorf("%w: %q is not a declared member; the convener authors the opening turn",
			ErrInvalidAutonomousConvener, convener)
	}
	// An observer (legacy `never`) convener can never author the opening turn
	// — its receiver gate suppresses it — so reject it, mirroring the chair.
	if convenerMember.RespondPolicy.Normalize() == RespondNever {
		return nil, fmt.Errorf("%w: %q is an observer (respond: never) and can never author the opening turn",
			ErrInvalidAutonomousConvener, convener)
	}
	return convenerMember, nil
}
