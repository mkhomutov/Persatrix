package channels

import (
	"path/filepath"
	"strconv"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func writeYAML(t *testing.T, body string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "channels.yaml")
	require.NoError(t, writeFile(p, body))
	return p
}

// formatFloat renders a float as a plain decimal for inline YAML bodies
// (avoids scientific notation that would parse as a string).
func formatFloat(v float64) string {
	return strconv.FormatFloat(v, 'f', -1, 64)
}

func TestLoadConfig_Empty_DefaultsApplied(t *testing.T) {
	p := writeYAML(t, "")
	cfg, err := LoadConfig(p)
	require.NoError(t, err)
	assert.Equal(t, DefaultMaxChannels, cfg.MaxChannels)
	assert.Empty(t, cfg.Channels)
}

func TestLoadConfig_AcceptsShorthandAndObjectMembers(t *testing.T) {
	body := `
max_channels: 10
channels:
  - name: planning
    description: "Planning"
    members:
      - alice
      - id: bob
        respond: always
      - id: carol
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	require.Len(t, cfg.Channels, 1)
	assert.Equal(t, "planning", cfg.Channels[0].Name)
	require.Len(t, cfg.Channels[0].Members, 3)
	assert.Equal(t, "alice", cfg.Channels[0].Members[0].ID)
	assert.Equal(t, RespondWhenMentioned, cfg.Channels[0].Members[0].RespondPolicy)
	assert.Equal(t, "bob", cfg.Channels[0].Members[1].ID)
	assert.Equal(t, RespondAlways, cfg.Channels[0].Members[1].RespondPolicy)
	assert.Equal(t, "carol", cfg.Channels[0].Members[2].ID)
	assert.Equal(t, RespondWhenMentioned, cfg.Channels[0].Members[2].RespondPolicy)
}

func TestLoadConfig_RejectsLegacyVocabulary(t *testing.T) {
	// `direct` / `broadcast` / `meeting` were dropped in v0.3.0 (RFC 0011).
	// The loader doesn't validate channel `type` (group is implicit), but a
	// legacy schema usually carries it. The schema rewrite forbids `type`
	// via additionalProperties: false; assert the loader surfaces it as a
	// parse error.
	body := `
channels:
  - name: planning
    type: broadcast
    members:
      - alice
`
	_, err := LoadConfig(writeYAML(t, body))
	assert.Error(t, err, "unknown field `type` should fail parse")
}

func TestLoadConfig_RejectsBadParticipantID(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - "agent:bad"
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidParticipantID)
}

func TestLoadConfig_RejectsDuplicateMember(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - alice
      - alice
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "duplicate id")
}

func TestLoadConfig_RejectsDuplicateChannelName(t *testing.T) {
	body := `
channels:
  - name: planning
    members: [alice]
  - name: planning
    members: [bob]
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "duplicate name")
}

func TestLoadConfig_RejectsExceedingCap(t *testing.T) {
	body := `
max_channels: 1
channels:
  - name: alpha
    members: [alice]
  - name: bravo
    members: [bob]
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrChannelCapExceeded)
}

func TestLoadConfig_RejectsBadRespondPolicy(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: alice
        respond: sometimes
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidRespondPolicy)
}

// TestLoadConfig_NormalizesDispositionVocabulary pins the RFC 0030
// relevance-amendment PR 1 contract (D1/D4): the disposition vocabulary
// (`participant`/`addressed`/`observer`) loads and normalizes to the
// legacy `respond_policy` the whole downstream stack already reads
// (`always`/`when_mentioned`/`never`). Normalization happens at the
// config-load boundary so the wire value, the fanout candidate set,
// floor control, and the Python gate all keep seeing the canonical
// three values — making the vocabulary addition behaviourally inert.
func TestLoadConfig_NormalizesDispositionVocabulary(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: participant-member
        respond: participant
      - id: addressed-member
        respond: addressed
      - id: observer-member
        respond: observer
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	require.Len(t, cfg.Channels, 1)
	require.Len(t, cfg.Channels[0].Members, 3)
	assert.Equal(t, RespondAlways, cfg.Channels[0].Members[0].RespondPolicy,
		"participant normalizes to the legacy `always`")
	assert.Equal(t, RespondWhenMentioned, cfg.Channels[0].Members[1].RespondPolicy,
		"addressed normalizes to the legacy `when_mentioned`")
	assert.Equal(t, RespondNever, cfg.Channels[0].Members[2].RespondPolicy,
		"observer normalizes to the legacy `never`")
}

// TestLoadConfig_LegacyVocabularyPassesThroughUnchanged pins back-compat
// (D4): an existing config that still uses `always`/`when_mentioned`/
// `never` loads with those exact canonical values — the disposition
// addition must not perturb the legacy path.
func TestLoadConfig_LegacyVocabularyPassesThroughUnchanged(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: a
        respond: always
      - id: b
        respond: when_mentioned
      - id: c
        respond: never
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	require.Len(t, cfg.Channels[0].Members, 3)
	assert.Equal(t, RespondAlways, cfg.Channels[0].Members[0].RespondPolicy)
	assert.Equal(t, RespondWhenMentioned, cfg.Channels[0].Members[1].RespondPolicy)
	assert.Equal(t, RespondNever, cfg.Channels[0].Members[2].RespondPolicy)
}

// TestLoadConfig_ChairNormalizesToAlwaysWithLowThreshold pins the v0.3.8
// Tier B PR 1 contract: the `chair` disposition loads as a `participant`
// (legacy `always`) carrying a low default `threshold`, so it clears the
// salience bid readily once Tier B reads the field (PR 2). The chair-ness
// survives only as the low threshold — the wire `respond_policy` stays the
// canonical `always` every downstream reader already understands.
func TestLoadConfig_ChairNormalizesToAlwaysWithLowThreshold(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: facilitator
        respond: chair
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	require.Len(t, cfg.Channels[0].Members, 1)
	m := cfg.Channels[0].Members[0]
	assert.Equal(t, RespondAlways, m.RespondPolicy,
		"chair normalizes to the legacy `always` wire value")
	require.NotNil(t, m.Threshold,
		"chair must carry the low default threshold so it clears the Tier B bid")
	assert.InDelta(t, DefaultChairThreshold, *m.Threshold, 1e-9)
}

// TestLoadConfig_ChairHonoursExplicitThreshold pins that an operator can
// override the chair's low default with an explicit value.
func TestLoadConfig_ChairHonoursExplicitThreshold(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: facilitator
        respond: chair
        threshold: 0.4
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	m := cfg.Channels[0].Members[0]
	assert.Equal(t, RespondAlways, m.RespondPolicy)
	require.NotNil(t, m.Threshold)
	assert.InDelta(t, 0.4, *m.Threshold, 1e-9,
		"an explicit threshold overrides the chair default")
}

// TestLoadConfig_AcceptsPerMemberThreshold pins that a per-disposition
// `threshold` in the `[0, 1]` salience range loads on any disposition, and
// that an absent threshold leaves the field unset (nil → bias-to-silence,
// honoured by Tier B in PR 2).
func TestLoadConfig_AcceptsPerMemberThreshold(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: with-threshold
        respond: participant
        threshold: 0.7
      - id: without-threshold
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	require.Len(t, cfg.Channels[0].Members, 2)
	require.NotNil(t, cfg.Channels[0].Members[0].Threshold)
	assert.InDelta(t, 0.7, *cfg.Channels[0].Members[0].Threshold, 1e-9)
	assert.Nil(t, cfg.Channels[0].Members[1].Threshold,
		"an absent threshold stays unset (bias-to-silence)")
}

// TestLoadConfig_AcceptsThresholdBoundaries pins both endpoints of the
// `[0, 1]` salience range as valid (mirrors the schema bound).
func TestLoadConfig_AcceptsThresholdBoundaries(t *testing.T) {
	for _, v := range []float64{0.0, 1.0} {
		body := `
channels:
  - name: planning
    members:
      - id: alice
        respond: participant
        threshold: ` + formatFloat(v) + `
`
		cfg, err := LoadConfig(writeYAML(t, body))
		require.NoError(t, err, "threshold %v must load", v)
		require.NotNil(t, cfg.Channels[0].Members[0].Threshold)
		assert.InDelta(t, v, *cfg.Channels[0].Members[0].Threshold, 1e-9)
	}
}

// TestLoadConfig_RejectsOutOfRangeThreshold pins that a threshold outside
// `[0, 1]` fails the loader with ErrInvalidThreshold (the belt-and-
// suspenders for an operator who skipped `make validate`).
func TestLoadConfig_RejectsOutOfRangeThreshold(t *testing.T) {
	for _, v := range []float64{-0.1, 1.5} {
		body := `
channels:
  - name: planning
    members:
      - id: alice
        respond: participant
        threshold: ` + formatFloat(v) + `
`
		_, err := LoadConfig(writeYAML(t, body))
		require.Error(t, err, "threshold %v must be rejected", v)
		assert.ErrorIs(t, err, ErrInvalidThreshold)
	}
}

func TestChannelConfig_CanonicalID(t *testing.T) {
	cc := ChannelConfig{Name: "planning"}
	assert.Equal(t, "group:planning", cc.CanonicalID())
}

// TestLoadConfig_RejectsNegativeMaxCascadeDepth pins PR #319 deep review
// finding 5.2: a negative `max_cascade_depth:` in `channels.yaml` MUST
// surface as a loader error rather than silently falling through to
// `SetMaxCascadeDepth(-1)` (which ignores non-positive and leaves the
// cap at the default). The JSON schema already rejects negatives at
// `make validate` time; this guard is the belt-and-suspenders for the
// operator who skipped that step.
func TestLoadConfig_RejectsNegativeMaxCascadeDepth(t *testing.T) {
	body := "max_cascade_depth: -1\n"
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidMaxCascadeDepth)
}

// TestLoadConfig_AcceptsZeroMaxCascadeDepth pins the loader-default
// sentinel contract from the schema. `max_cascade_depth: 0` MUST load
// cleanly: `cmd/orchestrator/channels.go` then calls
// `SetMaxCascadeDepth(0)` which the router ignores, so the active cap
// stays at `defaults.DefaultMaxCascadeDepth`. Rejecting zero here would
// break the documented "leave the key to take the default" path
// (`schemas/channel.schema.json` minimum: 0; companion
// `test_max_cascade_depth_zero_is_accepted_for_loader_default_substitution`
// in `tests/unit/python/test_channel_config_schema.py`).
func TestLoadConfig_AcceptsZeroMaxCascadeDepth(t *testing.T) {
	body := "max_cascade_depth: 0\n"
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, 0, cfg.MaxCascadeDepth)
}

// TestLoadConfig_AcceptsPositiveMaxCascadeDepth pins the operator's
// tightening path. The Go loader has no opinion on which positive
// integer is "correct" — that's the operator's call to make and the
// router consumes whatever it gets (`ChannelRouter.SetMaxCascadeDepth`).
func TestLoadConfig_AcceptsPositiveMaxCascadeDepth(t *testing.T) {
	body := "max_cascade_depth: 3\n"
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, 3, cfg.MaxCascadeDepth)
}

// TestLoadConfig_FloorControlDefaults — an absent `floor_control` key parses
// as the nil tri-state sentinel (operator said nothing), and the resolved
// default for a declared (group) channel is ON in PR 3 (RFC 0030 amendment
// Layer 2.5). The per-turn timeout still normalizes to the canonical 45s
// default (amendment D2). The raw field stays nil so the resolver — not the
// loader — owns the group-default policy; a DM (single responder, never
// declared here) is unaffected because floor control no-ops below 2
// responders regardless.
func TestLoadConfig_FloorControlDefaults(t *testing.T) {
	body := `
channels:
  - name: planning
    members: [alice, bob]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	require.Len(t, cfg.Channels, 1)
	assert.Nil(t, cfg.Channels[0].FloorControl,
		"an absent floor_control key parses as nil (the 'operator said nothing' tri-state)")
	assert.True(t, cfg.Channels[0].FloorControlEnabled(),
		"PR 3: the resolved default for a declared group channel is floor-control ON")
	assert.Equal(t, DefaultFloorTurnTimeoutSeconds, cfg.Channels[0].FloorTurnTimeoutSeconds,
		"absent floor_turn_timeout_seconds normalizes to the 45s default")
}

// TestLoadConfig_FloorControlExplicit — operators can opt a channel in and
// override the per-turn timeout; both pass through to the parsed config, and
// the resolver agrees with the explicit `true`.
func TestLoadConfig_FloorControlExplicit(t *testing.T) {
	body := `
channels:
  - name: planning
    floor_control: true
    floor_turn_timeout_seconds: 30
    members: [alice, bob]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	require.Len(t, cfg.Channels, 1)
	require.NotNil(t, cfg.Channels[0].FloorControl)
	assert.True(t, *cfg.Channels[0].FloorControl)
	assert.True(t, cfg.Channels[0].FloorControlEnabled())
	assert.Equal(t, 30, cfg.Channels[0].FloorTurnTimeoutSeconds)
}

// TestLoadConfig_FloorControlExplicitFalse — PR 3 makes the group default ON,
// so the override that now matters is opting a channel back OUT. An explicit
// `floor_control: false` must survive as a non-nil false and resolve to OFF,
// proving the group-default-on resolver still honours an operator's deliberate
// opt-out (the reason the field is a `*bool` tri-state, not a plain bool — a
// plain bool cannot distinguish "absent" from "explicitly off").
func TestLoadConfig_FloorControlExplicitFalse(t *testing.T) {
	body := `
channels:
  - name: planning
    floor_control: false
    members: [alice, bob]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	require.Len(t, cfg.Channels, 1)
	require.NotNil(t, cfg.Channels[0].FloorControl,
		"an explicit floor_control: false must survive as a non-nil sentinel, not collapse to the absent case")
	assert.False(t, *cfg.Channels[0].FloorControl)
	assert.False(t, cfg.Channels[0].FloorControlEnabled(),
		"an explicit opt-out resolves to floor-control OFF even though the group default is ON")
}

// TestLoadConfig_RejectsNegativeFloorTurnTimeout — a negative per-turn
// timeout is an operator typo; reject it at the loader (belt-and-suspenders
// for the `make validate` minimum:1 schema check), mirroring the
// max_cascade_depth posture. Zero is the "use default" sentinel and is
// accepted (normalized to 45).
func TestLoadConfig_RejectsNegativeFloorTurnTimeout(t *testing.T) {
	body := `
channels:
  - name: planning
    floor_turn_timeout_seconds: -1
    members: [alice]
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidFloorTurnTimeout)
}

// TestLoadConfig_RejectsBadChannelName pins PR #231 review Should-Fix #6:
// the loader's Validate() now compiles and applies the same `name` regex the
// JSON Schema does (`schemas/channel.schema.json` →
// `definitions.channel.name.pattern`). Before this fix, a `name` like
// "Planning" or "x" parsed through `LoadConfig` cleanly and only blew up at
// `make validate`, so the loader and the schema disagreed about what a legal
// channel name was.
func TestLoadConfig_RejectsBadChannelName(t *testing.T) {
	cases := map[string]string{
		"uppercase":     "Planning",
		"single-char":   "x",
		"trailing-dash": "planning-",
		"leading-dash":  "-planning",
		"underscore":    "team_alpha",
	}
	for label, name := range cases {
		t.Run(label, func(t *testing.T) {
			body := "channels:\n  - name: " + name + "\n    members: [alice]\n"
			_, err := LoadConfig(writeYAML(t, body))
			require.Error(t, err)
			assert.Contains(t, err.Error(), "does not match")
		})
	}
}
