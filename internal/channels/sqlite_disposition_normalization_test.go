package channels

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Store-write normalization for the RFC 0030 relevance-amendment
// disposition vocabulary (`participant`/`addressed`/`observer`).
//
// PR 1 widened [RespondPolicy.Valid] to accept the disposition vocabulary
// and normalized it at the YAML config-load boundary. But the REST API
// write path (`POST /api/v1/channels`, `POST .../members`) constructs a
// RespondPolicy straight from the wire and hands it to the store WITHOUT
// normalizing — and the membership table's CHECK constraint only allows
// the legacy triple. So before this fix, a disposition value submitted via
// REST passed the widened Valid() guard, then hit the DB CHECK and surfaced
// as an opaque non-sentinel error (HTTP 500) instead of either working or
// returning the clean ErrInvalidRespondPolicy (HTTP 400).
//
// These tests pin the store as the SECOND back-compat boundary (mirroring
// the loader): every write path normalizes the disposition vocabulary to
// the legacy triple before validating and persisting, so the canonical
// wire/DB value stays the legacy three for every caller, not just config.

// dispositionNormalizationCases maps each disposition value to the legacy
// policy it must persist as.
var dispositionNormalizationCases = []struct {
	disposition RespondPolicy
	wantLegacy  RespondPolicy
}{
	{RespondParticipant, RespondAlways},
	{RespondAddressed, RespondWhenMentioned},
	{RespondObserver, RespondNever},
	// v0.3.8 Tier B: `chair` is a `participant` with a low default
	// threshold, so it normalizes to the same legacy `always` wire value
	// at every write boundary (the threshold rides on the config struct,
	// not the membership CHECK constraint).
	{RespondChair, RespondAlways},
}

func TestSQLiteStore_AddMember_NormalizesDisposition(t *testing.T) {
	for _, tc := range dispositionNormalizationCases {
		t.Run(string(tc.disposition), func(t *testing.T) {
			store := newTestStore(t, SQLiteOptions{})
			ctx := context.Background()
			id := mustCreateGroup(t, store, "planning")

			require.NoError(t, store.AddMember(ctx, id, "alice", tc.disposition),
				"AddMember must normalize the disposition vocabulary, not pass it through to the CHECK constraint")

			got, err := store.GetMember(ctx, id, "alice")
			require.NoError(t, err)
			assert.Equal(t, tc.wantLegacy, got.RespondPolicy,
				"%s must persist as the legacy %s", tc.disposition, tc.wantLegacy)
		})
	}
}

func TestSQLiteStore_AddMember_RejectsUnknownPolicy(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	id := mustCreateGroup(t, store, "planning")

	// An unknown value must still surface the clean sentinel (HTTP 400),
	// not the DB CHECK error (HTTP 500). Normalize() returns it unchanged,
	// then Valid() rejects it.
	err := store.AddMember(ctx, id, "alice", RespondPolicy("participent")) // typo
	assert.ErrorIs(t, err, ErrInvalidRespondPolicy)
}

func TestSQLiteStore_SetMemberPolicy_NormalizesDisposition(t *testing.T) {
	for _, tc := range dispositionNormalizationCases {
		t.Run(string(tc.disposition), func(t *testing.T) {
			store := newTestStore(t, SQLiteOptions{})
			ctx := context.Background()
			id := mustCreateGroup(t, store, "planning", "alice")

			require.NoError(t, store.SetMemberPolicy(ctx, id, "alice", tc.disposition),
				"SetMemberPolicy must normalize the disposition vocabulary")

			got, err := store.GetMember(ctx, id, "alice")
			require.NoError(t, err)
			assert.Equal(t, tc.wantLegacy, got.RespondPolicy)
		})
	}
}

func TestSQLiteStore_CreateChannelWithMembers_NormalizesDisposition(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	ctx := context.Background()
	ch := Channel{ID: "group:planning", Name: "planning", Type: ChannelTypeGroup}
	members := []Member{
		{ParticipantID: "p", RespondPolicy: RespondParticipant},
		{ParticipantID: "a", RespondPolicy: RespondAddressed},
		{ParticipantID: "o", RespondPolicy: RespondObserver},
	}
	require.NoError(t, store.CreateChannelWithMembers(ctx, ch, members),
		"CreateChannelWithMembers (the REST create path) must normalize disposition members")

	got, err := store.GetMembers(ctx, ch.ID)
	require.NoError(t, err)
	byID := map[string]RespondPolicy{}
	for _, m := range got {
		byID[m.ParticipantID] = m.RespondPolicy
	}
	assert.Equal(t, RespondAlways, byID["p"], "participant → always")
	assert.Equal(t, RespondWhenMentioned, byID["a"], "addressed → when_mentioned")
	assert.Equal(t, RespondNever, byID["o"], "observer → never")
}
