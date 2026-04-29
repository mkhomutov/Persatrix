package channels

import (
	"path/filepath"
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

func TestChannelConfig_CanonicalID(t *testing.T) {
	cc := ChannelConfig{Name: "planning"}
	assert.Equal(t, "group:planning", cc.CanonicalID())
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
