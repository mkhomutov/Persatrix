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
// All eight governance knobs are router-held and made live here: floor control,
// the Tier B salience cap, the Layer 1 interaction budget, the Layer 2 reply
// budget, the Layer 4 end-vote K/W, the escalation chair, the interaction idle
// window, and the RFC 0051 reasoning block. The interaction budget (RFC 0030
// Layer 1) became router-held in
// the RFC 0050 amendment (interaction-budget enforcement): it now has a
// [ChannelRouter.ResolveInteractionBudgets] boot call and a
// [ChannelRouter.SetInteractionBudgetTokens] setter, so the apply path here
// stamps it like the others. Note enforcement of the resolved ceiling is still
// the amendment's PR 2 (wallet-side resolution); this PR resolves and surfaces
// the value but does not yet act on it.

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
	// Tier B channel-size cap (RFC 0030): reject anything below 1, mirroring the
	// loader's schema `minimum: 1` (config_validate.go). Unlike the reply/idle
	// budgets, zero is NOT a meaningful value here: SetSalienceMaxChannelMembers
	// coerces any non-positive cap to [DefaultSalienceMaxChannelMembers], so a
	// persisted explicit 0 reads back as a non-nil knob that nonetheless applies
	// the default — behaviourally indistinguishable from nil (inherit) and a
	// value the operator did not intend. The loader never sees a YAML 0 (LoadConfig
	// normalizes it before Validate); the override path has no such pre-pass, so
	// the rejection has to happen here or the apply path would persist a value it
	// silently swallows.
	if o.SalienceMaxChannelMembers != nil && *o.SalienceMaxChannelMembers < 1 {
		return fmt.Errorf("%w: %d (must be >= 1)",
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
	// RFC 0051 reasoning block: per-field enum + capability gate (deep / revise≥1
	// rejected as unbacked). The mode↔governance cross-field rule needs the
	// channel's membership and so lives in [ChannelRouter.validateReasoningGoverned]
	// (alongside the escalation-chair rule), not in this pure per-field Validate.
	if o.Reasoning != nil {
		if err := o.Reasoning.validate(); err != nil {
			return err
		}
	}
	// RFC 0052 autonomous block: per-field ranges + the cross-field rules
	// computable without the live roster (the mandatory cost cap, and the convener
	// being non-empty + distinct from the chair). The convener-IS-a-member rule
	// needs the store and lives in [ChannelRouter.validateAutonomousConvener],
	// alongside the escalation-chair membership rule.
	if err := o.validateAutonomous(); err != nil {
		return err
	}
	return nil
}

// ApplyChannelConfig is the RFC 0050 single validated apply path: it validates a
// sparse override patch, persists it to the store (bumping the per-channel
// revision under the PR-1 optimistic-concurrency primitive), and stamps the eight
// router-held knobs onto the live router so the change takes effect WITHOUT a
// restart.
//
// `patch` is the COMPLETE desired override set for the channel, not a delta:
// [ChannelStore.PutChannelConfig] replaces the `config_overrides_json` blob
// wholesale, so a knob absent from `patch` becomes "inherit" — the merge/`null`-
// means-unset semantics belong to the REST layer (PR 4), above this method.
// Consequently the router is re-seeded across all eight knobs from the resulting
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
	// RFC 0051: a non-off reasoning mode is also a cross-field rule (it needs a
	// salience-gated member), validated here against the store's membership.
	if err := r.validateReasoningGoverned(ctx, channelID, patch); err != nil {
		return err
	}
	// RFC 0052: an armed autonomous channel must be a group channel — a cross-field
	// rule (it needs the channel's type), validated here before the write so a
	// non-group arm never persists (autonomous convening is open-floor-group only).
	if err := r.validateAutonomousChannelType(ctx, channelID, patch); err != nil {
		return err
	}
	// RFC 0052: an armed autonomous channel's convener must be a declared member and
	// not an observer — a cross-field rule (it needs the store's live roster),
	// validated here before the write so a bad convener never persists.
	if err := r.validateAutonomousConvener(ctx, channelID, patch); err != nil {
		return err
	}
	// Accepted-but-discouraged: warn (do not reject) on a quality deliberation
	// model — it defeats the cheap-pass economics (RFC 0051 §F). Logged on the
	// runtime edit path only; the value still applies.
	if patch.Reasoning != nil && patch.Reasoning.Model != nil && *patch.Reasoning.Model == ReasoningModelQuality {
		r.logger.Warn("channels: reasoning.model=quality defeats the cheap-pass economics (RFC 0051 §F); prefer fast",
			zap.String("channel_id", channelID))
	}

	// Serialize the persist → re-read → stamp sequence so it is atomic as a
	// whole. The store CAS in PutChannelConfig already serializes the write, but
	// the re-read-and-stamp below is in-memory and would otherwise race: two
	// concurrent applies could interleave such that a slow apply stamps its
	// now-stale snapshot AFTER a newer apply committed and stamped, leaving the
	// live router on a superseded override that disagrees with the canonical
	// store until restart. Holding applyMu across all three steps makes the last
	// committed override also the last stamped. (See the field's doc in router.go.)
	r.applyMu.Lock()
	defer r.applyMu.Unlock()

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
		r.applyOverridesToRouter(channelID, overrides, r.channelGoverned(ctx, channelID))
	}
	return nil
}

// AnySalienceGated reports whether a resolved roster holds at least one
// salience-gated (RFC 0030 Tier B open-floor participant/chair) member — the
// single "is this channel governed?" predicate over the runtime [Member] shape.
// All three store-read callers fold into it, each keeping its own GetMembers +
// error posture (which differs by caller): [ChannelRouter.channelGoverned] and the
// server's channelHasSalienceGatedMember fail a read to "not governed", while
// [ChannelRouter.validateReasoningGoverned] fails it to an error. The load-time
// counterpart over the YAML [MemberConfig] shape is [ChannelConfig.governed]; both
// only read the per-member SalienceGated bool already resolved at unmarshal
// ([ResolveSalienceSignal]), so the governance *definition* stays single-sourced
// there and only the loop is shared here.
func AnySalienceGated(members []Member) bool {
	for i := range members {
		if members[i].SalienceGated {
			return true
		}
	}
	return false
}

// channelGoverned reports whether `channelID` currently has at least one
// salience-gated (open-floor participant/chair) member — the live-membership
// signal the RFC 0051 reasoning resolution needs to pick the governed default
// ([governedReasoningBase]). The router-side counterpart to the load-time
// [ChannelConfig.governed] and the server's [Server.channelHasSalienceGatedMember];
// the "any gated member?" test is shared via [AnySalienceGated]. Posture: a store
// error reading members resolves to "not governed" so the resolve falls back to the
// package `off` default (the conservative, no-op direction) rather than failing the
// apply/boot path on a transient fault.
func (r *ChannelRouter) channelGoverned(ctx context.Context, channelID string) bool {
	members, err := r.store.GetMembers(ctx, channelID)
	if err != nil {
		return false
	}
	return AnySalienceGated(members)
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
// The checks run in the SAME order as [Config.Validate] — membership → observer
// → floor-control — so the two enforcement points surface the same diagnosis for
// a patch that fails more than one rule (e.g. a non-member chair set alongside
// floor_control:false is reported as the more fundamental "not a declared
// member", not "requires floor control"). The doc claims to mirror the loader;
// the order is part of that mirror.
//
// A nil or empty chair clears the knob (no escalation) and needs no membership.
func (r *ChannelRouter) validateEscalationChair(ctx context.Context, channelID string, patch ChannelConfigOverrides) error {
	if patch.EscalationChairID == nil || *patch.EscalationChairID == "" {
		return nil
	}
	chairID := *patch.EscalationChairID

	members, err := r.store.GetMembers(ctx, channelID)
	if err != nil {
		return fmt.Errorf("channels: apply config %s: load members: %w", channelID, err)
	}
	chair := memberByID(members, chairID)
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
	// Floor control must be on in the resulting state. An absent floor_control
	// knob inherits the group default (ON); an explicit false opts out and makes
	// the chair inert.
	if patch.FloorControl != nil && !*patch.FloorControl {
		return fmt.Errorf("channels: apply config %s: %w: %q requires floor control, but the patch sets floor_control:false (stall detection runs only at the floor round's tail)",
			channelID, ErrInvalidEscalationChair, chairID)
	}
	return nil
}

// validateReasoningGoverned enforces the RFC 0051 §G cross-field rule against a
// runtime patch: a non-off `reasoning.mode` requires the channel to have at least
// one salience-gated member, because the deliberation rides the RFC 0030 Tier B
// salience seam and is silently inert otherwise. It mirrors the loader's
// channel-level governed check ([Config.Validate]) against the store's live
// membership, and runs before the write so a bad mode never persists.
//
// An override that does not set `mode` (or sets it to `off`) resolves to the
// default `off` and needs no membership — so an unrelated first edit on an
// ungoverned channel is never blocked by a mode the operator did not touch.
//
// It ALSO enforces the RFC 0051 Phase 5 cross-field rule that `reasoning.revise
// >= 1` requires `mode: plan` (the reflexion critic re-reads the draft against
// the plan). That check needs the MERGED effective mode — a `revise` PATCH may
// not touch `mode` — which is why it lives here rather than in the per-field
// [ReasoningOverrides.validate]. `plan` is never a default, so an effective plan
// mode can only come from an explicit override, making this a clean reject of an
// operator who set `revise` without promoting the rung to `plan`.
//
// NOTE: `patch` here is the COMPLETE merged override set, not a sparse edit —
// [Server.handlePatchChannelConfig] folds a sparse PATCH onto the channel's
// stored/resolved overrides (via mergeConfigPatch) before calling
// [ChannelRouter.ApplyChannelConfig], and the loader passes the full struct. So
// `patch.Reasoning.effectiveMode()`/`effectiveRevise()` ARE the merged effective
// values this cross-field rule needs — a separate `{revise}` PATCH onto an
// already-`plan` channel carries the inherited `plan` here and is correctly accepted.
func (r *ChannelRouter) validateReasoningGoverned(ctx context.Context, channelID string, patch ChannelConfigOverrides) error {
	mode := patch.Reasoning.effectiveMode()
	if patch.Reasoning.effectiveRevise() >= 1 && mode != ReasoningModePlan {
		return fmt.Errorf("channels: apply config %s: %w: requires mode: plan (the reflexion critic re-reads the draft against the plan)",
			channelID, ErrInvalidReasoningRevise)
	}
	if mode == ReasoningModeOff {
		return nil
	}
	members, err := r.store.GetMembers(ctx, channelID)
	if err != nil {
		return fmt.Errorf("channels: apply config %s: load members: %w", channelID, err)
	}
	if AnySalienceGated(members) {
		return nil
	}
	return fmt.Errorf("channels: apply config %s: %w: %q requires a salience-gated (open-floor participant/chair) member; the knob does not by itself arm the gate",
		channelID, ErrInvalidReasoningMode, mode)
}

// applyOverridesToRouter stamps the eight router-held knobs for `channelID` onto
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
//   - interaction budget: absent → ApplyDefaultInteractionBudget, which stamps
//     the captured fleet default (zero is a meaningful "uncapped" value, like the
//     reply budget, so it cannot inherit via Set(_, 0)).
//   - reasoning block: absent → SetReasoning of the GOVERNED default rung — `bid`
//     on a governed channel (the PR 6 go-live flip), `off` otherwise; a present
//     override overlays its set sub-knobs (including an explicit `mode: off` kill
//     switch) onto that base via [ReasoningOverrides.resolve]. `governed` is the
//     caller's live-membership read ([ChannelRouter.channelGoverned]).
func (r *ChannelRouter) applyOverridesToRouter(channelID string, o ChannelConfigOverrides, governed bool) {
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

	// Layer 1 interaction budget. Like the reply budget, zero is a meaningful
	// "uncapped" value, so an absent knob inherits the captured fleet default via
	// ApplyDefaultInteractionBudget rather than a Set(_, 0) sentinel.
	if o.InteractionBudgetTokens != nil {
		r.SetInteractionBudgetTokens(channelID, *o.InteractionBudgetTokens)
	} else {
		r.ApplyDefaultInteractionBudget(channelID)
	}

	// RFC 0051 reasoning block. Absent → the GOVERNED default rung (`bid` when the
	// channel has a salience-gated member, else `off`); a present override overlays
	// its set sub-knobs onto that base. So an inherit (no mode) governed channel
	// resolves to `bid` (the PR 6 flip), an explicit `mode: off` override overlays
	// back to the kill switch, and an ungoverned channel stays `off`. SetReasoning
	// normalizes any empty field, so a sparse override (`mode: bid` only) resolves
	// to a complete rung.
	r.SetReasoning(channelID, o.Reasoning.resolve(governedReasoningBase(governed)))

	// RFC 0052 autonomous block. Absent → the disabled default; a present override
	// overlays its set sub-knobs onto that base via [AutonomousOverrides.resolve].
	// SetAutonomous normalizes any zero max_rounds, so a sparse override (e.g.
	// `enabled: true` only) resolves to a complete rung. Governance-independent
	// (unlike reasoning) — autonomy has no membership-derived default.
	r.SetAutonomous(channelID, o.Autonomous.resolve(DefaultAutonomousConfig()))
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
// [ChannelRouter.applyOverridesToRouter] re-stamps all eight router-held knobs from
// its persisted overrides, so an un-edited knob on an edited channel falls back
// to the package/fleet default rather than its old YAML value — the
// shadow-the-whole-block semantics the revision gate turns on (and that PR 3's
// reconcile layers a sparse override over the YAML baseline to soften).
//
// Boot replay TRUSTS the last-validated state: it re-stamps the persisted
// overrides as-is and does NOT re-run the cross-field invariants
// [ChannelRouter.ApplyChannelConfig] enforced at write time (escalation-chair
// membership, floor-control-on). Those rules need the channel's live membership,
// which can have drifted since the apply (e.g. a chair who has since left).
// Re-validating here is unnecessary: post-apply drift is already absorbed at the
// only seam that consumes the knob — [ChannelRouter.maybeEscalateStall] skips a
// non-member chair at dispatch time (logging "escalation chair is not a member;
// stall stands") — and surfacing/reconciling such drift at boot is PR 3's job,
// not this overlay's. Re-validating would only mask the drift PR 3 means to
// detect.
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
		r.applyOverridesToRouter(ch.ID, overrides, r.channelGoverned(ctx, ch.ID))
		applied++
	}
	if applied > 0 {
		r.logger.Info("channels: store config overlaid onto router",
			zap.Int("edited_channels", applied),
			zap.Int("scanned_channels", len(channels)))
	}
	return nil
}
