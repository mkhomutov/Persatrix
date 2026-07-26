// RFC 0037 PR 1 (v0.3.12) — loader contract for the two classification
// fields: the per-channel `classification` and the fleet-wide
// `dm_default_classification` knob. Both are DARK in this PR (parsed and
// validated, not yet applied beyond the DM stamp), but the loader contract —
// absent → `internal` (§A rule (a)), unknown → loud rejection — must be
// pinned with the fields so a typo never sits silently in a config file
// waiting for a later PR to make it load-bearing.
package channels

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestLoadConfig_ClassificationAbsent_DefaultsInternal pins rule (a) at the
// load boundary: neither field declared → both read back `internal`.
func TestLoadConfig_ClassificationAbsent_DefaultsInternal(t *testing.T) {
	body := `
channels:
  - name: planning
    members: [alice]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, ClassificationInternal, cfg.DMDefaultClassification,
		"absent dm_default_classification defaults to internal (§A rule (a))")
	require.Len(t, cfg.Channels, 1)
	assert.Equal(t, ClassificationInternal, cfg.Channels[0].Classification,
		"absent per-channel classification defaults to internal (§A rule (a))")
}

// TestLoadConfig_ClassificationDeclared_ParsesVerbatim: declared lattice
// levels load unchanged on both fields (the decoder runs KnownFields(true), so
// this also pins that a schema-valid config file keeps loading at all).
//
// Exhaustive over the vocabulary — including `restricted` and `secret`,
// which the item-8 dark-window guard rejected at load through PRs 1–3.
// That guard was deleted when the §D gate armed (RFC 0037 PR 4), exactly
// as its REMOVAL note prescribed; this widened pin is the replacement:
// every §A level is now a declarable, enforced boundary.
func TestLoadConfig_ClassificationDeclared_ParsesVerbatim(t *testing.T) {
	body := `
dm_default_classification: restricted
channels:
  - name: leadership
    classification: secret
    members: [alice]
  - name: planning
    classification: restricted
    members: [alice]
  - name: engineering
    classification: internal
    members: [alice]
  - name: townsquare
    classification: public
    members: [alice]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, ClassificationRestricted, cfg.DMDefaultClassification,
		"above-internal DM default loads clean now that the §D gate is live")
	require.Len(t, cfg.Channels, 4)
	assert.Equal(t, ClassificationSecret, cfg.Channels[0].Classification)
	assert.Equal(t, ClassificationRestricted, cfg.Channels[1].Classification)
	assert.Equal(t, ClassificationInternal, cfg.Channels[2].Classification)
	assert.Equal(t, ClassificationPublic, cfg.Channels[3].Classification)
}

// TestLoadConfig_UnknownChannelClassification_Rejected: an out-of-vocabulary
// per-channel level is an operator typo the loader rejects loudly — it must
// NOT be silently coerced to internal (the schema enum catches it at `make
// validate`; this is the belt-and-suspenders for skippers).
func TestLoadConfig_UnknownChannelClassification_Rejected(t *testing.T) {
	body := `
channels:
  - name: planning
    classification: confidential
    members: [alice]
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidClassification)
	assert.Contains(t, err.Error(), "confidential",
		"the rejection names the bad value for operator triage")
}

// TestLoadConfig_UnknownDMDefaultClassification_Rejected: same posture for
// the fleet-wide DM knob.
func TestLoadConfig_UnknownDMDefaultClassification_Rejected(t *testing.T) {
	body := `
dm_default_classification: top-secret
channels:
  - name: planning
    members: [alice]
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidClassification)
}

// TestValidate_HandBuiltConfig_EmptyClassificationAccepted: a Config built in
// code without LoadConfig (fixtures, embedding callers) validates with the
// zero-value fields — empty means "the default", matching every other knob's
// zero-value posture, and the stamp-side [NormalizeForStamp] resolves it at
// the write boundary.
func TestValidate_HandBuiltConfig_EmptyClassificationAccepted(t *testing.T) {
	cfg := &Config{
		MaxChannels: DefaultMaxChannels,
		Channels: []ChannelConfig{{
			Name:                      "planning",
			FloorTurnTimeoutSeconds:   DefaultFloorTurnTimeoutSeconds,
			SalienceMaxChannelMembers: DefaultSalienceMaxChannelMembers,
			EndVoteThreshold:          DefaultEndVoteThreshold,
			EndVoteWindow:             DefaultEndVoteWindow,
			Members: []MemberConfig{
				{ID: "alice", RespondPolicy: RespondAlways},
			},
		}},
	}
	assert.NoError(t, cfg.Validate(),
		"zero-value classification fields are the inherit-default case, not an error")
}
