package channels

// ISSUE-0158 — the RFC 0037 §F recall filter gains the audience condition §D
// has: with an acting channel bound, a message is recallable only when every
// current member of the ACTING channel is also a member of the message's
// channel (the acting room adds nobody the source room did not hold — the
// same comparison agents/persona_runtime/audience.py makes for injection).
// Found live at the v0.3.16 release-prep arc: a DM's transcript came back to a
// group-room turn the injection gate had just withheld those facts from.

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

const (
	audienceDM       = "dm:alice:ember-owl"
	audiencePlanning = "group:planning"
	audiencePair     = "group:pair"
)

// audienceRecallFixture seeds three internal rooms the persona is in:
// Alice's DM (alice, ember-owl), planning (alice, bob, ember-owl) and a pair
// room with exactly the DM's members. One message in the DM, one in planning.
func audienceRecallFixture(t *testing.T) ChannelStore {
	t.Helper()
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	rooms := map[string][]string{
		audienceDM:       {"alice", "ember-owl"},
		audiencePlanning: {"alice", "bob", "ember-owl"},
		audiencePair:     {"alice", "ember-owl"},
	}
	names := map[string]string{audienceDM: audienceDM, audiencePlanning: "planning", audiencePair: "pair"}
	for id := range rooms {
		typ := ChannelTypeGroup
		if id == audienceDM {
			typ = ChannelTypeDM
		}
		require.NoError(t, store.CreateChannel(ctx, Channel{
			ID: id, Name: names[id], Type: typ, Classification: ClassificationInternal,
		}))
	}
	withDB(t, path, func(db *sql.DB) {
		for id, members := range rooms {
			for _, m := range members {
				_, err := db.Exec(
					`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
					 VALUES (?, ?, 'when_mentioned', ?)`, id, m, mins(0))
				require.NoError(t, err)
			}
			seedInterval(t, db, id, "ember-owl", mins(0), nil)
		}
		seedMsg(t, db, msgSeed{id: "m-dm", channelID: audienceDM, sender: "alice",
			content: "the helix rollout is paused", ts: mins(5)})
		seedMsg(t, db, msgSeed{id: "m-planning", channelID: audiencePlanning, sender: "alice",
			content: "where did the helix rollout land", ts: mins(6)})
	})
	return store
}

func TestRecallMessages_AudienceScope(t *testing.T) {
	store := audienceRecallFixture(t)
	ctx := context.Background()
	cases := []struct {
		name, acting string
		want         []string
	}{
		// planning adds bob, whom the DM never held → the DM's message is out.
		{"group room excludes a DM it is not a subset of", audiencePlanning, []string{"m-planning"}},
		// the pair room's members were all in the DM → the DM's message is in;
		// planning's members ⊇ pair's → planning's message is in too.
		{"a room whose members were all in the source admits", audiencePair, []string{"m-dm", "m-planning"}},
		// acting in the DM itself: its own message, and planning's (the DM adds
		// nobody planning did not hold).
		{"the source room admits itself", audienceDM, []string{"m-dm", "m-planning"}},
		// no acting channel bound (a channel-less turn, or the knob not live):
		// the filter is not applied — the additive contract.
		{"absent acting channel leaves recall unchanged", "", []string{"m-dm", "m-planning"}},
		// an acting id with no members resolves to an empty acting set, which
		// every source trivially contains: unknown admits, as live does.
		{"unknown acting channel admits", "group:nowhere", []string{"m-dm", "m-planning"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := store.RecallMessages(ctx, RecallParams{
				ParticipantID: "ember-owl", Query: "helix",
				ActingClassification: ClassificationInternal,
				ActingChannelID:      tc.acting,
			})
			require.NoError(t, err)
			assert.ElementsMatch(t, tc.want, idSlice(got))
		})
	}
}

func TestRecallMessages_AudienceScope_LikeFallback(t *testing.T) {
	// A query that sanitises to no FTS term takes the LIKE path; the audience
	// clause rides the shared scope, so it must hold there too.
	store := audienceRecallFixture(t)
	got, err := store.RecallMessages(context.Background(), RecallParams{
		ParticipantID: "ember-owl", Query: "*",
		ActingClassification: ClassificationInternal,
		ActingChannelID:      audiencePlanning,
	})
	require.NoError(t, err)
	assert.NotContains(t, idSlice(got), "m-dm")
}
