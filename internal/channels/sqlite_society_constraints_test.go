package channels

// The society tables' constraints (channels-store v15, RFC 0029 Phase 3 plan
// PR 2). Each refuses the row it exists to refuse, beside a row it must
// admit, so a check that refuses everything fails here too. The migration
// itself is pinned in sqlite_society_migration_test.go.

import (
	"database/sql"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// approval is one society_approvals row, as a test writes it.
type approval struct {
	ID, Kind, Actor, Hash, Nonce, State string
	// Turn is the held turn's protection level, cascade depth and origin, in
	// that order. Nil leaves all three NULL; a nil entry leaves that one NULL.
	Turn []any
	// TieGroup and Payload store NULL when nil.
	TieGroup, Payload any
}

// pendingAction is a persona's open hold on an action, with its held turn
// and a payload, numbered n.
func pendingAction(n int) approval {
	return approval{
		ID: fmt.Sprintf("approval-%d", n), Kind: "action", Actor: "ember-owl",
		Hash: fmt.Sprintf("hash-%d", n), Nonce: fmt.Sprintf("nonce-%d", n), State: "pending",
		Turn:    []any{"internal", 1, "group:room-0"},
		Payload: `{"tool":"file_write","arguments":{"path":"notes.md"}}`,
	}
}

// insertApproval writes a, expiring a day after it opens, its times in unix
// seconds.
func insertApproval(db *sql.DB, a approval) error {
	turn := []any{nil, nil, nil}
	copy(turn, a.Turn)
	now := time.Now().Unix()
	_, err := db.Exec(
		`INSERT INTO society_approvals (id, kind, actor_id, action_hash, nonce, state, expires_at,
		   protection_level, cascade_depth, origin, tie_group, payload, created_at)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		a.ID, a.Kind, a.Actor, a.Hash, a.Nonce, a.State, now+24*60*60,
		turn[0], turn[1], turn[2], a.TieGroup, a.Payload, now)
	return err
}

// assertRefusal checks err against want: "" means the row was admitted,
// anything else is a fragment of the refusal naming its constraint.
func assertRefusal(t *testing.T, err error, want string) {
	t.Helper()
	if want == "" {
		assert.NoError(t, err, "the row is admitted")
		return
	}
	require.Error(t, err, "the row is refused")
	assert.Contains(t, err.Error(), want)
}

// TestSocietyApprovals_Constraints walks each rule on an approval row: S-3's
// and the PR's own (the plan's 2026-10-24 note). Each case writes the rows in
// `first`, which must be admitted, then `row`, and checks what happens to it.
func TestSocietyApprovals_Constraints(t *testing.T) {
	const oneOpen = "UNIQUE constraint failed: society_approvals.actor_id, society_approvals.action_hash"
	const heldTurn = "CHECK constraint failed: society_approvals_held_turn"
	const cascadeDepth = "CHECK constraint failed: society_approvals_cascade_depth"
	const tieInGroup = "CHECK constraint failed: society_approvals_tie_in_group"
	with := func(a approval, edit func(*approval)) approval { edit(&a); return a }
	sameHold := func(a *approval) { a.Hash = "hash-1" }

	cases := []struct {
		name  string
		first []approval
		row   approval
		want  string
	}{
		{"a second open approval for one actor and hash",
			[]approval{pendingAction(1)}, with(pendingAction(2), sameHold), oneOpen},
		{"an approved row still holds its hash open",
			[]approval{with(pendingAction(1), func(a *approval) { a.State = "approved" })},
			with(pendingAction(2), sameHold), oneOpen},
		{"a settled row frees its hash",
			[]approval{with(pendingAction(1), func(a *approval) { a.State = "denied"; a.Payload = nil })},
			with(pendingAction(2), sameHold), ""},
		{"another actor may hold the same hash",
			[]approval{pendingAction(1)},
			with(pendingAction(2), func(a *approval) { a.Hash = "hash-1"; a.Actor = "iron-fox" }), ""},
		{"a reused nonce",
			[]approval{pendingAction(1)},
			with(pendingAction(2), func(a *approval) { a.Nonce = "nonce-1" }),
			"UNIQUE constraint failed: society_approvals.nonce"},
		{"an unknown state",
			nil, with(pendingAction(1), func(a *approval) { a.State = "granted" }),
			"CHECK constraint failed: society_approvals_state"},
		{"an unknown kind",
			nil, with(pendingAction(1), func(a *approval) { a.Kind = "vote" }),
			"CHECK constraint failed: society_approvals_kind"},
		{"a task agent's hold waits in its step, with no held turn",
			nil, with(pendingAction(1), func(a *approval) { a.Turn = nil }), ""},
		{"a held turn with no protection level",
			nil, with(pendingAction(1), func(a *approval) { a.Turn[0] = nil }), heldTurn},
		{"a held turn with no cascade depth",
			nil, with(pendingAction(1), func(a *approval) { a.Turn[1] = nil }), heldTurn},
		{"a held turn with no origin",
			nil, with(pendingAction(1), func(a *approval) { a.Turn[2] = nil }), heldTurn},
		{"a cascade depth stored as text",
			nil, with(pendingAction(1), func(a *approval) { a.Turn[1] = "one" }), cascadeDepth},
		{"a negative cascade depth",
			nil, with(pendingAction(1), func(a *approval) { a.Turn[1] = -1 }), cascadeDepth},
		{"a graph change has no held turn",
			nil, with(pendingAction(1), func(a *approval) {
				a.Kind, a.Actor, a.Turn = "graph_change", "operator-1", nil
			}), ""},
		{"a graph change with a held turn",
			nil, with(pendingAction(1), func(a *approval) { a.Kind, a.Actor = "graph_change", "operator-1" }),
			heldTurn},
		{"a tie row in its group",
			nil, with(pendingAction(1), func(a *approval) { a.Kind, a.Turn, a.TieGroup = "tie", nil, "tie-1" }), ""},
		{"a tie row with a held turn",
			nil, with(pendingAction(1), func(a *approval) { a.Kind, a.TieGroup = "tie", "tie-1" }), heldTurn},
		{"a tie row outside any group",
			nil, with(pendingAction(1), func(a *approval) { a.Kind, a.Turn = "tie", nil }), tieInGroup},
		{"an action row in a tie group",
			nil, with(pendingAction(1), func(a *approval) { a.TieGroup = "tie-1" }), tieInGroup},
		{"a graph change in a tie group",
			nil, with(pendingAction(1), func(a *approval) {
				a.Kind, a.Actor, a.Turn, a.TieGroup = "graph_change", "operator-1", nil, "tie-1"
			}), tieInGroup},
		{"a settled row that keeps its payload",
			nil, with(pendingAction(1), func(a *approval) { a.State = "spent" }),
			"CHECK constraint failed: society_approvals_payload_settled"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			db := newSocietyDB(t)
			for _, a := range tc.first {
				require.NoError(t, insertApproval(db, a), "the first row is admitted")
			}
			assertRefusal(t, insertApproval(db, tc.row), tc.want)
		})
	}
}

// TestSocietyApprovals_SettlingClearsThePayload is G-11 in the table itself:
// a row leaves `pending` and `approved` only with its held text cleared, by
// the same write, so the store keeps no settled row's text.
func TestSocietyApprovals_SettlingClearsThePayload(t *testing.T) {
	for _, settled := range []string{"denied", "expired", "withdrawn", "spent"} {
		t.Run(settled, func(t *testing.T) {
			db := newSocietyDB(t)
			require.NoError(t, insertApproval(db, pendingAction(1)))
			_, err := db.Exec(`UPDATE society_approvals SET state = 'approved' WHERE id = 'approval-1'`)
			require.NoError(t, err, "an approved row still waits to be spent, so it keeps its payload")

			_, err = db.Exec(`UPDATE society_approvals SET state = ? WHERE id = 'approval-1'`, settled)
			assertRefusal(t, err, "CHECK constraint failed: society_approvals_payload_settled")

			_, err = db.Exec(
				`UPDATE society_approvals SET state = ?, payload = NULL WHERE id = 'approval-1'`, settled)
			require.NoError(t, err)
		})
	}
}

// TestSocietyApprovals_TimesAreUnixSeconds: every time on an approval row is
// a whole number of unix seconds, as the accounts store keeps its expiry, so
// the expiry checks compare numbers. A time.Time, which the driver stores as
// text in the writer's own time zone, is refused.
func TestSocietyApprovals_TimesAreUnixSeconds(t *testing.T) {
	for _, column := range []string{"expires_at", "created_at", "decided_at", "delivered_at"} {
		t.Run(column, func(t *testing.T) {
			db := newSocietyDB(t)
			require.NoError(t, insertApproval(db, pendingAction(1)))
			set := column + ` = ?`
			if column == "decided_at" {
				set += `, decided_by = 'operator-1'` // who decided goes with when
			}
			update := `UPDATE society_approvals SET ` + set + ` WHERE id = 'approval-1'`
			now := time.Now()

			_, err := db.Exec(update, now)
			assertRefusal(t, err, "CHECK constraint failed: society_approvals_unix_times")
			_, err = db.Exec(update, now.Unix())
			assertRefusal(t, err, "")
		})
	}
}

// TestSocietyApprovals_DecisionNamesWhoAndWhen: a decided row records who
// decided and when, both or neither (RFC 0009 PR 12), so no decision is kept
// without its time or without its decider.
func TestSocietyApprovals_DecisionNamesWhoAndWhen(t *testing.T) {
	const decide = `UPDATE society_approvals SET state = 'denied', payload = NULL, %s WHERE id = 'approval-1'`
	const decision = "CHECK constraint failed: society_approvals_decision"
	cases := []struct{ name, set, want string }{
		{"a decider with no time", `decided_by = 'operator-1'`, decision},
		{"a time with no decider", `decided_at = 86400`, decision},
		{"both", `decided_by = 'operator-1', decided_at = 86400`, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			db := newSocietyDB(t)
			require.NoError(t, insertApproval(db, pendingAction(1)))
			_, err := db.Exec(fmt.Sprintf(decide, tc.set))
			assertRefusal(t, err, tc.want)
		})
	}
}

// TestSocietyTables_SingleColumnKeysAreNeverNull: SQLite lets a one-column
// TEXT primary key hold NULL, and NULLs never collide, so without NOT NULL
// two starts could each store a key with no purpose. Each statement is
// refused as written and admitted with a value in place of the NULL.
func TestSocietyTables_SingleColumnKeysAreNeverNull(t *testing.T) {
	cases := []struct{ name, stmt, want string }{
		{"an organization with no id",
			`INSERT INTO society_organizations (id, name, topology) VALUES (NULL, 'Acme', 'flat')`,
			"NOT NULL constraint failed: society_organizations.id"},
		{"an approval with no id",
			`INSERT INTO society_approvals (id, kind, actor_id, action_hash, nonce, expires_at, created_at)
			 VALUES (NULL, 'graph_change', 'operator-1', 'hash-1', 'nonce-1', 86400, 0)`,
			"NOT NULL constraint failed: society_approvals.id"},
		{"a key with no purpose",
			`INSERT INTO society_keys (purpose, key, created_at) VALUES (NULL, randomblob(32), 0)`,
			"NOT NULL constraint failed: society_keys.purpose"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			db := newSocietyDB(t)
			_, err := db.Exec(tc.stmt)
			assertRefusal(t, err, tc.want)
			_, err = db.Exec(strings.Replace(tc.stmt, "NULL", "'some-id'", 1))
			assertRefusal(t, err, "")
		})
	}
}

// seedOrganizations writes two organizations: acme, a hierarchy with a lead
// and a member role, and beta, a flat one with a member role only.
func seedOrganizations(t *testing.T, db *sql.DB) {
	t.Helper()
	for _, q := range []string{
		`INSERT INTO society_organizations (id, name, topology) VALUES
		   ('acme', 'Acme Engineering', 'hierarchy'), ('beta', 'Beta Lab', 'flat')`,
		`INSERT INTO society_roles (organization_id, id, authority) VALUES
		   ('acme', 'lead', 'directive'), ('acme', 'member', 'operational'), ('beta', 'member', 'peer')`,
	} {
		_, err := db.Exec(q)
		require.NoError(t, err)
	}
}

// TestSocietyOrganizations_Constraints walks S-4's keys. Every statement but
// the last must be admitted; the last is checked against want.
func TestSocietyOrganizations_Constraints(t *testing.T) {
	const member = `INSERT INTO society_members (organization_id, agent_id, role_id) VALUES `
	const edge = `INSERT INTO society_manages (organization_id, manager_id, report_id, scope) VALUES `
	const deleteRole = `DELETE FROM society_roles WHERE organization_id = 'acme' AND id = `

	cases := []struct {
		name  string
		stmts []string
		want  string
	}{
		{"an agent twice in one organization",
			[]string{member + `('acme', 'ember-owl', 'lead')`, member + `('acme', 'ember-owl', 'member')`},
			"UNIQUE constraint failed: society_members.organization_id, society_members.agent_id"},
		{"an agent in two organizations is admitted: that rule stays in validation",
			[]string{member + `('acme', 'ember-owl', 'lead')`, member + `('beta', 'ember-owl', 'member')`}, ""},
		{"a role its organization does not declare",
			[]string{member + `('beta', 'iron-fox', 'lead')`}, "FOREIGN KEY constraint failed"},
		{"a role a member still holds is never deleted, so no member goes with it",
			[]string{member + `('acme', 'ember-owl', 'lead')`, deleteRole + `'lead'`}, "FOREIGN KEY constraint failed"},
		{"a role no member holds",
			[]string{member + `('acme', 'ember-owl', 'lead')`, deleteRole + `'member'`}, ""},
		{"a manages edge to someone outside the organization",
			[]string{member + `('acme', 'ember-owl', 'lead')`, edge + `('acme', 'ember-owl', 'iron-fox', '')`},
			"FOREIGN KEY constraint failed"},
		{"a manages edge between members, with no scope",
			[]string{member + `('acme', 'ember-owl', 'lead')`, member + `('acme', 'iron-fox', 'member')`,
				edge + `('acme', 'ember-owl', 'iron-fox', '')`}, ""},
		{"one manager on two scopes, as a matrix allows",
			[]string{member + `('acme', 'ember-owl', 'lead')`, member + `('acme', 'iron-fox', 'member')`,
				edge + `('acme', 'ember-owl', 'iron-fox', 'project')`,
				edge + `('acme', 'ember-owl', 'iron-fox', 'function')`}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			db := newSocietyDB(t)
			seedOrganizations(t, db)
			last := len(tc.stmts) - 1
			for _, q := range tc.stmts[:last] {
				_, err := db.Exec(q)
				require.NoError(t, err, q)
			}
			_, err := db.Exec(tc.stmts[last])
			assertRefusal(t, err, tc.want)
		})
	}
}

// TestSocietyOrganizations_DeletingAnOrganizationTakesItsGraph is what PR 3's
// graph replacement leans on: an organization's roles, members and edges go
// with it, and another organization's stay.
func TestSocietyOrganizations_DeletingAnOrganizationTakesItsGraph(t *testing.T) {
	db := newSocietyDB(t)
	seedOrganizations(t, db)
	for _, q := range []string{
		`INSERT INTO society_members (organization_id, agent_id, role_id) VALUES
		   ('acme', 'ember-owl', 'lead'), ('acme', 'iron-fox', 'member'), ('beta', 'nova-sparrow', 'member')`,
		`INSERT INTO society_manages (organization_id, manager_id, report_id, scope) VALUES
		   ('acme', 'ember-owl', 'iron-fox', '')`,
		`DELETE FROM society_organizations WHERE id = 'acme'`,
	} {
		_, err := db.Exec(q)
		require.NoError(t, err, q)
	}

	left := map[string]string{}
	for _, table := range []string{"society_roles", "society_members", "society_manages"} {
		var orgs sql.NullString
		require.NoError(t, db.QueryRow(
			`SELECT group_concat(DISTINCT organization_id) FROM `+table).Scan(&orgs))
		left[table] = orgs.String
	}
	assert.Equal(t, map[string]string{
		"society_roles": "beta", "society_members": "beta", "society_manages": "",
	}, left)
}

// TestSocietyOrganizations_DeletingAMemberTakesItsEdges: a member removed on
// their own takes both the edge to their manager and the edges to their
// reports, so a graph change never leaves an edge to someone who has gone.
func TestSocietyOrganizations_DeletingAMemberTakesItsEdges(t *testing.T) {
	db := newSocietyDB(t)
	seedOrganizations(t, db)
	for _, q := range []string{
		`INSERT INTO society_members (organization_id, agent_id, role_id) VALUES
		   ('acme', 'ember-owl', 'lead'), ('acme', 'iron-fox', 'lead'), ('acme', 'nova-sparrow', 'member')`,
		`INSERT INTO society_manages (organization_id, manager_id, report_id, scope) VALUES
		   ('acme', 'ember-owl', 'iron-fox', ''), ('acme', 'iron-fox', 'nova-sparrow', '')`,
		`DELETE FROM society_members WHERE organization_id = 'acme' AND agent_id = 'iron-fox'`,
	} {
		_, err := db.Exec(q)
		require.NoError(t, err, q)
	}

	var edges int
	require.NoError(t, db.QueryRow(`SELECT COUNT(*) FROM society_manages`).Scan(&edges))
	assert.Zero(t, edges)
	var members int
	require.NoError(t, db.QueryRow(`SELECT COUNT(*) FROM society_members`).Scan(&members))
	assert.Equal(t, 2, members, "the other members stay")
}

// TestSocietyMeta_Constraints: the graph version lives in exactly one row,
// and is a whole number that never goes below zero.
func TestSocietyMeta_Constraints(t *testing.T) {
	cases := []struct {
		name, stmt, want string
	}{
		{"a second row", `INSERT INTO society_meta (id, graph_version) VALUES (2, 0)`,
			"CHECK constraint failed: society_meta_one_row"},
		{"a negative version", `UPDATE society_meta SET graph_version = -1 WHERE id = 1`,
			"CHECK constraint failed: society_meta_graph_version"},
		{"a version stored as text", `UPDATE society_meta SET graph_version = 'one' WHERE id = 1`,
			"CHECK constraint failed: society_meta_graph_version"},
		{"the version goes up", `UPDATE society_meta SET graph_version = graph_version + 1 WHERE id = 1`, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := newSocietyDB(t).Exec(tc.stmt)
			assertRefusal(t, err, tc.want)
		})
	}
}

// TestSocietyKeys_Constraints: one key per purpose (S-5), none shorter than
// the 32 bytes an HMAC-SHA256 key needs, and its time in unix seconds, as an
// approval row keeps its times.
func TestSocietyKeys_Constraints(t *testing.T) {
	const insert = `INSERT INTO society_keys (purpose, key, created_at) VALUES (?, ?, ?)`
	key := []byte(strings.Repeat("k", 32))

	cases := []struct {
		name  string
		first bool
		key   any
		at    any // nil writes the time in unix seconds
		want  string
	}{
		{"a 32-byte key", false, key, nil, ""},
		{"a second key for one purpose", true, key, nil, "UNIQUE constraint failed: society_keys.purpose"},
		{"a 16-byte key", false, key[:16], nil, "CHECK constraint failed: society_keys_length"},
		{"a key stored as text", false, string(key), nil, "CHECK constraint failed: society_keys_length"},
		{"a time stored as text", false, key, time.Now(), "CHECK constraint failed: society_keys_unix_time"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			db := newSocietyDB(t)
			now := time.Now().Unix()
			if tc.first {
				_, err := db.Exec(insert, "approval_signing", key, now)
				require.NoError(t, err)
			}
			at := tc.at
			if at == nil {
				at = now
			}
			_, err := db.Exec(insert, "approval_signing", tc.key, at)
			assertRefusal(t, err, tc.want)
		})
	}
}
