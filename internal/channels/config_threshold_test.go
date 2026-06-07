package channels

import (
	"strconv"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// formatFloat renders a float as a plain decimal for inline YAML bodies
// (avoids scientific notation that would parse as a string).
func formatFloat(v float64) string {
	return strconv.FormatFloat(v, 'f', -1, 64)
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
// `threshold` in the `[0, 1]` salience range loads on an open-floor
// disposition, and that an absent threshold leaves the field unset (nil →
// bias-to-silence, honoured by Tier B in PR 2).
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

// TestLoadConfig_RejectsNaNThreshold pins that a non-finite `threshold`
// (`.nan`) is rejected. NaN slips past a bare `< 0 || > 1` range check —
// every comparison against NaN is false — so the loader needs an explicit
// finite-value guard; otherwise a NaN salience bar would reach the Tier B
// bid, where `salience >= threshold` is unpredictably always-false. (`±.inf`
// are already caught by the range bound: `+inf > 1`, `-inf < 0`.)
func TestLoadConfig_RejectsNaNThreshold(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: alice
        respond: participant
        threshold: .nan
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err, "a NaN threshold must be rejected")
	assert.ErrorIs(t, err, ErrInvalidThreshold)
}

// TestLoadConfig_RejectsThresholdOnNonOpenFloorDisposition pins that a
// per-disposition `threshold` is only meaningful on an open-floor speaker —
// `participant`/`chair`/legacy `always`, all normalizing to RespondAlways.
// The salience bid gates open-floor traffic; a threshold on an `addressed`
// (when_mentioned) or `observer` (never) member — or on the default-disposition
// member that omits `respond` entirely — is a silent no-op. The loader rejects
// it loudly (a cross-field invariant the JSON schema cannot express) rather
// than letting an operator believe a bar is in force where no bid ever runs.
func TestLoadConfig_RejectsThresholdOnNonOpenFloorDisposition(t *testing.T) {
	cases := []struct {
		name    string
		respond string // empty → omit the key (default disposition)
	}{
		{"addressed", "respond: addressed"},
		{"observer", "respond: observer"},
		{"legacy_when_mentioned", "respond: when_mentioned"},
		{"legacy_never", "respond: never"},
		{"default_disposition", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			respondLine := ""
			if tc.respond != "" {
				respondLine = "        " + tc.respond + "\n"
			}
			body := `
channels:
  - name: planning
    members:
      - id: alice
` + respondLine + `        threshold: 0.5
`
			_, err := LoadConfig(writeYAML(t, body))
			require.Error(t, err, "a threshold on %s must be rejected", tc.name)
			assert.ErrorIs(t, err, ErrThresholdNotApplicable)
		})
	}
}

// TestLoadConfig_AcceptsThresholdOnLegacyAlways pins the open-floor positive
// case: the legacy `always` value is the wire form of `participant`/`chair`,
// so a `threshold` on it is the canonical "open-floor speaker with a salience
// bar" and must load (it normalizes to RespondAlways, same as participant).
func TestLoadConfig_AcceptsThresholdOnLegacyAlways(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: alice
        respond: always
        threshold: 0.5
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err, "a threshold on the open-floor `always` must load")
	require.NotNil(t, cfg.Channels[0].Members[0].Threshold)
	assert.InDelta(t, 0.5, *cfg.Channels[0].Members[0].Threshold, 1e-9)
}
