package channels

import (
	"errors"
	"fmt"
)

// config_reasoning.go holds the RFC 0051 (v0.3.10) per-channel `reasoning` block
// — "reasoning before posting". Split out of config.go (which is at the 500-line
// review cap) so the new value type, its normalization, and its capability-gated
// validation live with their own siblings, mirroring how reply_budget.go /
// router_salience.go carved their concerns off.
//
// The block generalizes the shipped RFC 0030 Tier B scalar salience bid into a
// structured deliberation: before composing a reply a `participant`/`chair`
// decides WHETHER the turn is worth a post (`mode: bid`, the silence-only rung)
// and, one rung up, WHAT the post should accomplish (`mode: plan`). It rides the
// existing Tier B salience seam — the deliberation runs only on a salience-gated
// channel at an open-floor admit — so `mode != off` on a channel with no
// salience-gated member is rejected rather than left silently inert.
//
// Phases 1–2 shipped the mechanism dark behind an internal `mode` parameter
// (default `off` = byte-for-byte today's score gate). This file is the Phase 3a
// config backend: the operator-editable knob on the RFC 0050 surface. The
// governed-channel default stays `off` here — the flip to `bid` is PR 6, in
// lockstep with the kill switch and telemetry.

// Reasoning mode rungs (RFC 0051 §C). The ladder is a strict superset chain
// `off ⊂ bid ⊂ plan`, so a channel is promoted/demoted one rung at a time.
const (
	// ReasoningModeOff is the kill-switch rung: no deliberation, the byte-for-byte
	// RFC 0030 Tier B scalar score gate (the per-member `threshold` governs here).
	ReasoningModeOff = "off"
	// ReasoningModeBid is the silence-only rung: the persona privately decides
	// `should_post` before the expensive compose, ending an unworthy turn in
	// `DO_NOTHING` (the net pile-on saving). No plan is threaded.
	ReasoningModeBid = "bid"
	// ReasoningModePlan is the full rung: a `should_post=true` turn additionally
	// threads a private CompositionPlan (intent / key points / addressed-to /
	// avoid-restating) into the Tier C compose.
	ReasoningModePlan = "plan"
)

// Reasoning model — which leased model runs the deliberation pass (RFC 0051 §D).
const (
	// ReasoningModelFast is the cheap leased model already in production for the
	// Tier B bid — the default, and the one the net-saving economics assume.
	ReasoningModelFast = "fast"
	// ReasoningModelQuality runs the deliberation on the expensive model. Accepted
	// but warned: it defeats the cheap-pass economics (RFC 0051 §F).
	ReasoningModelQuality = "quality"
)

// Reasoning depth (RFC 0051 §D / Phase 4). Only `shallow` is backed in v0.3.10;
// `deep` (native extended thinking) needs a provider-protocol change and is
// capability-rejected until Phase 4.
const (
	ReasoningDepthShallow = "shallow"
	ReasoningDepthDeep    = "deep"
)

// Reasoning block defaults — the shipped rung. `off` keeps the feature dark until
// the PR 6 flip; `fast`/`shallow`/`revise: 0` are the cheap single-pass baseline.
const (
	DefaultReasoningMode   = ReasoningModeOff
	DefaultReasoningModel  = ReasoningModelFast
	DefaultReasoningDepth  = ReasoningDepthShallow
	DefaultReasoningRevise = 0
)

// Reasoning validation sentinels — defined here rather than in the channels.go
// central block, which is itself at the 500-line cap. Matched by [errors.Is]; the
// REST layer maps each to 400 Bad Request.
var (
	// ErrInvalidReasoningMode — `reasoning.mode` is outside {off, bid, plan}, OR a
	// non-off mode was set on a channel with no salience-gated member (the knob
	// does not by itself arm the gate — RFC 0051 §G).
	ErrInvalidReasoningMode = errors.New("channels: invalid reasoning.mode")
	// ErrInvalidReasoningModel — `reasoning.model` is outside {fast, quality}.
	// (`quality` is accepted-but-warned, not an error.)
	ErrInvalidReasoningModel = errors.New("channels: invalid reasoning.model")
	// ErrInvalidReasoningDepth — `reasoning.depth` is outside {shallow}, OR is the
	// structurally-valid `deep` that Phase 4 has not yet built (capability-gated:
	// rejected loudly rather than silently degraded to shallow).
	ErrInvalidReasoningDepth = errors.New("channels: invalid reasoning.depth")
	// ErrInvalidReasoningRevise — `reasoning.revise` is negative, OR is `>= 1`
	// before Phase 5 (the reflexion loop) is deployed (capability-gated).
	ErrInvalidReasoningRevise = errors.New("channels: invalid reasoning.revise")
)

// ReasoningConfig is the per-channel RFC 0051 reasoning-before-posting block, the
// `reasoning:` mapping in a `config/channels.yaml` channel. A value type (not a
// pointer like floor_control): an absent block is the zero value, normalized to
// the shipped default rung at load ([ReasoningConfig.normalized]). The resolved
// value is stamped onto the router so the REST surface can report it; the
// agent-side seam reads `mode`/`model` (the dispatch wiring rides the go-live).
type ReasoningConfig struct {
	// Mode is the deliberation rung — off / bid / plan (RFC 0051 §C).
	Mode string `yaml:"mode"`
	// Model is the leased model the deliberation pass runs on — fast / quality.
	Model string `yaml:"model"`
	// Depth is the deliberation depth — shallow (deep is Phase 4, capability-gated).
	Depth string `yaml:"depth"`
	// Revise is the reflexion round count — 0 (single pass). `>= 1` is Phase 5,
	// capability-gated until the critic→revise loop is deployed.
	Revise int `yaml:"revise"`
}

// DefaultReasoningConfig is the shipped default rung — the value an un-configured
// channel resolves to (off / fast / shallow / 0).
func DefaultReasoningConfig() ReasoningConfig {
	return ReasoningConfig{
		Mode:   DefaultReasoningMode,
		Model:  DefaultReasoningModel,
		Depth:  DefaultReasoningDepth,
		Revise: DefaultReasoningRevise,
	}
}

// normalized fills any empty string field with its default — so a block that
// declares only `mode: bid` reads back as a complete rung (bid / fast / shallow)
// rather than carrying empty model/depth downstream. `Revise` needs no
// normalization: its zero IS the default (single pass).
func (rc ReasoningConfig) normalized() ReasoningConfig {
	if rc.Mode == "" {
		rc.Mode = DefaultReasoningMode
	}
	if rc.Model == "" {
		rc.Model = DefaultReasoningModel
	}
	if rc.Depth == "" {
		rc.Depth = DefaultReasoningDepth
	}
	return rc
}

// validate enforces the per-field reasoning invariants — the enum vocabulary plus
// the v0.3.10 capability gate (deep / revise≥1 rejected as unbacked). It runs on
// the NORMALIZED value, so an empty field has already been filled and is never
// the rejection cause. `governed` reports whether the channel has a salience-gated
// member; a non-off mode on an ungoverned channel is rejected because the
// deliberation rides the Tier B seam and would otherwise be silently inert.
//
// `model: quality` is accepted (not an error) — it is merely discouraged. The
// discouraged-economics WARNING is not surfaced here (validation has no logger and
// its only caller discards a return); it is logged at the two paths that DO have a
// logger and act on the value: [ChannelRouter.ResolveReasoning] (YAML/boot) and
// [ChannelRouter.ApplyChannelConfig] (runtime PATCH).
func (rc ReasoningConfig) validate(governed bool) error {
	switch rc.Mode {
	case ReasoningModeOff, ReasoningModeBid, ReasoningModePlan:
	default:
		return fmt.Errorf("%w: %q (must be one of off, bid, plan)", ErrInvalidReasoningMode, rc.Mode)
	}
	if rc.Mode != ReasoningModeOff && !governed {
		return fmt.Errorf("%w: %q requires a salience-gated (open-floor participant/chair) member; the knob does not by itself arm the gate",
			ErrInvalidReasoningMode, rc.Mode)
	}

	switch rc.Model {
	case ReasoningModelFast, ReasoningModelQuality: // quality accepted-but-discouraged; warned by the caller paths
	default:
		return fmt.Errorf("%w: %q (must be one of fast, quality)", ErrInvalidReasoningModel, rc.Model)
	}

	switch rc.Depth {
	case ReasoningDepthShallow:
	case ReasoningDepthDeep:
		return fmt.Errorf("%w: %q is not yet deployed (native extended thinking is RFC 0051 Phase 4)", ErrInvalidReasoningDepth, rc.Depth)
	default:
		return fmt.Errorf("%w: %q (must be shallow)", ErrInvalidReasoningDepth, rc.Depth)
	}

	if rc.Revise < 0 {
		return fmt.Errorf("%w: %d (must be >= 0)", ErrInvalidReasoningRevise, rc.Revise)
	}
	if rc.Revise >= 1 {
		return fmt.Errorf("%w: %d is not yet deployed (the reflexion loop is RFC 0051 Phase 5)", ErrInvalidReasoningRevise, rc.Revise)
	}
	return nil
}

// FreezeOverrides snapshots this RESOLVED rung into a sparse override for the
// config-freeze paths — the YAML reconcile snapshot ([ChannelConfig.toConfigOverrides])
// and the REST first-edit baseline ([Server.resolvedConfigBaseline]). It is the
// single source of truth for "which sub-knobs of a resolved rung are worth
// committing".
//
// The freeze is PER-SUB-KNOB, not whole-rung: only a sub-knob that differs from
// its package default is captured; a default sub-knob stays inherit (nil). Two
// consequences this shape exists for:
//
//   - `mode: off` (the default) is never frozen as an explicit override, even when
//     a SIBLING sub-knob is non-default (e.g. `model: quality`). An explicit
//     `mode: off` would silently opt the channel out of the RFC 0051 PR 6 default
//     flip (off→bid) — a flip an operator who only touched `model` never declined.
//     Leaving `mode` inherit keeps the channel responsive to the future default.
//   - a committed non-off `mode` (or a non-default model/depth/revise) IS captured,
//     so the reconcile→[ChannelRouter.ResolveFromStore] boot round-trip and the
//     first-edit baseline both preserve it instead of resetting it to the package
//     default. The drift hash ([channelConfigContentHash]) likewise sees the change.
//
// Returns nil for a fully-default rung (every sub-knob inherit) so a never-
// deliberated channel snapshots identically to never-set (no `reasoning` key in
// the blob), exactly like the escalation chair's absent-stays-nil treatment.
func (rc ReasoningConfig) FreezeOverrides() *ReasoningOverrides {
	var ov ReasoningOverrides
	if rc.Mode != DefaultReasoningMode {
		mode := rc.Mode
		ov.Mode = &mode
	}
	if rc.Model != DefaultReasoningModel {
		model := rc.Model
		ov.Model = &model
	}
	if rc.Depth != DefaultReasoningDepth {
		depth := rc.Depth
		ov.Depth = &depth
	}
	if rc.Revise != DefaultReasoningRevise {
		revise := rc.Revise
		ov.Revise = &revise
	}
	if ov.IsEmpty() {
		return nil
	}
	return &ov
}

// ReasoningOverrides is the sparse, tri-state-aware runtime override of the
// reasoning block, persisted nested under `reasoning` in the
// `channels.config_overrides_json` blob (RFC 0050). Each field is a pointer: nil
// inherits (the sub-knob is absent from the blob), a set value overrides. It is
// the reasoning sub-shape of [ChannelConfigOverrides] — the first nested knob on
// that surface.
type ReasoningOverrides struct {
	Mode   *string `json:"mode,omitempty"`
	Model  *string `json:"model,omitempty"`
	Depth  *string `json:"depth,omitempty"`
	Revise *int    `json:"revise,omitempty"`
}

// IsEmpty reports whether no reasoning sub-knob is set — the inherit-all state.
// Equivalent to the zero value because every field is a comparable pointer. Used
// by the REST merge to collapse an all-cleared reasoning block back to nil (absent
// from the blob) rather than persist a literal `{}`.
func (o ReasoningOverrides) IsEmpty() bool {
	return o == ReasoningOverrides{}
}

// validate runs the per-field reasoning invariants against the set sub-knobs. It
// is the override-path mirror of [ReasoningConfig.validate] — the REST PATCH path
// runs this, not the JSON schema — but it is per-field (a nil sub-knob is
// "inherit" and never checked) and it does NOT take `governed`: the mode↔governance
// cross-field rule needs the channel's membership and so lives in
// [ChannelRouter.validateReasoningGoverned], alongside the escalation-chair rule.
func (o ReasoningOverrides) validate() error {
	if o.Mode != nil {
		switch *o.Mode {
		case ReasoningModeOff, ReasoningModeBid, ReasoningModePlan:
		default:
			return fmt.Errorf("%w: %q (must be one of off, bid, plan)", ErrInvalidReasoningMode, *o.Mode)
		}
	}
	if o.Model != nil {
		switch *o.Model {
		case ReasoningModelFast, ReasoningModelQuality:
		default:
			return fmt.Errorf("%w: %q (must be one of fast, quality)", ErrInvalidReasoningModel, *o.Model)
		}
	}
	if o.Depth != nil {
		switch *o.Depth {
		case ReasoningDepthShallow:
		case ReasoningDepthDeep:
			return fmt.Errorf("%w: %q is not yet deployed (native extended thinking is RFC 0051 Phase 4)", ErrInvalidReasoningDepth, *o.Depth)
		default:
			return fmt.Errorf("%w: %q (must be shallow)", ErrInvalidReasoningDepth, *o.Depth)
		}
	}
	if o.Revise != nil {
		if *o.Revise < 0 {
			return fmt.Errorf("%w: %d (must be >= 0)", ErrInvalidReasoningRevise, *o.Revise)
		}
		if *o.Revise >= 1 {
			return fmt.Errorf("%w: %d is not yet deployed (the reflexion loop is RFC 0051 Phase 5)", ErrInvalidReasoningRevise, *o.Revise)
		}
	}
	return nil
}

// resolve overlays the set sub-knobs onto a base config (normally the package
// default) — the inherit-or-override resolution the apply path stamps onto the
// router. A nil sub-knob leaves the base value; a set one wins.
func (o *ReasoningOverrides) resolve(base ReasoningConfig) ReasoningConfig {
	if o == nil {
		return base
	}
	if o.Mode != nil {
		base.Mode = *o.Mode
	}
	if o.Model != nil {
		base.Model = *o.Model
	}
	if o.Depth != nil {
		base.Depth = *o.Depth
	}
	if o.Revise != nil {
		base.Revise = *o.Revise
	}
	return base
}

// effectiveMode reports the mode this override resolves to — the set `mode` or, if
// absent, the inherited default (`off`). Used by the cross-field governance check
// so an override that does not touch `mode` never trips the rule.
func (o *ReasoningOverrides) effectiveMode() string {
	if o != nil && o.Mode != nil {
		return *o.Mode
	}
	return DefaultReasoningMode
}
