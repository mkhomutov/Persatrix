package server

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// ISSUE-0209 at the config door. A channel's escalation chair and its
// autonomous convener are participant ids, and the config PATCH is the one
// route that writes them. A value the PATCH newly sets must fit the participant
// id rule. A value the channel already holds is left alone: a store from
// before the rule can name a member such as `User_1` as chair, and that
// channel's other settings must stay editable.

// looseMemberConfigServer builds a server with config editing on and one
// channel, group:ops, as an orchestrator before ISSUE-0209 left it: its
// members are User_1, Ops_2, alice and ember-owl, all of whom answer. The
// loose rows are written with plain SQL, since no route takes them.
func looseMemberConfigServer(t *testing.T) (*Server, string) {
	t.Helper()
	const id = "group:ops"
	path := filepath.Join(t.TempDir(), "channels.db")
	first, err := channels.NewSQLiteStore(path, channels.SQLiteOptions{MaxChannels: 50})
	require.NoError(t, err)
	require.NoError(t, first.Close())

	db, err := sql.Open("sqlite", path)
	require.NoError(t, err)
	at := time.Date(2026, 11, 1, 9, 0, 0, 0, time.UTC)
	_, err = db.Exec(`INSERT INTO channels (id, name, channel_type, description, created_at) VALUES (?, 'ops', 'group', '', ?)`, id, at)
	require.NoError(t, err)
	for _, member := range []string{"User_1", "Ops_2", "alice", "ember-owl"} {
		_, err = db.Exec(`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at) VALUES (?, ?, 'always', ?)`,
			id, member, at)
		require.NoError(t, err)
	}
	_, err = db.Exec(`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
		SELECT channel_id, participant_id, joined_at, NULL FROM memberships`)
	require.NoError(t, err)
	require.NoError(t, db.Close())

	store, err := channels.NewSQLiteStore(path, channels.SQLiteOptions{MaxChannels: 50, Logger: zap.NewNop()})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	logger := zap.NewNop()
	cfg := DefaultUIConfig()
	cfg.Panels["channel_timeline"] = PanelToggle{Enabled: true, CreateEnabled: true, ConfigEditEnabled: true}
	srv, err := New("127.0.0.1:0", t.TempDir(), state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger), planner.NewYAMLPlanner(logger), logger,
		WithChannels(store, channels.NewChannelRouter(store, channels.NoopDispatcher{}, logger, nil)),
		WithUIConfig(cfg))
	require.NoError(t, err)
	srv.applyRuntimeGroupGovernance(t.Context(), id)
	return srv, id
}

// patchConfig sends one config PATCH and returns the status and the body.
func patchConfig(t *testing.T, srv *Server, id string, revision int64, body map[string]any) (int, string) {
	t.Helper()
	raw, err := json.Marshal(body)
	require.NoError(t, err)
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPatch, "/api/v1/channels/"+id+"/config",
		raw, map[string]string{"If-Match": strconv.FormatInt(revision, 10)})
	return rec.Code, rec.Body.String()
}

// storedConfig reads what the store holds for the channel.
func storedConfig(t *testing.T, srv *Server, id string) (channels.ChannelConfigOverrides, int64) {
	t.Helper()
	overrides, revision, err := srv.channelStore.GetChannelConfig(context.Background(), id)
	require.NoError(t, err)
	return overrides, revision
}

func TestChannelConfig_ALooseIDIsRefusedAsANewChairOrConvener(t *testing.T) {
	tests := []struct {
		name string
		body map[string]any
	}{
		{
			name: "the chair",
			body: map[string]any{"escalation_chair_id": "User_1"},
		},
		{
			name: "the convener of an armed channel",
			body: map[string]any{
				"interaction_budget_tokens": 200000,
				"escalation_chair_id":       "ember-owl",
				"autonomous":                map[string]any{"enabled": true, "convener": "User_1"},
			},
		},
		{
			name: "the convener of a channel that is not armed",
			body: map[string]any{"autonomous": map[string]any{"convener": "User_1"}},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			srv, id := looseMemberConfigServer(t)

			code, body := patchConfig(t, srv, id, 0, tt.body)

			requireRefusedByTheRule(t, code, body, "User_1")
			_, revision := storedConfig(t, srv, id)
			assert.Equal(t, int64(0), revision, "nothing was written")
		})
	}
}

// The control for the test above: the same three edits with a member whose
// id fits the rule are taken, so a refusal there is the id's doing.
func TestChannelConfig_AnIDThatFitsTheRuleIsTakenAsChairOrConvener(t *testing.T) {
	for name, body := range map[string]map[string]any{
		"the chair": {"escalation_chair_id": "alice"},
		"the convener of an armed channel": {
			"interaction_budget_tokens": 200000,
			"escalation_chair_id":       "ember-owl",
			"autonomous":                map[string]any{"enabled": true, "convener": "alice"},
		},
		"the convener of a channel that is not armed": {"autonomous": map[string]any{"convener": "alice"}},
	} {
		t.Run(name, func(t *testing.T) {
			srv, id := looseMemberConfigServer(t)

			code, respBody := patchConfig(t, srv, id, 0, body)

			require.Equal(t, http.StatusOK, code, "body=%s", respBody)
		})
	}
}

// A channel whose stored config already names a loose id as chair and as
// convener keeps working: its other settings can be edited, the same values
// can be sent again, and each can be pointed at a member whose id fits the
// rule. Once re-pointed, the loose id is a new value again and is refused.
func TestChannelConfig_AStoredLooseChairAndConvenerKeepTheChannelEditable(t *testing.T) {
	srv, id := looseMemberConfigServer(t)
	chair, convener, armed, budget := "User_1", "Ops_2", true, int64(200000)
	// What an orchestrator before ISSUE-0209 could store: written below the
	// REST layer, where the earlier rule let it through.
	require.NoError(t, srv.channelRouter.ApplyChannelConfig(t.Context(), id, channels.ChannelConfigOverrides{
		EscalationChairID:       &chair,
		InteractionBudgetTokens: &budget,
		Autonomous:              &channels.AutonomousOverrides{Enabled: &armed, Convener: &convener},
	}, 0, ""))

	code, body := patchConfig(t, srv, id, 1, map[string]any{"interaction_idle_timeout_seconds": 60})
	require.Equal(t, http.StatusOK, code, "an unrelated setting is edited: %s", body)

	code, body = patchConfig(t, srv, id, 2, map[string]any{
		"escalation_chair_id": "User_1",
		"autonomous":          map[string]any{"convener": "Ops_2", "max_rounds": 4},
	})
	require.Equal(t, http.StatusOK, code, "the values the channel already holds are not new: %s", body)
	stored, revision := storedConfig(t, srv, id)
	require.Equal(t, int64(3), revision)
	require.NotNil(t, stored.EscalationChairID)
	assert.Equal(t, "User_1", *stored.EscalationChairID, "the chair is as it was")
	require.NotNil(t, stored.Autonomous)
	require.NotNil(t, stored.Autonomous.Convener)
	assert.Equal(t, "Ops_2", *stored.Autonomous.Convener, "and so is the convener")

	code, body = patchConfig(t, srv, id, 3, map[string]any{
		"escalation_chair_id": "ember-owl",
		"autonomous":          map[string]any{"convener": "alice"},
	})
	require.Equal(t, http.StatusOK, code, "both are pointed at members whose ids fit the rule: %s", body)

	code, body = patchConfig(t, srv, id, 4, map[string]any{"escalation_chair_id": "User_1"})
	requireRefusedByTheRule(t, code, body, "User_1")
	code, body = patchConfig(t, srv, id, 4, map[string]any{"autonomous": map[string]any{"convener": "Ops_2"}})
	requireRefusedByTheRule(t, code, body, "Ops_2")
	_, revision = storedConfig(t, srv, id)
	assert.Equal(t, int64(4), revision, "neither refused edit was written")
}

// A chair that only the channels file names (the router holds it, the store
// has no config yet) is carried into the first edit as it is: the edit names
// another setting, so no participant id is newly set.
func TestChannelConfig_AFirstEditKeepsALooseChairFromTheChannelsFile(t *testing.T) {
	srv, id := looseMemberConfigServer(t)
	srv.channelRouter.SetEscalationChair(id, "User_1")

	code, body := patchConfig(t, srv, id, 0, map[string]any{"interaction_idle_timeout_seconds": 60})

	require.Equal(t, http.StatusOK, code, "body=%s", body)
	stored, _ := storedConfig(t, srv, id)
	require.NotNil(t, stored.EscalationChairID)
	assert.Equal(t, "User_1", *stored.EscalationChairID)
}

// An empty value is not an id: it clears the knob, as it did before the rule
// was applied here.
func TestChannelConfig_AnEmptyChairOrConvenerStillClearsIt(t *testing.T) {
	srv, id := looseMemberConfigServer(t)
	code, body := patchConfig(t, srv, id, 0, map[string]any{
		"escalation_chair_id": "ember-owl",
		"autonomous":          map[string]any{"convener": "alice"},
	})
	require.Equal(t, http.StatusOK, code, "body=%s", body)

	code, body = patchConfig(t, srv, id, 1, map[string]any{
		"escalation_chair_id": "",
		"autonomous":          map[string]any{"convener": ""},
	})

	require.Equal(t, http.StatusOK, code, "body=%s", body)
	_, fields := decodeConfig(t, []byte(body))
	assert.Equal(t, "", fields["escalation_chair_id"].Value, "the chair is cleared")
	assert.Equal(t, "", decodeAutonomous(t, []byte(body))["convener"].Value, "and so is the convener")
}
