package channels

import (
	"context"
	"fmt"

	"go.uber.org/zap"
)

// config_apply.go holds the RFC 0050 Phase 1 PR 2 apply path — the single
// validated route from a sparse [ChannelConfigOverrides] patch to BOTH the
// canonical channel store and the router's in-memory governance maps — plus the
// boot repoint ([ChannelRouter.ResolveFromStore]) that seeds those maps from the
// store at startup.
//
// PR 1 landed storage only: overrides were persisted and read back but never
// consulted at runtime (the router was still seeded from `config/channels.yaml`
// by the per-knob `Resolve*` boot calls). PR 2 closes that gap. The flow shifts
// from YAML → router to YAML →(store)→ router: an apply writes the store and the
// router together, and at boot the router is overlaid from the store for any
// channel an operator has edited.
//
// Six of the seven governance knobs are router-held and made live here:
// floor control, the Tier B salience cap, the Layer 2 reply budget, the Layer 4
// end-vote K/W, the escalation chair, and the interaction idle window. The
// seventh — interaction budget (RFC 0030 Layer 1) — is NOT router-held (there is
// no `Resolve*` boot call and no setter; it is read on demand by
// [ChannelConfig.ResolveInteractionBudgetTokens] on the wallet path). PR 1
// persists its override uniformly in `config_overrides_json`, but applying it
// live needs new plumbing and is deferred (RFC 0050 PR-2 plan, Open item 4); so
// the apply path here makes six of the seven knobs runtime-editable.

// Validate enforces the per-channel field-range invariants on a sparse override
// patch — the single-channel subset of [Config.Validate], applied to the knobs
// an operator can edit at runtime. It mirrors the belt-and-suspenders negative
// rejections in config_validate.go (the cross-field rules the JSON Schema cannot
// express), restated against the pointer/tri-state override fields: a nil knob
// is "inherit" and is never an error; only an explicitly-set out-of-range value
// is rejected. The escalation-chair member rule is cross-field (it needs the
// channel's membership) and so lives in [ChannelRouter.ApplyChannelConfig], not
// here.
//
// Kept aligned with the matching checks in [Config.Validate] by convention —
// the same mirror-with-a-comment discipline config_validate.go keeps against
// `schemas/channel.schema.json`.
func (o ChannelConfigOverrides) Validate() error {
	// Tier B channel-size cap (RFC 0030): negative is a typo; an explicit value
	// is honoured as-is (the router's SetSalienceMaxChannelMembers normalizes a
	// non-positive value to the default, but the operator should not be able to
	// persist a negative).
	if o.SalienceMaxChannelMembers != nil && *o.SalienceMaxChannelMembers < 0 {
		return fmt.Errorf("%w: %d (must be >= 0)",
			ErrInvalidSalienceMaxChannelMembers, *o.SalienceMaxChannelMembers)
	}
	// Layer 1 per-interaction cost ceiling: zero is meaningful (uncapped);
	// negative is rejected.
	if o.InteractionBudgetTokens != nil && *o.InteractionBudgetTokens < 0 {
		return fmt.Errorf("%w: %d (must be >= 0)",
			ErrInvalidInteractionBudgetTokens, *o.InteractionBudgetTokens)
	}
	// Layer 2 per-participant reply budget: zero is meaningful (uncapped);
	// negative is rejected.
	if o.MaxRepliesPerParticipantPerInteraction != nil && *o.MaxRepliesPerParticipantPerInteraction < 0 {
		return fmt.Errorf("%w: %d (must be >= 0)",
			ErrInvalidMaxRepliesPerParticipant, *o.MaxRepliesPerParticipantPerInteraction)
	}
	// Layer 4 end-vote quorum (K) / recency window (W): a zero is not a
	// meaningful value here (the router normalizes it to the K=2/W=3 default),
	// but only a negative is an error — symmetric with the YAML loader.
	if o.EndVoteThreshold != nil && *o.EndVoteThreshold < 0 {
		return fmt.Errorf("%w: %d (must be >= 1)", ErrInvalidEndVoteThreshold, *o.EndVoteThreshold)
	}
	if o.EndVoteWindow != nil && *o.EndVoteWindow < 0 {
		return fmt.Errorf("%w: %d (must be >= 1)", ErrInvalidEndVoteWindow, *o.EndVoteWindow)
	}
	// Interaction idle window (IP3): an explicit 0 is valid (idle rotation off);
	// negative is rejected.
	if o.InteractionIdleTimeoutSeconds != nil && *o.InteractionIdleTimeoutSeconds < 0 {
		return fmt.Errorf("%w: %d (must be >= 0)",
			ErrInvalidInteractionIdleTimeout, *o.InteractionIdleTimeoutSeconds)
	}
	return nil
}

// ApplyChannelConfig is the RFC 0050 single validated apply path: it validates a
// sparse override patch, persists it to the store (bumping the per-channel
// revision under the PR-1 optimistic-concurrency primitive), and stamps the six
// router-held knobs onto the live router so the change takes effect WITHOUT a
// restart.
//
// `patch` is the COMPLETE desired override set for the channel, not a delta:
// [ChannelStore.PutChannelConfig] replaces the `config_overrides_json` blob
// wholesale, so a knob absent from `patch` becomes "inherit" — the merge/`null`-
// means-unset semantics belong to the REST layer (PR 4), above this method.
// Consequently the router is re-seeded across all six knobs from the resulting
// stored state (present → value, absent → inherited default), not just the knobs
// `patch` happened to mention — otherwise the router would drift from the
// canonical store (e.g. a prior `floor_control:false` would linger after a patch
// that omits it).
//
// `expectedRevision` is the revision the caller last read; a stale value yields
// [ErrConfigRevisionConflict] and no write. `lineage` is the (dormant, RFC 0050
// Open Q2) governance id of the mutation; pass "" until it is activated.
//
// Validation runs BEFORE any write: a malformed patch leaves both the store and
// the router untouched.
func (r *ChannelRouter) ApplyChannelConfig(ctx context.Context, channelID string, patch ChannelConfigOverrides, expectedRevision int64, lineage string) error {
	if err := patch.Validate(); err != nil {
		return fmt.Errorf("channels: apply config %s: %w", channelID, err)
	}
	// The escalation chair is a cross-field rule (it names a declared member),
	// so it is validated here against the store's membership rather than in the
	// pure per-field Validate above. Runs before the write, so a bad chair never
	// persists.
	if err := r.validateEscalationChair(ctx, channelID, patch); err != nil {
		return err
	}

	if err := r.store.PutChannelConfig(ctx, channelID, patch, expectedRevision, lineage); err != nil {
		return err
	}

	// Re-read the canonical stored state and seed the router from THAT, not from
	// `patch` directly: PutChannelConfig treats an empty apply on a never-edited
	// channel as a no-op (revision stays 0 so the YAML still seeds it under the
	// revision gate), and in that case the router must be left alone. A revision
	// > 0 means the channel is store-canonical and the router follows it.
	overrides, revision, err := r.store.GetChannelConfig(ctx, channelID)
	if err != nil {
		return fmt.Errorf("channels: apply config %s: re-read: %w", channelID, err)
	}
	if revision > 0 {
		r.applyOverridesToRouter(channelID, overrides)
	}
	return nil
}

// validateEscalationChair enforces the cross-field escalation-chair rules from
// [Config.Validate] against a runtime patch: the chair must be a declared member
// of the channel, must not be an observer (its gate suppresses every turn), and
// floor control must be on for the channel (stall detection runs only at the
// floor round's tail, so a chair under `floor_control:false` would be silently
// inert). The effective floor-control state is read from the patch itself —
// because the patch replaces the whole override blob, a patch that omits
// floor_control resolves to the group default (ON).
//
// A nil or empty chair clears the knob (no escalation) and needs no membership.
func (r *ChannelRouter) validateEscalationChair(ctx context.Context, channelID string, patch ChannelConfigOverrides) error {
	if patch.EscalationChairID == nil || *patch.EscalationChairID == "" {
		return nil
	}
	chairID := *patch.EscalationChairID

	// Floor control must be on in the resulting state. An absent floor_control
	// knob inherits the group default (ON); an explicit false opts out and makes
	// the chair inert.
	if patch.FloorControl != nil && !*patch.FloorControl {
		return fmt.Errorf("channels: apply config %s: %w: %q requires floor control, but the patch sets floor_control:false (stall detection runs only at the floor round's tail)",
			channelID, ErrInvalidEscalationChair, chairID)
	}

	members, err := r.store.GetMembers(ctx, channelID)
	if err != nil {
		return fmt.Errorf("channels: apply config %s: load members: %w", channelID, err)
	}
	var chair *Member
	for i := range members {
		if members[i].ParticipantID == chairID {
			chair = &members[i]
			break
		}
	}
	if chair == nil {
		return fmt.Errorf("channels: apply config %s: %w: %q is not a declared member",
			channelID, ErrInvalidEscalationChair, chairID)
	}
	// An observer (legacy `never`) chair is as guaranteed-futile as a non-member
	// — the receiver gate suppresses it before any LLM — so reject it loudly,
	// mirroring [Config.Validate].
	if chair.RespondPolicy.Normalize() == RespondNever {
		return fmt.Errorf("channels: apply config %s: %w: %q is an observer (respond: never) and can never take the forced turn",
			channelID, ErrInvalidEscalationChair, chairID)
	}
	return nil
}

// applyOverridesToRouter stamps the six router-held knobs for `channelID` onto
// the live router from a (canonical) override set: present → the override value,
// absent → the inherited default. It is the shared seam used by both the runtime
// apply path ([ChannelRouter.ApplyChannelConfig]) and the boot repoint
// ([ChannelRouter.ResolveFromStore]), so a knob resolves identically whether it
// was edited live or replayed at startup.
//
// Each setter already normalizes its "inherit" sentinel to the right default, so
// "absent → inherited default" is expressed in the setter's own vocabulary:
//
//   - floor control: absent → the group default (ON); the per-turn timeout is
//     not part of the override set, so the current resolved timeout is preserved.
//   - salience cap: absent → SetSalienceMaxChannelMembers(_, 0), which the setter
//     normalizes to [DefaultSalienceMaxChannelMembers].
//   - reply budget: absent → ApplyDefaultReplyBudget, which stamps the captured
//     fleet default (zero is a meaningful "uncapped" value, so it cannot inherit
//     via Set(_, 0)).
//   - end-vote K/W: absent → 0/0, which SetEndVoteParams normalizes to the
//     K=2/W=3 defaults.
//   - escalation chair: absent → SetEscalationChair(_, ""), which unsets it (no
//     escalation — the opt-in default).
//   - idle window: absent → SetInteractionIdleTimeout(_, -1), whose negative
//     sentinel deletes the entry so the channel falls back to the fleet default.
//
// Interaction budget is intentionally absent: it is not router-held (RFC 0050
// PR-2 plan, Open item 4), so there is no setter to call here.
func (r *ChannelRouter) applyOverridesToRouter(channelID string, o ChannelConfigOverrides) {
	// Floor control. Preserve the channel's resolved per-turn timeout (it rides
	// a separate YAML knob, not the override set); a non-positive value falls
	// back to the default inside SetFloorControl.
	_, turnTimeout, _ := r.FloorControlFor(channelID)
	enabled := true // group default ON
	if o.FloorControl != nil {
		enabled = *o.FloorControl
	}
	r.SetFloorControl(channelID, enabled, turnTimeout)

	// Tier B salience cap.
	if o.SalienceMaxChannelMembers != nil {
		r.SetSalienceMaxChannelMembers(channelID, *o.SalienceMaxChannelMembers)
	} else {
		r.SetSalienceMaxChannelMembers(channelID, 0) // → DefaultSalienceMaxChannelMembers
	}

	// Layer 2 reply budget.
	if o.MaxRepliesPerParticipantPerInteraction != nil {
		r.SetReplyBudget(channelID, *o.MaxRepliesPerParticipantPerInteraction)
	} else {
		r.ApplyDefaultReplyBudget(channelID) // captured fleet default
	}

	// Layer 4 end-vote quorum / window. A nil knob passes 0, which
	// SetEndVoteParams normalizes to the K=2/W=3 default.
	var k, w int
	if o.EndVoteThreshold != nil {
		k = *o.EndVoteThreshold
	}
	if o.EndVoteWindow != nil {
		w = *o.EndVoteWindow
	}
	r.SetEndVoteParams(channelID, k, w)

	// Escalation chair. A nil/empty knob unsets it (no escalation).
	chair := ""
	if o.EscalationChairID != nil {
		chair = *o.EscalationChairID
	}
	r.SetEscalationChair(channelID, chair)

	// Interaction idle window. A nil knob passes -1, the delete sentinel that
	// drops the channel back to the fleet default.
	if o.InteractionIdleTimeoutSeconds != nil {
		r.SetInteractionIdleTimeout(channelID, *o.InteractionIdleTimeoutSeconds)
	} else {
		r.SetInteractionIdleTimeout(channelID, -1)
	}
}

// ResolveFromStore is the RFC 0050 Phase 1 PR 2 boot repoint: after the per-knob
// YAML resolvers have seeded the router from `config/channels.yaml`, it overlays
// the canonical store overrides for every channel an operator has edited.
//
// It is deliberately conservative about un-edited channels. A channel still at
// `config_revision` 0 has never been edited through the store, so its YAML/default
// seeding stands and the channel is skipped entirely — a fleet that has never
// used the live-edit path boots byte-identically to before this PR ("empty
// overrides → identical to today"). A channel at revision > 0 is store-canonical:
// [ChannelRouter.applyOverridesToRouter] re-stamps all six router-held knobs from
// its persisted overrides, so an un-edited knob on an edited channel falls back
// to the package/fleet default rather than its old YAML value — the
// shadow-the-whole-block semantics the revision gate turns on (and that PR 3's
// reconcile layers a sparse override over the YAML baseline to soften).
//
// Call once at startup after [ChannelRouter.ReconcileConfig] and the per-knob
// resolvers (so the captured fleet defaults the inherit-paths rely on are in
// place). Idempotent. Non-fatal posture is the caller's: a store-enumeration
// failure is returned so the orchestrator can log-and-continue, leaving the
// YAML-seeded maps in place.
func (r *ChannelRouter) ResolveFromStore(ctx context.Context) error {
	channels, err := r.store.ListChannels(ctx, 0, "")
	if err != nil {
		return fmt.Errorf("channels: resolve from store: list channels: %w", err)
	}
	applied := 0
	for _, ch := range channels {
		overrides, revision, err := r.store.GetChannelConfig(ctx, ch.ID)
		if err != nil {
			return fmt.Errorf("channels: resolve from store: get config %s: %w", ch.ID, err)
		}
		if revision == 0 {
			continue // never edited — YAML/default seeding stands.
		}
		r.applyOverridesToRouter(ch.ID, overrides)
		applied++
	}
	if applied > 0 {
		r.logger.Info("channels: store config overlaid onto router",
			zap.Int("edited_channels", applied),
			zap.Int("scanned_channels", len(channels)))
	}
	return nil
}
