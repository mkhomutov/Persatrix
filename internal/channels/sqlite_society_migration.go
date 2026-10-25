package channels

// Society tables (channelStoreSchemaVersion v14 → v15, RFC 0029 Phase 3 PR 2).
// Lives in its own sibling file per the convention in sqlite_migrations.go.

import (
	"database/sql"
	"fmt"
)

// societySchemaV15 is every society table, in the order their foreign keys
// need. The RFC 0029 Phase 3 plan names what each holds (S-3, S-4, S-5) and
// which later PR writes it; its 2026-10-24 note lists the rules this PR adds.
// A one-column TEXT primary key is NOT NULL, since SQLite otherwise lets it
// hold NULLs that never collide.
var societySchemaV15 = []string{
	// The organization graph's version, a whole number in a single row. A
	// graph write raises it in the same transaction (RFC 0012 OQ 2); 0 means
	// no graph is loaded.
	`CREATE TABLE society_meta (
        id            INTEGER PRIMARY KEY CONSTRAINT society_meta_one_row CHECK (id = 1),
        graph_version INTEGER NOT NULL DEFAULT 0
                      CONSTRAINT society_meta_graph_version
                      CHECK (typeof(graph_version) = 'integer' AND graph_version >= 0)
    )`,
	`INSERT INTO society_meta (id, graph_version) VALUES (1, 0)`,

	// The organization graph, as RFC 0012 §B declares it. Topology and role
	// authority carry no CHECK: the graph is written only after the loader has
	// validated it, and the loader owns both vocabularies, as classification.go
	// owns v11's. Members are keyed by organization and agent, so the rule
	// that an agent belongs to one organization stays in validation and
	// RFC 0012 Phase 5 lifts it without a migration. An unscoped `manages`
	// edge stores an empty scope. Deleting an organization deletes its graph,
	// and removing a member removes the edges to and from them; a role a
	// member still holds cannot be deleted, so no change drops a member with
	// its role.
	`CREATE TABLE society_organizations (
        id       TEXT NOT NULL PRIMARY KEY,
        name     TEXT NOT NULL,
        topology TEXT NOT NULL
    )`,
	`CREATE TABLE society_roles (
        organization_id TEXT NOT NULL REFERENCES society_organizations(id) ON DELETE CASCADE,
        id              TEXT NOT NULL,
        authority       TEXT NOT NULL,
        PRIMARY KEY (organization_id, id)
    )`,
	`CREATE TABLE society_members (
        organization_id TEXT NOT NULL REFERENCES society_organizations(id) ON DELETE CASCADE,
        agent_id        TEXT NOT NULL,
        role_id         TEXT NOT NULL,
        PRIMARY KEY (organization_id, agent_id),
        FOREIGN KEY (organization_id, role_id) REFERENCES society_roles(organization_id, id)
    )`,
	// A persona's organization and role, read by agent (RFC 0012 C1 PR 4).
	`CREATE INDEX idx_society_members_agent ON society_members(agent_id)`,
	// A role's holders, which each role delete looks up to check that no
	// member still holds it.
	`CREATE INDEX idx_society_members_role ON society_members(organization_id, role_id)`,
	`CREATE TABLE society_manages (
        organization_id TEXT NOT NULL,
        manager_id      TEXT NOT NULL,
        report_id       TEXT NOT NULL,
        scope           TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (organization_id, manager_id, report_id, scope),
        FOREIGN KEY (organization_id, manager_id)
            REFERENCES society_members(organization_id, agent_id) ON DELETE CASCADE,
        FOREIGN KEY (organization_id, report_id)
            REFERENCES society_members(organization_id, agent_id) ON DELETE CASCADE
    )`,
	// A member's managers, the walk up the chain `directs` makes (RFC 0012 §C).
	`CREATE INDEX idx_society_manages_report ON society_manages(organization_id, report_id)`,

	// The pending-approval queue (RFC 0028 E-8 and E-11, RFC 0009 G-9 to
	// G-11). The state is the used-nonce record: a spend moves an `action`
	// row from `approved` to `spent`. The nonce is made when the row opens,
	// and the token an approval mints carries it. A persona's hold keeps the
	// turn its release resumes (E-13): its protection level, cascade depth
	// and origin, all three or none, the depth a whole number. A task agent's
	// hold waits inside its step, a `graph_change` row's actor is an operator,
	// and a `tie` row releases nothing, so they keep none, and only an
	// `action` row may keep one. A `tie` row, and no other, belongs to a tie
	// group. The held action's payload is JSON, and a row leaves `pending` and
	// `approved` only with it cleared (G-11), so the store keeps no settled
	// row's text. A decided row records who decided and when, both or
	// neither. Times are unix seconds, as the accounts store keeps its
	// expiry, so expiry is compared as a number in Go, never as text.
	`CREATE TABLE society_approvals (
        id               TEXT NOT NULL PRIMARY KEY,
        kind             TEXT NOT NULL
                         CONSTRAINT society_approvals_kind CHECK (kind IN ('action', 'graph_change', 'tie')),
        actor_id         TEXT NOT NULL,
        action_hash      TEXT NOT NULL,
        nonce            TEXT NOT NULL UNIQUE,
        state            TEXT NOT NULL DEFAULT 'pending'
                         CONSTRAINT society_approvals_state CHECK (state IN
                             ('pending', 'approved', 'denied', 'expired', 'withdrawn', 'spent')),
        expires_at       INTEGER NOT NULL,
        protection_level TEXT,
        cascade_depth    INTEGER,
        origin           TEXT,
        tie_group        TEXT,
        payload          TEXT,
        decided_by       TEXT,
        decided_at       INTEGER,
        delivered_at     INTEGER,
        created_at       INTEGER NOT NULL,
        CONSTRAINT society_approvals_held_turn CHECK
            ((protection_level IS NULL) = (cascade_depth IS NULL) AND (cascade_depth IS NULL) = (origin IS NULL)
             AND (kind = 'action' OR protection_level IS NULL)),
        CONSTRAINT society_approvals_cascade_depth CHECK
            (cascade_depth IS NULL OR (typeof(cascade_depth) = 'integer' AND cascade_depth >= 0)),
        CONSTRAINT society_approvals_tie_in_group CHECK ((kind = 'tie') = (tie_group IS NOT NULL)),
        CONSTRAINT society_approvals_decision CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
        CONSTRAINT society_approvals_unix_times CHECK
            (typeof(expires_at) = 'integer' AND typeof(created_at) = 'integer'
             AND typeof(decided_at) IN ('integer', 'null') AND typeof(delivered_at) IN ('integer', 'null')),
        CONSTRAINT society_approvals_payload_settled CHECK
            (state IN ('pending', 'approved') OR payload IS NULL)
    )`,
	// At most one open row per actor and action, so a retried hold reuses its
	// row (E-11); a settled row frees the hash.
	`CREATE UNIQUE INDEX ux_society_approvals_open ON society_approvals(actor_id, action_hash)
        WHERE state IN ('pending', 'approved')`,
	// The operators' queue by state, and the expiry sweep.
	`CREATE INDEX idx_society_approvals_state_expiry ON society_approvals(state, expires_at)`,
	// An agent's own rows, on its holds route.
	`CREATE INDEX idx_society_approvals_actor ON society_approvals(actor_id, created_at)`,
	// A tie group's siblings, withdrawn together when one is approved.
	`CREATE INDEX idx_society_approvals_tie_group ON society_approvals(tie_group)
        WHERE tie_group IS NOT NULL`,

	// One key per purpose, made once by the store's first call for it (S-5).
	// The approval-signing key is an HMAC-SHA256 key, so nothing shorter than
	// 32 bytes, nor stored as text, is a key. Its time is unix seconds, as an
	// approval row's are.
	`CREATE TABLE society_keys (
        purpose    TEXT NOT NULL PRIMARY KEY,
        key        BLOB NOT NULL
                   CONSTRAINT society_keys_length CHECK (typeof(key) = 'blob' AND length(key) >= 32),
        created_at INTEGER NOT NULL
                   CONSTRAINT society_keys_unix_time CHECK (typeof(created_at) = 'integer')
    )`,
}

// migrateV14ToV15 adds the society tables to the channels store: the state
// v0.4.0's organizations share rather than one persona holds — the
// organization graph and its version, the approvals waiting for a person,
// and the key that signs them. RFC 0029's 2026-10-20 amendment puts them in
// `data/channels.db`, in this store's one migration chain, since SQLite keeps
// one `user_version` per file (plan S-2); a later society store reads them on
// this store's own connection (PR 3).
//
// A pure addition: no existing table, row or index is touched, and nothing
// reads the new tables yet. Plain CREATE TABLE, as v9 does, so a store that
// already holds a table of one of these names fails the step, and its
// transaction leaves the store at v14. One transaction stamps the version
// with the tables (PR #335 review L3).
func migrateV14ToV15(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	for _, q := range societySchemaV15 {
		if _, err := tx.Exec(q); err != nil {
			return fmt.Errorf("exec %q: %w", firstLine(q), err)
		}
	}
	if err := stampUserVersionTx(tx, 15); err != nil {
		return err
	}
	return tx.Commit()
}
