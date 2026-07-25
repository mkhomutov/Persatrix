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
// Only levels at or below the dark-window ceiling appear here — `restricted`
// and `secret` are rejected by [CheckDarkWindowClassification] until the §D
// gate ships (see TestLoadConfig_ClassificationAboveDarkWindow_Rejected). When
// that guard is removed at RFC 0037 PR 4, widen this back over the full
// vocabulary so the parse path keeps its exhaustive pin.
func TestLoadConfig_ClassificationDeclared_ParsesVerbatim(t *testing.T) {
	body := `
dm_default_classification: internal
channels:
  - name: leadership
    classification: internal
    members: [alice]
  - name: townsquare
    classification: public
    members: [alice]
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, ClassificationInternal, cfg.DMDefaultClassification)
	require.Len(t, cfg.Channels, 2)
	assert.Equal(t, ClassificationInternal, cfg.Channels[0].Classification)
	assert.Equal(t, ClassificationPublic, cfg.Channels[1].Classification)
}

// TestLoadConfig_ClassificationAboveDarkWindow_Rejected pins the item-8
// dark-window rule as an ENFORCED ceiling, not a documented request: a level
// above `internal` is refused at load on both declaration surfaces while the
// §D hard gate (PR 4) and §F recall filter (PR 5) are missing.
//
// The failure this closes is worse than an unclassified channel: in PR 1 a
// declared group classification never reaches the store row at all, so an
// operator reading the schema and writing `classification: restricted` would
// get a config that loads clean, a row that still says `internal`, and no gate
// on either side — a confidentiality boundary that exists only in their head.
//
// TEMPORARY: delete this test with the guard at PR 4.
func TestLoadConfig_ClassificationAboveDarkWindow_Rejected(t *testing.T) {
	for _, level := range []Classification{ClassificationRestricted, ClassificationSecret} {
		t.Run("channel/"+string(level), func(t *testing.T) {
			body := `
channels:
  - name: leadership
    classification: ` + string(level) + `
    members: [alice]
`
			_, err := LoadConfig(writeYAML(t, body))
			require.Error(t, err)
			assert.ErrorIs(t, err, ErrClassificationAboveDarkWindow)
			assert.NotErrorIs(t, err, ErrInvalidClassification,
				"a lattice-valid level declared too early is a timing error, not a typo")
			assert.Contains(t, err.Error(), "leadership",
				"the rejection names the channel for operator triage")
			assert.Contains(t, err.Error(), string(level))
		})

		t.Run("dm_default/"+string(level), func(t *testing.T) {
			body := `
dm_default_classification: ` + string(level) + `
channels:
  - name: planning
    members: [alice]
`
			_, err := LoadConfig(writeYAML(t, body))
			require.Error(t, err)
			assert.ErrorIs(t, err, ErrClassificationAboveDarkWindow)
			assert.Contains(t, err.Error(), "dm_default_classification")
		})
	}
}

// TestCheckDarkWindowClassification_ComposesWithVocabularyCheck: the ceiling
// and the vocabulary check must not swallow each other. An unknown level is
// [ErrInvalidClassification]'s alone (the guard passes it through), and the
// ceiling admits everything at or below `internal` including the absent case.
func TestCheckDarkWindowClassification_ComposesWithVocabularyCheck(t *testing.T) {
	assert.NoError(t, CheckDarkWindowClassification(""),
		"absent is the default, not a declaration")
	assert.NoError(t, CheckDarkWindowClassification(ClassificationPublic))
	assert.NoError(t, CheckDarkWindowClassification(DarkWindowMaxClassification))
	for _, unknown := range unknownLevels {
		assert.NoError(t, CheckDarkWindowClassification(unknown),
			"unknown %q belongs to the vocabulary check, not the ceiling", unknown)
	}
	assert.Error(t, CheckDarkWindowClassification(ClassificationSecret))
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
