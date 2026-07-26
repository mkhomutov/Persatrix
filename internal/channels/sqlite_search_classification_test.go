// RFC 0037 PR 5 — the §F recall classification filter: recall is capped at the
// ACTING channel's confidentiality level, composed with (never replacing) the
// RFC 0036 membership scope. Split from sqlite_search_test.go (which holds the
// scope/epoch/narrowing/ranking contracts and the shared fixtures) to keep each
// file under the repo's 500-line cap.
//
// These tests pin the store-level §F contract with no endpoint or persona in
// the loop:
//
//   - The cap: a `secret`-channel message is excluded acting-`public` /
//     `internal` / `restricted` and included acting-`secret` — the full
//     lattice matrix, asserted against [InjectableLevels] so SQL and the Go
//     resolver cannot drift.
//   - Composition: acting-`secret` never WIDENS scope — a message in a channel
//     the participant was never a member of stays excluded at every level.
//   - Rule (b): an unset/unknown acting level floors to `public` (the
//     least-disclosing set), never the `internal` stamp default.
//   - Rule (c): a corrupted channel label falls out of the IN-set and is
//     withheld at EVERY acting level; a missing channel row likewise fails the
//     EXISTS. Both are the SQL realisation of "unknown → withheld".
//   - The LIKE fallback applies the identical cap (the §F clause rides the
//     scope fragment both paths share verbatim).
package channels

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// classifiedRecallFixture creates one channel per §A level plus messages, with
// alice a member of all four from mins(0), and returns the store + db path.
// Message ids are "m-<level>" and every content carries the shared "budget"
// term so the text match never trims the matrix.
func classifiedRecallFixture(t *testing.T) (ChannelStore, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "channels.db")
	store, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()

	for _, level := range []Classification{
		ClassificationPublic, ClassificationInternal,
		ClassificationRestricted, ClassificationSecret,
	} {
		id := "group:" + string(level)
		require.NoError(t, store.CreateChannel(ctx, Channel{
			ID: id, Name: string(level), Type: ChannelTypeGroup,
			Classification: level,
		}))
	}
	withDB(t, path, func(db *sql.DB) {
		for _, level := range []Classification{
			ClassificationPublic, ClassificationInternal,
			ClassificationRestricted, ClassificationSecret,
		} {
			id := "group:" + string(level)
			seedInterval(t, db, id, "alice", mins(0), nil)
			seedMsg(t, db, msgSeed{
				id: "m-" + string(level), channelID: id, sender: "bob",
				content: "budget in " + string(level), ts: mins(10),
			})
		}
	})
	return store, path
}

// TestRecallMessages_ClassificationCap_Matrix pins the §F cap over the full
// lattice: acting at each level recalls exactly the channels whose
// classification ranks at or below it — asserted against [InjectableLevels],
// so the SQL IN-set and the Go resolver agree on every row of the matrix. The
// plan's headline case (a `secret`-channel message excluded acting-`public`,
// included acting-`secret`) is the matrix's two corner rows.
func TestRecallMessages_ClassificationCap_Matrix(t *testing.T) {
	store, _ := classifiedRecallFixture(t)
	ctx := context.Background()

	for _, acting := range []Classification{
		ClassificationPublic, ClassificationInternal,
		ClassificationRestricted, ClassificationSecret,
	} {
		got, err := store.RecallMessages(ctx, RecallParams{
			ParticipantID: "alice", Query: "budget", ActingClassification: acting,
		})
		require.NoErrorf(t, err, "acting %s", acting)

		want := make([]string, 0, 4)
		for _, level := range InjectableLevels(acting) {
			want = append(want, "m-"+string(level))
		}
		assert.ElementsMatchf(t, want, idSlice(got),
			"acting %s recalls exactly the ≤-rank channels", acting)
	}

	// The two §F corner rows, named explicitly (the PR-plan test contract).
	got, err := store.RecallMessages(ctx, RecallParams{
		ParticipantID: "alice", Query: "budget",
		ActingClassification: ClassificationPublic,
	})
	require.NoError(t, err)
	assert.NotContains(t, idSlice(got), "m-secret",
		"a secret-channel message is excluded acting-public")
	got, err = store.RecallMessages(ctx, RecallParams{
		ParticipantID: "alice", Query: "budget",
		ActingClassification: ClassificationSecret,
	})
	require.NoError(t, err)
	assert.Contains(t, idSlice(got), "m-secret",
		"a secret-channel message is included acting-secret")
}

// TestRecallMessages_Classification_ComposesWithMembership pins that the §F
// clause only ever NARROWS the RFC 0036 membership scope: acting `secret` (the
// widest cap) does not surface a message from a channel the participant was
// never a member of. The two predicates are AND-ed — classification cannot
// substitute for membership.
func TestRecallMessages_Classification_ComposesWithMembership(t *testing.T) {
	store, path := classifiedRecallFixture(t)
	ctx := context.Background()

	// A public channel alice was NEVER in — the least-classified channel there
	// is, so if any row could leak past membership on classification alone,
	// this is it.
	require.NoError(t, store.CreateChannel(ctx, Channel{
		ID: "group:outside", Name: "outside", Type: ChannelTypeGroup,
		Classification: ClassificationPublic,
	}))
	withDB(t, path, func(db *sql.DB) {
		seedMsg(t, db, msgSeed{
			id: "m-outside", channelID: "group:outside", sender: "carol",
			content: "budget outside", ts: mins(10),
		})
	})

	got, err := store.RecallMessages(ctx, RecallParams{
		ParticipantID: "alice", Query: "budget",
		ActingClassification: ClassificationSecret,
	})
	require.NoError(t, err)
	assert.NotContains(t, idSlice(got), "m-outside",
		"acting-secret must not widen recall past the membership scope")
	assert.Contains(t, idSlice(got), "m-secret",
		"positive control: the member channel's row is served at the same level")
}

// TestRecallMessages_Classification_UnsetActingFloorsPublic pins §A rule (b)
// at the store seam: an UNSET or unknown acting classification resolves to the
// `public` floor — return LESS — never the `internal` stamp default that would
// serve every legacy channel. This is the defensive layer under the handler's
// fail-loud required parameter: a future caller that bypasses validation
// degrades to the least-disclosing set instead of the confidential one.
func TestRecallMessages_Classification_UnsetActingFloorsPublic(t *testing.T) {
	store, _ := classifiedRecallFixture(t)
	ctx := context.Background()

	for name, acting := range map[string]Classification{
		"unset":   "",
		"unknown": "sekrit",
	} {
		got, err := store.RecallMessages(ctx, RecallParams{
			ParticipantID: "alice", Query: "budget", ActingClassification: acting,
		})
		require.NoErrorf(t, err, "%s acting level", name)
		assert.Equalf(t, []string{"m-public"}, idSlice(got),
			"%s acting level floors to public — only the public channel is served", name)
	}
}

// TestRecallMessages_Classification_UnknownLabelWithheld pins §A rule (c)
// realised in SQL: a channel whose stored classification is corrupted falls
// out of the IN-set and its messages are withheld at EVERY acting level —
// never coerced onto the lattice where a bad label could serve into an
// `internal` turn. (The clause's other fail-closed arm — a message whose
// channel row is MISSING fails the EXISTS — is structurally unreachable
// here: `messages.channel_id` is FK-enforced with ON DELETE CASCADE, so an
// orphan row cannot exist to be tested; the EXISTS shape is defense-in-depth
// for a schema that ever relaxes that.)
func TestRecallMessages_Classification_UnknownLabelWithheld(t *testing.T) {
	store, path := classifiedRecallFixture(t)
	ctx := context.Background()

	withDB(t, path, func(db *sql.DB) {
		// Corrupt the internal channel's label out of the §A vocabulary — the
		// one path a bad label can arrive by (no write path produces it; think
		// manual surgery or partial-write damage).
		_, err := db.Exec(`UPDATE channels SET classification = 'zzz' WHERE id = 'group:internal'`)
		require.NoError(t, err)
	})

	for _, acting := range []Classification{
		ClassificationPublic, ClassificationInternal,
		ClassificationRestricted, ClassificationSecret,
	} {
		got, err := store.RecallMessages(ctx, RecallParams{
			ParticipantID: "alice", Query: "budget", ActingClassification: acting,
		})
		require.NoErrorf(t, err, "acting %s", acting)
		assert.NotContainsf(t, idSlice(got), "m-internal",
			"a corrupted channel label is withheld acting-%s (rule (c))", acting)
	}

	// Positive control at the widest cap: the intact channels still serve, so
	// the exclusions above are the label/row, not a broken fixture.
	got, err := store.RecallMessages(ctx, RecallParams{
		ParticipantID: "alice", Query: "budget",
		ActingClassification: ClassificationSecret,
	})
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"m-public", "m-restricted", "m-secret"}, idSlice(got),
		"intact channels serve at acting-secret; only the corrupted and ghost rows are withheld")
}

// TestRecallMessages_Classification_LikeFallbackSameCap pins that the §F cap
// is byte-identical on the FTS5-unavailable LIKE path: the clause rides the
// scope fragment both paths receive verbatim, so dropping the index cannot
// widen the confidentiality boundary (the same no-widening property the
// RFC 0036 fallback tests pin for membership).
func TestRecallMessages_Classification_LikeFallbackSameCap(t *testing.T) {
	store, path := classifiedRecallFixture(t)
	ctx := context.Background()
	require.NoError(t, store.Close())

	dropMessagesFTS(t, path) // reopen must take the LIKE path
	store2, err := NewSQLiteStore(path, SQLiteOptions{})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store2.Close() })

	got, err := store2.RecallMessages(ctx, RecallParams{
		ParticipantID: "alice", Query: "budget",
		ActingClassification: ClassificationInternal,
	})
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"m-public", "m-internal"}, idSlice(got),
		"the LIKE fallback applies the identical §F cap (public+internal at acting-internal)")
	assert.NotContains(t, idSlice(got), "m-secret",
		"a secret-channel message stays excluded with FTS5 unavailable")
}
