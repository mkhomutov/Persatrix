package channels

import (
	"fmt"
)

// config_override_validate.go holds [ChannelConfigOverrides.Validate] — the
// per-field range invariants on a sparse RFC 0050 override patch. Split out of
// config_apply.go when the ISSUE-0114 per-channel cascade-depth knob (v0.3.13)
// pushed that file past the 500-line review cap (the same carve config.go made
// with config_validate.go): the apply/boot orchestration stays in
// config_apply.go, the pure per-field validation lives here, and the
// cross-field rules that need the store's live state remain method-adjacent to
// the apply path ([ChannelRouter.validateEscalationChair] and siblings).

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
	// ISSUE-0114 per-channel Layer 0 cascade-depth cap: reject anything below 1,
	// the salience-cap posture — SetChannelMaxCascadeDepth treats a non-positive
	// value as the inherit sentinel (it deletes the entry), so a persisted
	// explicit 0 would read back as a non-nil knob that behaves exactly like nil.
	// The option (c) above-fleet rule is deliberately NOT rejected here: on the
	// live edit path it is a loud setter warning instead
	// ([ChannelRouter.SetChannelMaxCascadeDepth] — the fleet cap is startup-only,
	// so a reject would force a restart into a live edit loop), while
	// config-as-code enforces it at load ([Config.Validate]).
	if o.MaxCascadeDepth != nil && *o.MaxCascadeDepth < 1 {
		return fmt.Errorf("%w: %d (must be >= 1)",
			ErrInvalidMaxCascadeDepth, *o.MaxCascadeDepth)
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
