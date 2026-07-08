// RFC 0052 Phase 3a (PR 7) — the STANDING/scheduled config backend: the
// `autonomous.{schedule_interval_seconds,max_convenings,standing_budget_tokens}`
// knobs and the MANDATORY aggregate-bound safety gate. These pin the load,
// override, apply, and reconcile-freeze halves — the sibling of
// config_autonomous_test.go for the standing block.
//
// A STANDING channel is an armed channel with a positive schedule interval (the
// RFC 0024 timer that re-convenes it): every fire opens a fresh, SEPARATELY
// capped interaction, so the per-interaction cost cap does NOT bound the
// recurring total ([RFC 0052 §E](../../docs/rfcs/0052-autonomous-agent-channels.md)).
// A standing channel is therefore un-creatable without an AGGREGATE bound
// (`max_convenings` and/or `standing_budget_tokens`) — the §E mirror of the
// per-interaction cap-required gate that PR 1 landed.
//
// Ships DARK: nothing fires the schedule or counts convenings yet (the
// config-round-trip timer seam + the convening counter are PR 7b). The one
// LIVE effect of this slice is the aggregate-bound validate gate.
package channels

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- YAML load + validate (the aggregate-bound gate) ----------------------

// TestLoadConfig_AutonomousStandingRequiresBound: the headline §E safety
// invariant — an armed STANDING channel (a positive schedule interval) with NO
// aggregate bound is rejected at load, exactly as an uncapped armed channel is.
// The per-interaction cap alone leaves the recurring total unbounded.
func TestLoadConfig_AutonomousStandingRequiresBound(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    escalation_chair_id: ada
    autonomous:
      enabled: true
      topic: "Daily architecture review"
      convener: nova
      schedule_interval_seconds: 3600
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAutonomousStandingBoundRequired)
}

// TestLoadConfig_AutonomousStandingAcceptedWithMaxConvenings: schedule + a
// positive `max_convenings` count loads clean — the aggregate bound is present.
func TestLoadConfig_AutonomousStandingAcceptedWithMaxConvenings(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    escalation_chair_id: ada
    autonomous:
      enabled: true
      topic: "Daily architecture review"
      convener: nova
      schedule_interval_seconds: 3600
      max_convenings: 10
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	a := cfg.Channels[0].Autonomous
	assert.Equal(t, 3600, a.ScheduleIntervalSeconds)
	assert.Equal(t, 10, a.MaxConvenings)
}

// TestLoadConfig_AutonomousStandingAcceptedWithStandingBudget: schedule + a
// positive `standing_budget_tokens` (the aggregate cost budget, the alternative
// bound) loads clean — either bound satisfies the gate.
func TestLoadConfig_AutonomousStandingAcceptedWithStandingBudget(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    escalation_chair_id: ada
    autonomous:
      enabled: true
      topic: "Daily architecture review"
      convener: nova
      schedule_interval_seconds: 3600
      standing_budget_tokens: 5000000
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, int64(5000000), cfg.Channels[0].Autonomous.StandingBudgetTokens)
}

// TestLoadConfig_AutonomousOneShotNeedsNoAggregateBound: an armed channel with
// NO schedule (a one-shot brainstorm — today's behaviour) needs no aggregate
// bound; the per-interaction cap suffices. The gate is standing-only.
func TestLoadConfig_AutonomousOneShotNeedsNoAggregateBound(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    escalation_chair_id: ada
    autonomous:
      enabled: true
      topic: "Should we adopt a monorepo?"
      convener: nova
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err, "a one-shot armed channel needs no aggregate bound")
}

// TestLoadConfig_AutonomousDisabledStandingSkipsBound: a DISABLED block that
// carries a schedule (authored but not yet armed) does not trip the aggregate
// bound gate — the gate, like every other autonomous gate, guards only an armed
// channel.
func TestLoadConfig_AutonomousDisabledStandingSkipsBound(t *testing.T) {
	body := `
channels:
  - name: roundtable
    autonomous:
      enabled: false
      schedule_interval_seconds: 3600
    members:
      - id: nova
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err, "a disabled standing block is inert and not gated")
}

// TestLoadConfig_AutonomousNegativeScheduleInterval: a negative schedule interval
// is a typo the loader rejects per-field (zero is one-shot; only negative is an
// error).
func TestLoadConfig_AutonomousNegativeScheduleInterval(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    escalation_chair_id: ada
    autonomous:
      enabled: true
      convener: nova
      schedule_interval_seconds: -1
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidAutonomousSchedule)
}

// TestLoadConfig_AutonomousNegativeMaxConvenings: a negative aggregate count is a
// per-field typo the loader rejects (zero is unset).
func TestLoadConfig_AutonomousNegativeMaxConvenings(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    escalation_chair_id: ada
    autonomous:
      enabled: true
      convener: nova
      schedule_interval_seconds: 3600
      max_convenings: -5
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidAutonomousMaxConvenings)
}

// TestLoadConfig_AutonomousNegativeStandingBudget: a negative aggregate cost
// budget is a per-field typo the loader rejects (zero is unset).
func TestLoadConfig_AutonomousNegativeStandingBudget(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    escalation_chair_id: ada
    autonomous:
      enabled: true
      convener: nova
      schedule_interval_seconds: 3600
      standing_budget_tokens: -1
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidAutonomousStandingBudget)
}

// --- override per-field + cross-field validate ----------------------------

// TestAutonomousOverridesValidate_StandingBound: the runtime override Validate
// enforces the same standing aggregate-bound and per-field range invariants the
// loader does (the REST PATCH path runs this, not the JSON schema).
func TestAutonomousOverridesValidate_StandingBound(t *testing.T) {
	enabled := true
	budget := int64(200000)
	convener := "nova"
	chair := "ada"
	interval := 3600
	convenings := 10
	standingBudget := int64(5000000)

	// enabled + cap + convener + chair + schedule but NO aggregate bound →
	// standing-bound-required.
	noBound := ChannelConfigOverrides{
		Autonomous: &AutonomousOverrides{
			Enabled: &enabled, Convener: &convener, ScheduleIntervalSeconds: &interval,
		},
		InteractionBudgetTokens: &budget,
		EscalationChairID:       &chair,
	}
	assert.ErrorIs(t, noBound.Validate(), ErrAutonomousStandingBoundRequired)

	// + a max_convenings bound → clean.
	withCount := ChannelConfigOverrides{
		Autonomous: &AutonomousOverrides{
			Enabled: &enabled, Convener: &convener, ScheduleIntervalSeconds: &interval, MaxConvenings: &convenings,
		},
		InteractionBudgetTokens: &budget,
		EscalationChairID:       &chair,
	}
	assert.NoError(t, withCount.Validate())

	// + a standing_budget_tokens bound (the alternative) → clean.
	withBudget := ChannelConfigOverrides{
		Autonomous: &AutonomousOverrides{
			Enabled: &enabled, Convener: &convener, ScheduleIntervalSeconds: &interval, StandingBudgetTokens: &standingBudget,
		},
		InteractionBudgetTokens: &budget,
		EscalationChairID:       &chair,
	}
	assert.NoError(t, withBudget.Validate())

	// A one-shot armed override (no schedule) needs no aggregate bound.
	oneShot := ChannelConfigOverrides{
		Autonomous:              &AutonomousOverrides{Enabled: &enabled, Convener: &convener},
		InteractionBudgetTokens: &budget,
		EscalationChairID:       &chair,
	}
	assert.NoError(t, oneShot.Validate())

	// Per-field negatives are rejected regardless of enabled.
	neg := -1
	negBudget := int64(-1)
	assert.ErrorIs(t,
		(ChannelConfigOverrides{Autonomous: &AutonomousOverrides{ScheduleIntervalSeconds: &neg}}).Validate(),
		ErrInvalidAutonomousSchedule)
	assert.ErrorIs(t,
		(ChannelConfigOverrides{Autonomous: &AutonomousOverrides{MaxConvenings: &neg}}).Validate(),
		ErrInvalidAutonomousMaxConvenings)
	assert.ErrorIs(t,
		(ChannelConfigOverrides{Autonomous: &AutonomousOverrides{StandingBudgetTokens: &negBudget}}).Validate(),
		ErrInvalidAutonomousStandingBudget)
}

// --- apply path -----------------------------------------------------------

// TestApplyChannelConfig_AutonomousStandingRoundTrips: a standing override
// persists, bumps the revision, and is stamped onto the router (read back via
// AutonomousFor).
func TestApplyChannelConfig_AutonomousStandingRoundTrips(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	id := mustCreateGroup(t, store, "roundtable", "nova", "ada")

	enabled := true
	convener := "nova"
	chair := "ada"
	budget := int64(200000)
	interval := 3600
	convenings := 10
	standingBudget := int64(5000000)
	patch := ChannelConfigOverrides{
		Autonomous: &AutonomousOverrides{
			Enabled:                 &enabled,
			Convener:                &convener,
			ScheduleIntervalSeconds: &interval,
			MaxConvenings:           &convenings,
			StandingBudgetTokens:    &standingBudget,
		},
		InteractionBudgetTokens: &budget,
		EscalationChairID:       &chair,
	}
	require.NoError(t, router.ApplyChannelConfig(ctx, id, patch, 0, ""))

	got, revision, err := store.GetChannelConfig(ctx, id)
	require.NoError(t, err)
	assert.Equal(t, int64(1), revision, "apply bumps revision")
	require.NotNil(t, got.Autonomous)
	require.NotNil(t, got.Autonomous.ScheduleIntervalSeconds)
	assert.Equal(t, 3600, *got.Autonomous.ScheduleIntervalSeconds)

	a := router.AutonomousFor(id)
	assert.Equal(t, 3600, a.ScheduleIntervalSeconds, "router reflects the schedule")
	assert.Equal(t, 10, a.MaxConvenings)
	assert.Equal(t, int64(5000000), a.StandingBudgetTokens)
}

// TestApplyChannelConfig_AutonomousStandingBoundRequired: arming a standing
// channel with no aggregate bound is rejected on the runtime apply path too, and
// never writes.
func TestApplyChannelConfig_AutonomousStandingBoundRequired(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	id := mustCreateGroup(t, store, "roundtable", "nova", "ada")

	enabled := true
	convener := "nova"
	chair := "ada"
	budget := int64(200000)
	interval := 3600
	err := router.ApplyChannelConfig(ctx, id, ChannelConfigOverrides{
		Autonomous: &AutonomousOverrides{
			Enabled: &enabled, Convener: &convener, ScheduleIntervalSeconds: &interval,
		},
		InteractionBudgetTokens: &budget,
		EscalationChairID:       &chair,
	}, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAutonomousStandingBoundRequired)

	_, revision, err := store.GetChannelConfig(ctx, id)
	require.NoError(t, err)
	assert.Equal(t, int64(0), revision, "rejected applies never write")
}

// --- reconcile / freeze ---------------------------------------------------

// TestToConfigOverrides_CapturesStandingSchedule: a YAML-declared STANDING rung
// must be captured in the override snapshot the reconcile persists — without it
// the boot round-trip (reconcile → ResolveFromStore) would silently drop the
// schedule + aggregate bound of a revisioned standing channel, and the apply-path
// gate would lose the frozen bound the merge base depends on.
func TestToConfigOverrides_CapturesStandingSchedule(t *testing.T) {
	body := `
channels:
  - name: roundtable
    revision: 1
    interaction_budget_tokens: 200000
    escalation_chair_id: ada
    autonomous:
      enabled: true
      topic: "Daily architecture review"
      convener: nova
      schedule_interval_seconds: 3600
      max_convenings: 10
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)

	o := cfg.Channels[0].toConfigOverrides(cfg)
	require.NotNil(t, o.Autonomous, "an armed standing YAML rung must be captured")
	require.NotNil(t, o.Autonomous.ScheduleIntervalSeconds)
	assert.Equal(t, 3600, *o.Autonomous.ScheduleIntervalSeconds)
	require.NotNil(t, o.Autonomous.MaxConvenings)
	assert.Equal(t, 10, *o.Autonomous.MaxConvenings)
	// An unset standing_budget_tokens stays inherit (per-sub-knob freeze), not
	// frozen as an explicit zero.
	assert.Nil(t, o.Autonomous.StandingBudgetTokens, "an unset aggregate cost budget stays inherit")
}

// TestAutonomousFor_StandingDefaultsUnset: a channel with no resolved entry falls
// back to the disabled default rung — schedule/aggregate-bound knobs read as
// unset (0), never a spurious standing channel.
func TestAutonomousFor_StandingDefaultsUnset(t *testing.T) {
	router, _, _ := newApplyRouter(t)
	a := router.AutonomousFor("group:never-seen")
	assert.Equal(t, 0, a.ScheduleIntervalSeconds)
	assert.Equal(t, 0, a.MaxConvenings)
	assert.Equal(t, int64(0), a.StandingBudgetTokens)
}
