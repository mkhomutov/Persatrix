package channels

import (
	"errors"
	"fmt"
	"strings"
)

// config_autonomous.go holds the RFC 0052 (v0.3.11) per-channel `autonomous` block
// — "conversations that run themselves". Split out of config.go (at the 500-line
// review cap) so the new value type, its normalization, and its safety-critical
// validation live with their own siblings, mirroring how config_reasoning.go
// carved off the RFC 0051 block.
//
// An autonomous channel convenes a roster of personas on a topic and runs a
// human-free discussion that converges, terminates, and synthesizes. PR 1 lands
// the config BACKEND only — the block is parsed, validated, persisted, and
// surfaced over the RFC 0050 REST layer, but NOTHING consults it at runtime yet
// (no convene path until PR 3). So the block is inert ("dark") this PR; its one
// load-bearing effect is the MANDATORY-cost-cap safety gate below.
//
// The genuinely new safety invariant ([RFC 0052 §Security](../../docs/rfcs/0052-autonomous-agent-channels.md)):
// an unattended channel has no human circuit-breaker, so `validate` REJECTS an
// `autonomous.enabled` channel that has no enforced per-interaction cost cap. The
// cap is not a new field — it reuses the shipped RFC 0030 Layer 1 / RFC 0050
// `interaction_budget_tokens`, already fail-closed in the wallet — so autonomy
// rides the existing enforcement seam rather than inventing a parallel one. The
// gate is on the RESOLVED budget (per-channel value, else the fleet default), so
// an operator who caps the fleet may arm autonomy without a per-channel value.

// Autonomous block defaults (conservative — RFC 0052 OQ #5; a calibration
// tracked-issue tunes these after a soak on real rosters).
const (
	// DefaultAutonomousEnabled is the package default: autonomy is OPT-IN, so an
	// absent block (and every existing channel) is disabled and byte-for-byte
	// unchanged.
	DefaultAutonomousEnabled = false
	// DefaultAutonomousMaxRounds is the hard floor-round bound an absent/zero
	// `max_rounds` fills to — a second independent terminator alongside the cost
	// cap ([RFC 0052 §D](../../docs/rfcs/0052-autonomous-agent-channels.md)).
	DefaultAutonomousMaxRounds = 12
)

// MaxAutonomousAgendaItems caps the agenda length. It bounds the per-agenda-item
// escalation ration the convener gets in PR 6 (one escalation per item → total
// convener turns are agenda-length-bounded), so a pathological agenda cannot
// become an unbounded turn budget. Generous; only a typo-scale list trips it.
const MaxAutonomousAgendaItems = 64

// Autonomous validation sentinels — defined here rather than in the channels.go
// central block (itself at the 500-line cap). Matched by [errors.Is]; the REST
// layer maps each to 400 Bad Request.
var (
	// ErrAutonomousCapRequired — `autonomous.enabled` on a channel whose resolved
	// `interaction_budget_tokens` is not positive (uncapped). The first line of the
	// no-runaway defense ([RFC 0052 §Security](../../docs/rfcs/0052-autonomous-agent-channels.md)):
	// uncapped autonomy is un-creatable.
	ErrAutonomousCapRequired = errors.New("channels: autonomous.enabled requires a positive interaction_budget_tokens cap")
	// ErrInvalidAutonomousConvener — `autonomous.convener` is empty, names a
	// non-member, names an `observer` (respond: never) member, or collides with
	// `escalation_chair_id`. The convener owns the agenda lifecycle and is a DISTINCT
	// role from the chair (RFC 0052 OQ #1); it authors the opening turn, so — exactly
	// like the escalation chair — an observer (whose receiver gate suppresses it
	// before any LLM) can never fill the role.
	ErrInvalidAutonomousConvener = errors.New("channels: invalid autonomous.convener")
	// ErrInvalidAutonomousMaxRounds — `autonomous.max_rounds` is negative (zero
	// normalizes to the default before validate runs).
	ErrInvalidAutonomousMaxRounds = errors.New("channels: invalid autonomous.max_rounds")
	// ErrAutonomousChairRequired — `autonomous.enabled` on a channel that declares no
	// `escalation_chair_id`. RFC 0052 §D "always produce an artifact" makes the chair
	// load-bearing: it authors the mandatory synthesis turn the bounded close draws
	// from the synthesis reserve (PR 4). PR 1 validated only `convener != chair`, which
	// is vacuous when no chair exists; PR 4 closes that gap — an armed channel must
	// declare the role that synthesizes on close. The REST layer maps it to 400.
	ErrAutonomousChairRequired = errors.New("channels: autonomous.enabled requires an escalation_chair_id to author the synthesis turn on close")
	// ErrInvalidAutonomousAgenda — the agenda is longer than [MaxAutonomousAgendaItems]
	// or carries a blank item.
	ErrInvalidAutonomousAgenda = errors.New("channels: invalid autonomous.agenda")
	// ErrAutonomousNotGroup — `autonomous.enabled` on a non-group (DM/thread)
	// channel. Autonomous convening is an open-floor GROUP concept:
	// [ChannelRouter.ResolveAutonomous] seeds only group channels and the convene
	// path (PR 3) dispatches an open-floor seed turn, so arming a DM/thread would
	// create a channel the convene path can never act on. Rejected at the apply path
	// (the load path declares only group channels). The REST layer maps it to 400.
	ErrAutonomousNotGroup = errors.New("channels: autonomous.enabled requires a group channel")
)

// validateConvenerMembership enforces the load-path OQ #1 convener rules for an
// ARMED channel: the convener is non-empty, distinct from the escalation chair
// (the convener owns the agenda lifecycle, a separate role from the chair's
// shipped close role), a declared member of the channel, and not an `observer`
// (respond: never). It authors the opening turn, so — exactly like the escalation
// chair ([Config.Validate]) — a non-member is a guaranteed dispatch failure and an
// observer is suppressed by the receiver gate before any LLM; both are rejected
// loudly at load. The override path's mirror is split across
// [ChannelConfigOverrides.validateAutonomous] (non-empty + chair-distinct) and
// [ChannelRouter.validateAutonomousConvener] (member + observer, which need the
// live store).
func validateConvenerMembership(ch ChannelConfig) error {
	if ch.Autonomous.Convener == "" {
		return fmt.Errorf("%w: an autonomous channel needs a convener to author the opening turn", ErrInvalidAutonomousConvener)
	}
	if ch.Autonomous.Convener == ch.EscalationChairID {
		return fmt.Errorf("%w: %q is also the escalation_chair_id; the convener owns the agenda lifecycle and is a distinct role from the chair (RFC 0052 OQ #1)",
			ErrInvalidAutonomousConvener, ch.Autonomous.Convener)
	}
	for j := range ch.Members {
		if ch.Members[j].ID == ch.Autonomous.Convener {
			// An observer (legacy `never`) convener is as guaranteed-futile as a
			// non-member — the receiver gate suppresses it before any LLM — so reject
			// it loudly, mirroring the escalation-chair observer rule.
			if ch.Members[j].RespondPolicy.Normalize() == RespondNever {
				return fmt.Errorf("%w: %q is an observer (respond: never) and can never author the opening turn",
					ErrInvalidAutonomousConvener, ch.Autonomous.Convener)
			}
			return nil
		}
	}
	return fmt.Errorf("%w: %q is not a declared member; the convener authors the opening turn",
		ErrInvalidAutonomousConvener, ch.Autonomous.Convener)
}

// AutonomousConfig is the per-channel RFC 0052 autonomous-discussion block, the
// `autonomous:` mapping in a `config/channels.yaml` channel. A value type (not a
// pointer): an absent block is the zero value, normalized to the disabled default
// at load ([AutonomousConfig.normalized]). The resolved value is stamped onto the
// router so the REST surface can report it; the convene path (PR 3) reads
// `topic`/`agenda`/`convener`/`goal` from here.
type AutonomousConfig struct {
	// Enabled arms the channel for human-free convening. Opt-in: false (the zero
	// value) is an ordinary channel, untouched by the realism-arc counter-pressure.
	Enabled bool `yaml:"enabled"`
	// Topic is the free-text subject the convener opens the discussion on
	// (RFC 0052 OQ #3). Wrapped as RFC 0009 `<external_data>` before injection (PR 3).
	Topic string `yaml:"topic"`
	// Agenda is the optional ordered list of sub-topics the convener advances
	// through on a stall (PR 6). Empty = a single-topic discussion.
	Agenda []string `yaml:"agenda"`
	// Convener is the agent id of the persona that authors the opening turn and
	// advances the agenda. A declared roster member, DISTINCT from the escalation
	// chair (RFC 0052 OQ #1). Same id vocabulary as `escalation_chair_id`.
	Convener string `yaml:"convener"`
	// Goal is the free-text outcome the chair's synthesis turn aims at on close
	// (PR 4). Empty = a generic synthesis.
	Goal string `yaml:"goal"`
	// MaxRounds is the hard floor-round bound — a second independent terminator
	// alongside the cost cap. Zero/absent fills [DefaultAutonomousMaxRounds] at
	// load; negative is rejected.
	MaxRounds int `yaml:"max_rounds"`
}

// DefaultAutonomousConfig is the shipped default rung — the value an un-configured
// channel resolves to (disabled, the default round bound).
func DefaultAutonomousConfig() AutonomousConfig {
	return AutonomousConfig{
		Enabled:   DefaultAutonomousEnabled,
		MaxRounds: DefaultAutonomousMaxRounds,
	}
}

// normalized fills the zero `max_rounds` with its default so a block that declares
// only `enabled: true` reads back as a complete rung. The other fields are
// free-text/opt-in, whose zero value IS the default (disabled / no topic / single
// item).
func (a AutonomousConfig) normalized() AutonomousConfig {
	if a.MaxRounds == 0 {
		a.MaxRounds = DefaultAutonomousMaxRounds
	}
	return a
}

// validateFields enforces the per-field range invariants the JSON Schema cannot
// fully express (the agenda blank-item check). Runs on the NORMALIZED value, so a
// zero `max_rounds` has already been filled and is never the rejection cause.
// Field-range only: the cross-field cap / convener rules need the channel's
// resolved budget + live roster and live in [Config.Validate] (load) and
// [ChannelRouter.validateAutonomousConvener] (apply).
func (a AutonomousConfig) validateFields() error {
	if a.MaxRounds < 0 {
		return fmt.Errorf("%w: %d (must be >= 0)", ErrInvalidAutonomousMaxRounds, a.MaxRounds)
	}
	if len(a.Agenda) > MaxAutonomousAgendaItems {
		return fmt.Errorf("%w: %d items (max %d)", ErrInvalidAutonomousAgenda, len(a.Agenda), MaxAutonomousAgendaItems)
	}
	for i, item := range a.Agenda {
		// Trim before the blank check: a whitespace-only item is blank in spirit and
		// slips past both the schema's `minLength: 1` and a bare `== ""` test, yet
		// reaches the convener prompt (PR 3) as an empty agenda entry.
		if strings.TrimSpace(item) == "" {
			return fmt.Errorf("%w: item %d is blank", ErrInvalidAutonomousAgenda, i)
		}
	}
	return nil
}

// FreezeOverrides snapshots this RESOLVED rung into a sparse override for BOTH
// config-freeze paths: the RFC 0050 first-edit baseline ([Server.autonomousBaseline])
// and the YAML reconcile snapshot ([ChannelConfig.toConfigOverrides]). CONDITIONAL
// like the escalation chair, not unconditional like the flat knobs: only a
// non-default sub-knob is captured, and a fully-default (disabled) rung returns nil
// so a never-autonomous channel snapshots identically to never-set (no `autonomous`
// key in the blob). A frozen `enabled:true` is what preserves an armed channel both
// across an unrelated first edit (baseline) and across the reconcile→ResolveFromStore
// boot round-trip (snapshot) — omit it from either path and a revisioned armed
// channel silently reads back disabled.
func (a AutonomousConfig) FreezeOverrides() *AutonomousOverrides {
	var ov AutonomousOverrides
	if a.Enabled != DefaultAutonomousEnabled {
		enabled := a.Enabled
		ov.Enabled = &enabled
	}
	if a.Topic != "" {
		topic := a.Topic
		ov.Topic = &topic
	}
	if len(a.Agenda) > 0 {
		agenda := append([]string(nil), a.Agenda...)
		ov.Agenda = &agenda
	}
	if a.Convener != "" {
		convener := a.Convener
		ov.Convener = &convener
	}
	if a.Goal != "" {
		goal := a.Goal
		ov.Goal = &goal
	}
	if a.MaxRounds != DefaultAutonomousMaxRounds {
		rounds := a.MaxRounds
		ov.MaxRounds = &rounds
	}
	if ov.IsEmpty() {
		return nil
	}
	return &ov
}

// AutonomousOverrides is the sparse, tri-state-aware runtime override of the
// autonomous block, persisted nested under `autonomous` in the
// `channels.config_overrides_json` blob (RFC 0050). Each field is a pointer: nil
// inherits (the sub-knob is absent from the blob), a set value overrides. The
// autonomous sub-shape of [ChannelConfigOverrides], the second nested knob on
// that surface (after `reasoning`).
type AutonomousOverrides struct {
	Enabled   *bool     `json:"enabled,omitempty"`
	Topic     *string   `json:"topic,omitempty"`
	Agenda    *[]string `json:"agenda,omitempty"`
	Convener  *string   `json:"convener,omitempty"`
	Goal      *string   `json:"goal,omitempty"`
	MaxRounds *int      `json:"max_rounds,omitempty"`
}

// IsEmpty reports whether no autonomous sub-knob is set — the inherit-all state.
// Used by the REST merge to collapse an all-cleared block back to nil (absent
// from the blob) rather than persist a literal `{}`. Not a struct `==` (the slice
// pointer makes the struct non-comparable), so it is checked field by field.
func (o AutonomousOverrides) IsEmpty() bool {
	return o.Enabled == nil && o.Topic == nil && o.Agenda == nil &&
		o.Convener == nil && o.Goal == nil && o.MaxRounds == nil
}

// validateFields runs the per-field range invariants against the set sub-knobs —
// the override-path mirror of [AutonomousConfig.validateFields]. Per-field only
// (a nil sub-knob is "inherit" and never checked); the cap / convener cross-field
// rules live in [ChannelConfigOverrides.validateAutonomous] (cap, convener-distinct)
// and [ChannelRouter.validateAutonomousConvener] (convener membership).
func (o *AutonomousOverrides) validateFields() error {
	if o == nil {
		return nil
	}
	if o.MaxRounds != nil && *o.MaxRounds < 0 {
		return fmt.Errorf("%w: %d (must be >= 0)", ErrInvalidAutonomousMaxRounds, *o.MaxRounds)
	}
	if o.Agenda != nil {
		if len(*o.Agenda) > MaxAutonomousAgendaItems {
			return fmt.Errorf("%w: %d items (max %d)", ErrInvalidAutonomousAgenda, len(*o.Agenda), MaxAutonomousAgendaItems)
		}
		for i, item := range *o.Agenda {
			// Trim before the blank check — a whitespace-only item is blank in spirit
			// (see [AutonomousConfig.validateFields]).
			if strings.TrimSpace(item) == "" {
				return fmt.Errorf("%w: item %d is blank", ErrInvalidAutonomousAgenda, i)
			}
		}
	}
	return nil
}

// resolve overlays the set sub-knobs onto a base config (normally the disabled
// default) — the inherit-or-override resolution the apply path stamps onto the
// router. A nil sub-knob leaves the base value; a set one wins.
func (o *AutonomousOverrides) resolve(base AutonomousConfig) AutonomousConfig {
	if o == nil {
		return base
	}
	if o.Enabled != nil {
		base.Enabled = *o.Enabled
	}
	if o.Topic != nil {
		base.Topic = *o.Topic
	}
	if o.Agenda != nil {
		base.Agenda = append([]string(nil), (*o.Agenda)...)
	}
	if o.Convener != nil {
		base.Convener = *o.Convener
	}
	if o.Goal != nil {
		base.Goal = *o.Goal
	}
	if o.MaxRounds != nil {
		base.MaxRounds = *o.MaxRounds
	}
	return base
}

// effectiveEnabled reports whether this override resolves to an armed channel —
// the set `enabled` or, if absent, the disabled default. Used by the cross-field
// cap / convener rules so an override that does not touch `enabled` is judged on
// the inherited (disabled) state and never trips the gates.
func (o *AutonomousOverrides) effectiveEnabled() bool {
	if o != nil && o.Enabled != nil {
		return *o.Enabled
	}
	return DefaultAutonomousEnabled
}

// effectiveConvener reports the convener this override resolves to — the set value
// or, if absent, "" (no convener). Used by the convener cross-field checks.
func (o *AutonomousOverrides) effectiveConvener() string {
	if o != nil && o.Convener != nil {
		return *o.Convener
	}
	return ""
}

// validateAutonomous enforces the autonomous cross-field invariants computable
// from the override struct alone (no live roster): the per-field ranges, plus —
// when the merged block is armed — the MANDATORY cost cap and the convener rules
// that do not need membership (non-empty, distinct from the chair). The
// convener-IS-a-member rule needs the store and lives in
// [ChannelRouter.validateAutonomousConvener], called from
// [ChannelRouter.ApplyChannelConfig].
//
// `patch` here is the COMPLETE merged override set ([Server.handlePatchChannelConfig]
// folds a sparse PATCH onto the stored/resolved overrides first), so the budget
// and chair fields below ARE the effective post-apply values — a separate
// `{autonomous}` PATCH onto a channel whose budget is already frozen in the blob
// carries that budget here and is judged correctly.
func (o ChannelConfigOverrides) validateAutonomous() error {
	if err := o.Autonomous.validateFields(); err != nil {
		return err
	}
	if !o.Autonomous.effectiveEnabled() {
		return nil
	}
	// Cap-required: the merged budget must resolve positive. An explicit override
	// wins; an absent one would inherit the fleet default, which the apply path
	// cannot see here — but the RFC 0050 first-edit baseline freezes the resolved
	// budget into the blob, so an armed channel always carries an explicit value by
	// the time it is edited. A non-positive (or absent) budget on an armed channel
	// is rejected: uncapped autonomy is un-creatable.
	if o.InteractionBudgetTokens == nil || *o.InteractionBudgetTokens <= 0 {
		return fmt.Errorf("%w (set interaction_budget_tokens to a positive value)", ErrAutonomousCapRequired)
	}
	convener := o.Autonomous.effectiveConvener()
	if convener == "" {
		return fmt.Errorf("%w: an autonomous channel needs a convener to author the opening turn", ErrInvalidAutonomousConvener)
	}
	// Chair-required (RFC 0052 §D / PR 4): the chair authors the mandatory synthesis
	// turn on close, so an armed channel must declare one. The merge base freezes the
	// resolved chair into the override set (the matched pair in [Server.resolvedConfigBaseline]
	// + [Server.autonomousBaseline]), so an armed channel carries an explicit chair by
	// the time it is edited — and a drifted/absent chair drops the whole armed block
	// from the first-edit baseline (the chair leg of the un-closeable drop), so this
	// gate is the apply-time mirror of the load-path [Config.Validate] check, never a
	// lockout on an unrelated edit. The membership/observer/floor-control rules on a
	// SET chair stay in [ChannelRouter.validateEscalationChair].
	if o.EscalationChairID == nil || *o.EscalationChairID == "" {
		return ErrAutonomousChairRequired
	}
	if *o.EscalationChairID == convener {
		return fmt.Errorf("%w: %q is also the escalation_chair_id; the convener owns the agenda lifecycle and is a distinct role from the chair (RFC 0052 OQ #1)",
			ErrInvalidAutonomousConvener, convener)
	}
	return nil
}
