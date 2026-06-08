package channels

import (
	"strconv"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// RFC 0030 Layer 4 (v0.3.8): the per-channel end-of-interaction vote knobs
// (`end_vote_threshold` K, `end_vote_window` W). Like the floor-turn timeout
// and the salience cap — and unlike the cost ceiling / reply budget — these
// normalize a zero/absent value to a non-zero default at load (K=2, W=3): a
// zero threshold or window is not a meaningful "uncapped" value the way a zero
// budget is, so the layer always reads a populated K/W. The layer is still
// opt-in: with no producer emitting `END_INTERACTION_VOTE`, a populated K/W
// never fires.

// TestLoadConfig_DefaultEndVoteThresholdWindow pins that absent end-vote knobs
// normalize to the K=2 / W=3 defaults at load.
func TestLoadConfig_DefaultEndVoteThresholdWindow(t *testing.T) {
	body := `
channels:
  - name: planning
    members:
      - id: alice
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, DefaultEndVoteThreshold, cfg.Channels[0].EndVoteThreshold,
		"absent end_vote_threshold normalizes to the default")
	assert.Equal(t, DefaultEndVoteWindow, cfg.Channels[0].EndVoteWindow,
		"absent end_vote_window normalizes to the default")
}

// TestLoadConfig_AcceptsEndVoteThresholdWindow pins that explicit K/W load.
func TestLoadConfig_AcceptsEndVoteThresholdWindow(t *testing.T) {
	body := `
channels:
  - name: planning
    end_vote_threshold: 3
    end_vote_window: 5
    members:
      - id: alice
        respond: participant
`
	cfg, err := LoadConfig(writeYAML(t, body))
	require.NoError(t, err)
	assert.Equal(t, 3, cfg.Channels[0].EndVoteThreshold)
	assert.Equal(t, 5, cfg.Channels[0].EndVoteWindow)
}

// TestLoadConfig_RejectsNegativeEndVoteThreshold pins the loud-failure
// belt-and-suspenders for an operator who skipped `make validate`.
func TestLoadConfig_RejectsNegativeEndVoteThreshold(t *testing.T) {
	body := `
channels:
  - name: planning
    end_vote_threshold: ` + strconv.Itoa(-1) + `
    members:
      - id: alice
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidEndVoteThreshold)
}

// TestLoadConfig_RejectsNegativeEndVoteWindow pins the same for the window.
func TestLoadConfig_RejectsNegativeEndVoteWindow(t *testing.T) {
	body := `
channels:
  - name: planning
    end_vote_window: ` + strconv.Itoa(-2) + `
    members:
      - id: alice
        respond: participant
`
	_, err := LoadConfig(writeYAML(t, body))
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidEndVoteWindow)
}
