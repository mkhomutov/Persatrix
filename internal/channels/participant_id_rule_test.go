package channels

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

// ISSUE-0209: one rule for a participant id.
//
// This package used to accept `User_1`, `alice_b`, `a`, `Bob` and `ops-`,
// which the agents' own check refuses. A message from such a member was
// stored, sent to every persona, and refused by each: the member got no
// reply and nothing told them why. The rule is now the agents' rule, held
// where an id enters. What an earlier orchestrator already stored under the
// looser rule stays listed and readable.

// looseIDs are the five ids of the issue: each fits the rule this package
// held before, and none fits the participant id rule.
var looseIDs = []string{"User_1", "alice_b", "a", "Bob", "ops-"}

const participantIDRuleText = "^[a-z0-9][a-z0-9-]*[a-z0-9]$"

func TestValidateParticipantID_IsTheOneRule(t *testing.T) {
	tests := []struct {
		id   string
		want bool
	}{
		{"ember-owl", true},
		{"user-1", true},
		{"a1", true},
		{"0a", true},
		{"ab", true},
		{"User_1", false},  // a capital and an underscore
		{"alice_b", false}, // an underscore
		{"a", false},       // one character
		{"Bob", false},     // a capital
		{"ops-", false},    // ends with a hyphen
		{"-ops", false},
		{"", false},
		{"a:b", false},
		{"a b", false},
		{MentionEveryone, false},
	}
	for _, tt := range tests {
		t.Run(tt.id, func(t *testing.T) {
			err := ValidateParticipantID(tt.id)
			if tt.want {
				require.NoError(t, err)
				assert.False(t, IsLegacyParticipantID(tt.id))
				return
			}
			require.ErrorIs(t, err, ErrInvalidParticipantID)
			if tt.id != "" {
				assert.Contains(t, err.Error(), participantIDRuleText, "the message names the rule")
				assert.Contains(t, err.Error(), "lowercase letters, digits and hyphens, at least two characters",
					"and says it in words")
			}
		})
	}
}

func TestIsLegacyParticipantID(t *testing.T) {
	for _, id := range looseIDs {
		assert.True(t, IsLegacyParticipantID(id), "%q fits only the earlier rule", id)
	}
	for _, id := range []string{"ember-owl", "a1", "", "a:b", "a b", MentionEveryone, "-ops", "_ops"} {
		assert.False(t, IsLegacyParticipantID(id), "%q", id)
	}
}

// Every write that carries a loose id is refused, and stores nothing.
func TestStoreWrites_RefuseALooseParticipantID(t *testing.T) {
	ctx := context.Background()
	for _, loose := range looseIDs {
		t.Run(loose, func(t *testing.T) {
			store := newTestStore(t, SQLiteOptions{})
			group := mustCreateGroup(t, store, "planning", "alice", "ember-owl")

			writes := map[string]func() error{
				"a message from it": func() error {
					return store.PublishMessage(ctx, ChannelMessage{ID: "m1", ChannelID: group, SenderID: loose, Content: "hi"})
				},
				"a message that mentions it": func() error {
					return store.PublishMessage(ctx, ChannelMessage{
						ID: "m2", ChannelID: group, SenderID: "alice", Content: "hi", Mentions: []string{"ember-owl", loose},
					})
				},
				"adding it as a member": func() error { return store.AddMember(ctx, group, loose, RespondNever) },
				"a new channel with it as a member": func() error {
					return store.CreateChannelWithMembers(ctx,
						Channel{ID: "group:other", Name: "other", Type: ChannelTypeGroup},
						[]Member{{ParticipantID: "alice"}, {ParticipantID: loose}})
				},
				"a new DM with it": func() error {
					_, err := store.GetOrCreateDM(ctx, loose, "ember-owl")
					return err
				},
			}
			for name, write := range writes {
				err := write()
				require.ErrorIs(t, err, ErrInvalidParticipantID, name)
				assert.Contains(t, err.Error(), participantIDRuleText, "%s: the message names the rule", name)
			}

			history, err := store.GetHistory(ctx, group, 10, time.Time{})
			require.NoError(t, err)
			assert.Empty(t, history, "no refused message was stored")
			members, err := store.GetMembers(ctx, group)
			require.NoError(t, err)
			assert.Len(t, members, 2, "no refused member was stored")
			_, err = store.GetChannel(ctx, "group:other")
			assert.ErrorIs(t, err, ErrChannelNotFound, "no channel was created for the refused member")
			_, err = store.LookupDM(ctx, loose, "ember-owl")
			assert.ErrorIs(t, err, ErrChannelNotFound, "no DM was created")
		})
	}
}

// earlierStore makes a store as an orchestrator before ISSUE-0209 left it:
// group:ops with the member User_1 and one message from it, and the DM
// between User_1 and ember-owl with one message. The rows are written with
// plain SQL, since no write path of this package takes such an id any more.
// It returns the store's path, closed, for the test to open.
func earlierStore(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	require.NoError(t, store.Close())

	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	defer db.Close() //nolint:errcheck
	at := time.Date(2026, 11, 1, 9, 0, 0, 0, time.UTC)
	for _, stmt := range []struct {
		sql  string
		args []any
	}{
		{`INSERT INTO channels (id, name, channel_type, description, created_at) VALUES (?, ?, 'group', '', ?)`,
			[]any{"group:ops", "ops", at}},
		{`INSERT INTO channels (id, name, channel_type, description, created_at) VALUES (?, ?, 'dm', '', ?)`,
			[]any{"dm:User_1:ember-owl", "dm:User_1:ember-owl", at}},
		{`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at) VALUES (?, ?, 'never', ?)`,
			[]any{"group:ops", "User_1", at}},
		{`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at) VALUES (?, ?, 'always', ?)`,
			[]any{"group:ops", "ember-owl", at}},
		{`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at) VALUES (?, ?, 'always', ?)`,
			[]any{"dm:User_1:ember-owl", "User_1", at}},
		{`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at) VALUES (?, ?, 'always', ?)`,
			[]any{"dm:User_1:ember-owl", "ember-owl", at}},
		// The ledger of who was a member when, one open stint for each row
		// above, as AddMember writes it.
		{`INSERT INTO membership_intervals (channel_id, participant_id, joined_at, left_at)
			SELECT channel_id, participant_id, joined_at, NULL FROM memberships`, nil},
		{`INSERT INTO messages (id, channel_id, sender_id, content, timestamp) VALUES (?, ?, ?, ?, ?)`,
			[]any{"old-1", "group:ops", "User_1", "can anyone hear me?", at.Add(time.Minute)}},
		{`INSERT INTO messages (id, channel_id, sender_id, content, timestamp) VALUES (?, ?, ?, ?, ?)`,
			[]any{"old-2", "dm:User_1:ember-owl", "User_1", "hello", at.Add(2 * time.Minute)}},
	} {
		_, err := db.Exec(stmt.sql, stmt.args...)
		require.NoError(t, err, stmt.sql)
	}
	return path
}

func openEarlierStore(t *testing.T, logger *zap.Logger) ChannelStore {
	t.Helper()
	store, err := NewSQLiteStore(earlierStore(t), SQLiteOptions{Logger: logger})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	return store
}

// Nothing stored becomes unreadable: a channel, a DM and a history that hold
// a loose id are listed and read as before.
func TestStoredLooseParticipantID_StaysReadable(t *testing.T) {
	ctx := context.Background()
	store := openEarlierStore(t, nil)

	channels, err := store.ListChannels(ctx, 100, "")
	require.NoError(t, err)
	var ids []string
	for _, ch := range channels {
		ids = append(ids, ch.ID)
	}
	assert.ElementsMatch(t, []string{"group:ops", "dm:User_1:ember-owl"}, ids)

	members, err := store.GetMembers(ctx, "group:ops")
	require.NoError(t, err)
	var memberIDs []string
	for _, m := range members {
		memberIDs = append(memberIDs, m.ParticipantID)
	}
	assert.ElementsMatch(t, []string{"User_1", "ember-owl"}, memberIDs)

	history, err := store.GetHistory(ctx, "group:ops", 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, history, 1)
	assert.Equal(t, "User_1", history[0].SenderID)

	dm, err := store.LookupDM(ctx, "ember-owl", "User_1")
	require.NoError(t, err, "the DM of a stored loose id is still found")
	assert.Equal(t, "dm:User_1:ember-owl", dm.ID)
	dmHistory, err := store.GetHistory(ctx, dm.ID, 10, time.Time{})
	require.NoError(t, err)
	assert.Len(t, dmHistory, 1)

	found, err := store.GetOrCreateDM(ctx, "User_1", "ember-owl")
	require.NoError(t, err, "a DM that exists is returned: nothing is created")
	assert.Equal(t, dm.ID, found.ID)
}

// Only new writes are refused: the stored member cannot post or be added
// again, and can be removed, which is the first half of renaming it.
func TestStoredLooseParticipantID_NewWritesAreRefused(t *testing.T) {
	ctx := context.Background()
	store := openEarlierStore(t, nil)

	err := store.PublishMessage(ctx, ChannelMessage{ID: "new-1", ChannelID: "group:ops", SenderID: "User_1", Content: "still here"})
	require.ErrorIs(t, err, ErrInvalidParticipantID, "a stored member with a loose id cannot post")
	err = store.PublishMessage(ctx, ChannelMessage{ID: "new-2", ChannelID: "dm:User_1:ember-owl", SenderID: "User_1", Content: "hello again"})
	require.ErrorIs(t, err, ErrInvalidParticipantID, "nor in its DM")

	require.NoError(t, store.RemoveMember(ctx, "group:ops", "User_1"), "it can be removed")
	require.ErrorIs(t, store.AddMember(ctx, "group:ops", "User_1", RespondNever), ErrInvalidParticipantID,
		"and not added again under the same id")
	require.NoError(t, store.AddMember(ctx, "group:ops", "user-1", RespondNever), "but under an id that fits the rule")
}

// The store says once, when it opens, which members the rule refuses.
func TestOpeningAStore_WarnsOnceOfStoredLooseMemberIDs(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)

	openEarlierStore(t, zap.New(core))

	warnings := logs.FilterMessage(legacyMemberIDsWarning).All()
	require.Len(t, warnings, 1, "one warning, however many members and channels")
	fields := warnings[0].ContextMap()
	assert.EqualValues(t, 1, fields["count"])
	assert.Equal(t, []any{"User_1 (2)"}, fields["members"], "each id, with the number of channels it is a member of")
	assert.EqualValues(t, 0, fields["not_named"])
	assert.Equal(t, participantIDRuleText, fields["rule"])
	assert.Contains(t, warnings[0].Message, "they stay listed and readable")
	assert.Contains(t, warnings[0].Message, "remove the member and add it under an id that fits the rule")
}

func TestOpeningAStore_WithOnlyRuleIDsWarnsOfNothing(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	mustCreateGroup(t, store, "planning", "alice", "ember-owl", "user-1")
	require.NoError(t, store.Close())

	reopened, err := NewSQLiteStore(path, SQLiteOptions{Logger: zap.New(core)})
	require.NoError(t, err)
	t.Cleanup(func() { _ = reopened.Close() })

	assert.Empty(t, logs.FilterMessage(legacyMemberIDsWarning).All())
}

// A store with more loose ids than one line should hold names the first
// maxLegacyIDsNamed and counts the rest.
func TestWarnLegacyMembers_BoundsTheNamesInOneLine(t *testing.T) {
	path := earlierStore(t)
	db, err := sql.Open("sqlite", buildDSN(path))
	require.NoError(t, err)
	defer db.Close() //nolint:errcheck
	at := time.Date(2026, 11, 1, 9, 0, 0, 0, time.UTC)
	for i := 0; i < maxLegacyIDsNamed+7; i++ {
		_, err := db.Exec(`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at) VALUES ('group:ops', ?, 'never', ?)`,
			fmt.Sprintf("Member_%d", i), at)
		require.NoError(t, err)
	}
	core, logs := observer.New(zap.WarnLevel)

	warnLegacyMembers(context.Background(), db, zap.New(core))

	warnings := logs.FilterMessage(legacyMemberIDsWarning).All()
	require.Len(t, warnings, 1)
	fields := warnings[0].ContextMap()
	assert.EqualValues(t, maxLegacyIDsNamed+8, fields["count"], "every one is counted")
	assert.Len(t, fields["members"], maxLegacyIDsNamed)
	assert.EqualValues(t, 8, fields["not_named"])
}

// The config loader reads what may already be stored, so a declared member
// with a loose id still loads. Whether it is a write is settled at the
// reconcile, where the store is known.
func TestConfig_ADeclaredLooseMemberID(t *testing.T) {
	declared := func(memberID string) *Config {
		return &Config{MaxChannels: 10, Channels: []ChannelConfig{{
			Name: "ops",
			Members: []MemberConfig{
				{ID: memberID, RespondPolicy: RespondNever},
				{ID: "ember-owl", RespondPolicy: RespondAlways},
			},
		}}}
	}

	t.Run("loads, for each id of the earlier rule", func(t *testing.T) {
		for _, loose := range looseIDs {
			require.NoError(t, declared(loose).Validate(), loose)
		}
	})

	t.Run("an id outside both rules is still refused by the loader", func(t *testing.T) {
		for _, bad := range []string{"a:b", "a b", "", "@ops"} {
			assert.ErrorIs(t, declared(bad).Validate(), ErrInvalidParticipantID, "%q", bad)
		}
	})

	t.Run("a channel already stored with that member reconciles", func(t *testing.T) {
		store := openEarlierStore(t, nil)
		router := NewChannelRouter(store, NoopDispatcher{}, zap.NewNop(), nil)

		require.NoError(t, router.ReconcileConfig(context.Background(), declared("User_1")))
	})

	t.Run("a channel the store lacks is not created with that member", func(t *testing.T) {
		store := newTestStore(t, SQLiteOptions{})
		router := NewChannelRouter(store, NoopDispatcher{}, zap.NewNop(), nil)

		err := router.ReconcileConfig(context.Background(), declared("User_1"))

		require.ErrorIs(t, err, ErrInvalidParticipantID)
		assert.Contains(t, err.Error(), participantIDRuleText)
		_, getErr := store.GetChannel(context.Background(), "group:ops")
		assert.ErrorIs(t, getErr, ErrChannelNotFound, "nothing of the refused channel is left behind")
	})
}
