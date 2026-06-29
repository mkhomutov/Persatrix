// RFC 0052 Phase 1a (PR 1) — the per-channel `autonomous` config block on the RFC
// 0050 surface. These tests pin (a) the YAML load/normalize/validate path for the
// `autonomous.{enabled,topic,agenda,convener,goal,max_rounds}` knob — most of all
// the MANDATORY-cost-cap safety gate (`autonomous.enabled` requires a positive
// resolved `interaction_budget_tokens`) and the OQ #1 convener rules (a declared
// member, distinct from the escalation chair) — and (b) the runtime override apply
// path (validate → persist → bump revision → stamp router). The block ships DARK:
// no convene path consults it yet, so these only exercise config plumbing.
package channels

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- YAML load + validate -------------------------------------------------

// TestLoadConfig_AutonomousDisabledByDefault: a channel with no `autonomous`
// block loads as a disabled, default rung (the byte-for-byte ordinary channel).
func TestLoadConfig_AutonomousDisabledByDefault(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: ada
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	a := cfg.Channels[0].Autonomous
	assert.False(t, a.Enabled, "autonomy is opt-in: absent block is disabled")
	assert.Equal(t, DefaultAutonomousMaxRounds, a.MaxRounds, "max_rounds fills the default")
}

// TestLoadConfig_AutonomousRequiresCap: the headline safety invariant — an
// `autonomous.enabled` channel with NO interaction budget (neither per-channel nor
// fleet default) is rejected at load. Uncapped autonomy never reaches the store.
func TestLoadConfig_AutonomousRequiresCap(t *testing.T) {
	body := `
channels:
  - name: roundtable
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
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAutonomousCapRequired)
}

// TestLoadConfig_AutonomousAcceptedWithCap: enabled + a positive per-channel
// `interaction_budget_tokens` + a valid convener loads clean and fills max_rounds.
func TestLoadConfig_AutonomousAcceptedWithCap(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    autonomous:
      enabled: true
      topic: "Should we adopt a monorepo?"
      agenda:
        - "Build tooling cost"
        - "Cross-team coupling"
      convener: nova
      goal: "A synthesized recommendation."
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	a := cfg.Channels[0].Autonomous
	assert.True(t, a.Enabled)
	assert.Equal(t, "nova", a.Convener)
	assert.Len(t, a.Agenda, 2)
	assert.Equal(t, DefaultAutonomousMaxRounds, a.MaxRounds, "absent max_rounds fills the default")
}

// TestLoadConfig_AutonomousFleetDefaultCapSatisfies: an enabled channel with no
// per-channel budget but a positive fleet `default_interaction_budget_tokens`
// resolves to an enforced cap and is accepted — the gate is on the RESOLVED value.
func TestLoadConfig_AutonomousFleetDefaultCapSatisfies(t *testing.T) {
	body := `
default_interaction_budget_tokens: 150000
channels:
  - name: roundtable
    autonomous:
      enabled: true
      topic: "Tradeoffs"
      convener: nova
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err, "a positive fleet default is an enforced cap")
}

// TestLoadConfig_AutonomousConvenerMustBeMember: the convener authors the opening
// turn, so it must be a declared roster member (OQ #1) — a non-member is rejected.
func TestLoadConfig_AutonomousConvenerMustBeMember(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    autonomous:
      enabled: true
      topic: "Tradeoffs"
      convener: ghost
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidAutonomousConvener)
}

// TestLoadConfig_AutonomousConvenerRequired: an enabled channel with no convener
// is rejected — there is no persona to author the opening turn.
func TestLoadConfig_AutonomousConvenerRequired(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    autonomous:
      enabled: true
      topic: "Tradeoffs"
    members:
      - id: nova
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidAutonomousConvener)
}

// TestLoadConfig_AutonomousConvenerDistinctFromChair: OQ #1 reverses the RFC's
// "chair = convener" lean — the convener owns the agenda lifecycle while the chair
// keeps its shipped close role, so the two roles must be distinct members.
func TestLoadConfig_AutonomousConvenerDistinctFromChair(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    escalation_chair_id: nova
    autonomous:
      enabled: true
      topic: "Tradeoffs"
      convener: nova
    members:
      - id: nova
        respond: participant
      - id: ada
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidAutonomousConvener)
}

// TestLoadConfig_AutonomousConvenerMustNotBeObserver: the convener authors the
// opening turn, so an `observer` (respond: never) convener is as guaranteed-futile
// as a non-member — the receiver gate suppresses it before any LLM — exactly the
// rule the escalation chair already enforces. Rejected at load.
func TestLoadConfig_AutonomousConvenerMustNotBeObserver(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    autonomous:
      enabled: true
      topic: "Tradeoffs"
      convener: ghost
    members:
      - id: ghost
        respond: never
      - id: ada
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidAutonomousConvener)
}

// TestAutonomousValidateFields_RejectsBlankAgendaItem: a whitespace-only agenda
// item is blank in spirit — the schema's `minLength: 1` and a bare `== ""` check
// both miss it — so validateFields trims before the blank check. Pinned on both the
// load value type and the override mirror.
func TestAutonomousValidateFields_RejectsBlankAgendaItem(t *testing.T) {
	loaded := AutonomousConfig{Agenda: []string{"   "}}.normalized()
	assert.ErrorIs(t, loaded.validateFields(), ErrInvalidAutonomousAgenda)

	ws := "\t "
	ov := &AutonomousOverrides{Agenda: &[]string{ws}}
	assert.ErrorIs(t, ov.validateFields(), ErrInvalidAutonomousAgenda)
}

// TestLoadConfig_AutonomousNegativeMaxRounds: a negative `max_rounds` is a typo the
// loader rejects (zero normalizes to the default before validate runs).
func TestLoadConfig_AutonomousNegativeMaxRounds(t *testing.T) {
	body := `
channels:
  - name: roundtable
    interaction_budget_tokens: 200000
    autonomous:
      enabled: true
      topic: "Tradeoffs"
      convener: nova
      max_rounds: -1
    members:
      - id: nova
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidAutonomousMaxRounds)
}

// TestLoadConfig_AutonomousDisabledSkipsGates: a DISABLED block (e.g. authored but
// not yet armed) does not trip the cap / convener gates — they guard only an armed
// autonomous channel.
func TestLoadConfig_AutonomousDisabledSkipsGates(t *testing.T) {
	body := `
channels:
  - name: roundtable
    autonomous:
      enabled: false
      topic: "Tradeoffs"
    members:
      - id: nova
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err, "a disabled autonomous block is inert and not gated")
}

// --- override per-field validate ------------------------------------------

// TestAutonomousOverridesValidate: the runtime override Validate rejects the same
// field-range and cross-field invariants the loader does (the REST PATCH path runs
// this, not the JSON schema).
func TestAutonomousOverridesValidate(t *testing.T) {
	enabled := true
	neg := -1
	budget := int64(200000)
	convener := "nova"

	// enabled without a cap → cap-required.
	capMissing := ChannelConfigOverrides{Autonomous: &AutonomousOverrides{Enabled: &enabled, Convener: &convener}}
	assert.ErrorIs(t, capMissing.Validate(), ErrAutonomousCapRequired)

	// enabled with a positive cap + convener → clean.
	ok := ChannelConfigOverrides{
		Autonomous:              &AutonomousOverrides{Enabled: &enabled, Convener: &convener},
		InteractionBudgetTokens: &budget,
	}
	assert.NoError(t, ok.Validate())

	// negative max_rounds → rejected per-field regardless of enabled.
	badRounds := ChannelConfigOverrides{Autonomous: &AutonomousOverrides{MaxRounds: &neg}}
	assert.ErrorIs(t, badRounds.Validate(), ErrInvalidAutonomousMaxRounds)

	// enabled + convener == chair → convener rule.
	chair := "nova"
	clash := ChannelConfigOverrides{
		Autonomous:              &AutonomousOverrides{Enabled: &enabled, Convener: &convener},
		InteractionBudgetTokens: &budget,
		EscalationChairID:       &chair,
	}
	assert.ErrorIs(t, clash.Validate(), ErrInvalidAutonomousConvener)
}

// --- apply path -----------------------------------------------------------

// TestApplyChannelConfig_AutonomousRoundTrips: an autonomous override persists,
// bumps the revision, and is stamped onto the router (read back via AutonomousFor).
func TestApplyChannelConfig_AutonomousRoundTrips(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	id := mustCreateGroup(t, store, "roundtable", "nova", "ada")

	enabled := true
	convener := "nova"
	budget := int64(200000)
	topic := "Should we adopt a monorepo?"
	patch := ChannelConfigOverrides{
		Autonomous: &AutonomousOverrides{
			Enabled:  &enabled,
			Convener: &convener,
			Topic:    &topic,
		},
		InteractionBudgetTokens: &budget,
	}
	require.NoError(t, router.ApplyChannelConfig(ctx, id, patch, 0, ""))

	got, revision, err := store.GetChannelConfig(ctx, id)
	require.NoError(t, err)
	assert.Equal(t, int64(1), revision, "apply bumps revision")
	require.NotNil(t, got.Autonomous)
	require.NotNil(t, got.Autonomous.Enabled)
	assert.True(t, *got.Autonomous.Enabled)

	a := router.AutonomousFor(id)
	assert.True(t, a.Enabled, "router reflects the edit")
	assert.Equal(t, "nova", a.Convener)
	assert.Equal(t, topic, a.Topic)
	assert.Equal(t, DefaultAutonomousMaxRounds, a.MaxRounds, "unset max_rounds resolves to the default")
}

// TestApplyChannelConfig_AutonomousCapRequired: an enabled override with no
// positive budget is rejected and never writes.
func TestApplyChannelConfig_AutonomousCapRequired(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	id := mustCreateGroup(t, store, "roundtable", "nova", "ada")

	enabled := true
	convener := "nova"
	err := router.ApplyChannelConfig(ctx, id,
		ChannelConfigOverrides{Autonomous: &AutonomousOverrides{Enabled: &enabled, Convener: &convener}}, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAutonomousCapRequired)

	_, revision, err := store.GetChannelConfig(ctx, id)
	require.NoError(t, err)
	assert.Equal(t, int64(0), revision, "rejected applies never write")
}

// TestApplyChannelConfig_AutonomousConvenerMustBeMember: the convener-membership
// rule is cross-field (it needs live roster), enforced at apply against the store.
func TestApplyChannelConfig_AutonomousConvenerMustBeMember(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	id := mustCreateGroup(t, store, "roundtable", "nova", "ada")

	enabled := true
	ghost := "ghost"
	budget := int64(200000)
	err := router.ApplyChannelConfig(ctx, id, ChannelConfigOverrides{
		Autonomous:              &AutonomousOverrides{Enabled: &enabled, Convener: &ghost},
		InteractionBudgetTokens: &budget,
	}, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidAutonomousConvener)
}

// TestApplyChannelConfig_AutonomousConvenerMustNotBeObserver: the convener-observer
// rule is cross-field (it needs the live roster's respond policy), enforced at apply
// against the store — the mirror of the escalation chair's observer rejection.
func TestApplyChannelConfig_AutonomousConvenerMustNotBeObserver(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	id := mustCreateGroupWithPolicies(t, store, "roundtable",
		map[string]RespondPolicy{"ghost": RespondNever, "ada": RespondAlways}, "ghost", "ada")

	enabled := true
	convener := "ghost"
	budget := int64(200000)
	err := router.ApplyChannelConfig(ctx, id, ChannelConfigOverrides{
		Autonomous:              &AutonomousOverrides{Enabled: &enabled, Convener: &convener},
		InteractionBudgetTokens: &budget,
	}, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidAutonomousConvener)

	_, revision, err := store.GetChannelConfig(ctx, id)
	require.NoError(t, err)
	assert.Equal(t, int64(0), revision, "rejected applies never write")
}

// TestAutonomousFor_DefaultForUnconfigured: a channel with no resolved entry falls
// back to the disabled default rung — the read is always a complete value.
func TestAutonomousFor_DefaultForUnconfigured(t *testing.T) {
	router, _, _ := newApplyRouter(t)
	a := router.AutonomousFor("group:never-seen")
	assert.False(t, a.Enabled)
	assert.Equal(t, DefaultAutonomousMaxRounds, a.MaxRounds)
}

// TestApplyChannelConfig_AutonomousRejectedOnNonGroup: autonomous convening is an
// open-floor GROUP concept — [ChannelRouter.ResolveAutonomous] seeds only group
// channels and the PR 3 convene path dispatches an open-floor seed turn. Arming a
// DM (or thread) would create a channel the convene path can never act on — an
// armed-but-unconvenable channel the validation otherwise accepted — so the apply
// path rejects it. The convener here IS a DM member, so only the channel-type rule
// (not the convener-membership rule) is under test.
func TestApplyChannelConfig_AutonomousRejectedOnNonGroup(t *testing.T) {
	router, store, ctx := newApplyRouter(t)
	dm, err := store.GetOrCreateDM(ctx, "nova", "ada")
	require.NoError(t, err)

	enabled := true
	convener := "nova"
	budget := int64(200000)
	err = router.ApplyChannelConfig(ctx, dm.ID, ChannelConfigOverrides{
		Autonomous:              &AutonomousOverrides{Enabled: &enabled, Convener: &convener},
		InteractionBudgetTokens: &budget,
	}, 0, "")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAutonomousNotGroup)

	_, revision, err := store.GetChannelConfig(ctx, dm.ID)
	require.NoError(t, err)
	assert.Equal(t, int64(0), revision, "rejected applies never write")
}
