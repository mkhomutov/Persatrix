package channels

// Society-store benchmark, part 1 of 3: the candidate society tables and the
// calls a society store would make on them (v0.4.0 row R2).
//
// The tables are a stand-in, not A1's schema. They carry what the RFC 0012
// and RFC 0028 PR plans ask of A1 — an organization graph with a version
// counter, the approval-signing key, and pending approvals whose state is the
// used-nonce record, at most one open row per (actor, action) — so the
// benchmark loads the same shape of rows and indexes A1 will. A1 writes the
// real schema; if it differs much from this one, it re-runs the benchmark.

import (
	"context"
	"crypto/rand"
	"database/sql"
	"errors"
	"fmt"
	"time"
)

// benchSocietySchema is created in the channels database beside the channel
// tables, as RFC 0029's 2026-10-20 amendment places the society tables.
const benchSocietySchema = `
CREATE TABLE IF NOT EXISTS bench_society_meta (
	key   TEXT PRIMARY KEY,
	value BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS bench_org_roles (
	org_id    TEXT NOT NULL,
	role      TEXT NOT NULL,
	authority TEXT NOT NULL,
	PRIMARY KEY (org_id, role)
);
CREATE TABLE IF NOT EXISTS bench_org_members (
	org_id   TEXT NOT NULL,
	agent_id TEXT NOT NULL,
	role     TEXT NOT NULL,
	PRIMARY KEY (org_id, agent_id)
);
CREATE INDEX IF NOT EXISTS bench_org_members_agent ON bench_org_members(agent_id);
CREATE TABLE IF NOT EXISTS bench_org_edges (
	org_id     TEXT NOT NULL,
	manager_id TEXT NOT NULL,
	report_id  TEXT NOT NULL,
	scope      TEXT NOT NULL DEFAULT '*',
	PRIMARY KEY (org_id, manager_id, report_id, scope)
);
CREATE INDEX IF NOT EXISTS bench_org_edges_report ON bench_org_edges(report_id);
CREATE TABLE IF NOT EXISTS bench_approvals (
	id               TEXT PRIMARY KEY,
	actor_id         TEXT NOT NULL,
	action_hash      TEXT NOT NULL,
	nonce            TEXT NOT NULL UNIQUE,
	state            TEXT NOT NULL CHECK (state IN
	                   ('pending','approved','denied','expired','withdrawn','spent')),
	expires_at       TIMESTAMP NOT NULL,
	protection_level TEXT NOT NULL,
	cascade_depth    INTEGER NOT NULL,
	origin           TEXT NOT NULL,
	tie_group        TEXT,
	created_at       TIMESTAMP NOT NULL,
	decided_at       TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS bench_approvals_one_open
	ON bench_approvals(actor_id, action_hash) WHERE state IN ('pending','approved');
CREATE INDEX IF NOT EXISTS bench_approvals_state_expiry ON bench_approvals(state, expires_at);
CREATE INDEX IF NOT EXISTS bench_approvals_tie ON bench_approvals(tie_group) WHERE tie_group IS NOT NULL;
`

const benchOrgID = "bench-org"

// seedSociety creates the society tables and fills them with one
// organization over every agent — a manager for each block of five, the rest
// reporting to their block's manager — the signing key, a graph version, and
// `history` settled approvals so the indexes are not empty.
func seedSociety(ctx context.Context, db *sql.DB, agents []string, history int) error {
	if _, err := db.ExecContext(ctx, benchSocietySchema); err != nil {
		return fmt.Errorf("society schema: %w", err)
	}
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO bench_society_meta (key, value) VALUES ('signing_key', ?), ('graph_version', 1)`,
		key); err != nil {
		return err
	}
	for _, role := range [][2]string{{"lead", "high"}, {"member", "standard"}} {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO bench_org_roles (org_id, role, authority) VALUES (?, ?, ?)`,
			benchOrgID, role[0], role[1]); err != nil {
			return err
		}
	}
	for i, agent := range agents {
		role := "member"
		if i%5 == 0 {
			role = "lead"
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO bench_org_members (org_id, agent_id, role) VALUES (?, ?, ?)`,
			benchOrgID, agent, role); err != nil {
			return err
		}
		if role == "member" {
			if _, err := tx.ExecContext(ctx,
				`INSERT INTO bench_org_edges (org_id, manager_id, report_id) VALUES (?, ?, ?)`,
				benchOrgID, agents[i-i%5], agent); err != nil {
				return err
			}
		}
	}
	settled := []string{"spent", "denied", "expired", "withdrawn"}
	past := time.Now().UTC().Add(-24 * time.Hour)
	for i := 0; i < history; i++ {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO bench_approvals (id, actor_id, action_hash, nonce, state, expires_at,
			   protection_level, cascade_depth, origin, created_at, decided_at)
			 VALUES (?, ?, ?, ?, ?, ?, 'internal', 1, 'channel', ?, ?)`,
			fmt.Sprintf("hist-%d", i), agents[i%len(agents)], fmt.Sprintf("action-hist-%d", i),
			fmt.Sprintf("nonce-hist-%d", i), settled[i%len(settled)],
			past.Add(15*time.Minute), past, past.Add(time.Minute)); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// societyOrgRead is the read an authority check makes: the graph version, the
// agent's role and authority, and its managers with theirs, in one snapshot.
func societyOrgRead(ctx context.Context, db *sql.DB, agent string) error {
	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	var version int64
	if err := tx.QueryRowContext(ctx,
		`SELECT CAST(value AS INTEGER) FROM bench_society_meta WHERE key = 'graph_version'`).Scan(&version); err != nil {
		return fmt.Errorf("graph version: %w", err)
	}
	var authority string
	if err := tx.QueryRowContext(ctx,
		`SELECT r.authority FROM bench_org_members m
		   JOIN bench_org_roles r ON r.org_id = m.org_id AND r.role = m.role
		  WHERE m.agent_id = ?`, agent).Scan(&authority); err != nil {
		return fmt.Errorf("role: %w", err)
	}
	rows, err := tx.QueryContext(ctx,
		`SELECT e.manager_id, e.scope, r.authority FROM bench_org_edges e
		   JOIN bench_org_members m ON m.org_id = e.org_id AND m.agent_id = e.manager_id
		   JOIN bench_org_roles r ON r.org_id = m.org_id AND r.role = m.role
		  WHERE e.report_id = ?`, agent)
	if err != nil {
		return fmt.Errorf("managers: %w", err)
	}
	for rows.Next() {
		var manager, scope, auth string
		if err := rows.Scan(&manager, &scope, &auth); err != nil {
			_ = rows.Close()
			return err
		}
	}
	if err := rows.Close(); err != nil {
		return err
	}
	return rows.Err()
}

// societyListPending is the operator's approval-queue page.
func societyListPending(ctx context.Context, db *sql.DB) error {
	rows, err := db.QueryContext(ctx,
		`SELECT id, actor_id, action_hash, expires_at FROM bench_approvals
		  WHERE state = 'pending' ORDER BY created_at LIMIT 50`)
	if err != nil {
		return err
	}
	for rows.Next() {
		var id, actor, hash string
		var exp time.Time
		if err := rows.Scan(&id, &actor, &hash, &exp); err != nil {
			_ = rows.Close()
			return err
		}
	}
	if err := rows.Close(); err != nil {
		return err
	}
	return rows.Err()
}

const benchInsertApproval = `INSERT INTO bench_approvals (id, actor_id, action_hash, nonce, state, expires_at,
	   protection_level, cascade_depth, origin, tie_group, created_at)
	 VALUES (?, ?, ?, ?, 'pending', ?, 'internal', 1, 'channel', ?, ?)`

// societyOpenApproval holds an action: one pending row with its nonce.
func societyOpenApproval(ctx context.Context, db *sql.DB, id, actor, tieGroup string, ttl time.Duration) error {
	now := time.Now().UTC()
	tie := sql.NullString{String: tieGroup, Valid: tieGroup != ""}
	_, err := db.ExecContext(ctx, benchInsertApproval,
		id, actor, "action-"+id, "nonce-"+id, now.Add(ttl), tie, now)
	return err
}

// societyOpenTieGroup opens a tie group whole, every row in one transaction,
// as A4 does, so nothing joins the group later.
func societyOpenTieGroup(ctx context.Context, db *sql.DB, ids []string, actor, tieGroup string, ttl time.Duration) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	now := time.Now().UTC()
	for _, id := range ids {
		if _, err := tx.ExecContext(ctx, benchInsertApproval,
			id, actor, "action-"+id, "nonce-"+id, now.Add(ttl), tieGroup, now); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// errBenchNoRow marks a conditional update that matched nothing — a lost race
// the lifecycle expects (the sweeper expired the row first), not a failure.
var errBenchNoRow = errors.New("no row in the expected state")

// societyDecide approves or denies a pending row. Approving withdraws the
// rest of its tie group in the same transaction; with graphChange set it also
// changes the organization graph and bumps its version, so the approval and
// the change commit together or not at all.
func societyDecide(ctx context.Context, db *sql.DB, id string, approve, graphChange bool) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	state := "denied"
	if approve {
		state = "approved"
	}
	now := time.Now().UTC()
	res, err := tx.ExecContext(ctx,
		`UPDATE bench_approvals SET state = ?, decided_at = ?
		  WHERE id = ? AND state = 'pending' AND expires_at > ?`, state, now, id, now)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return errBenchNoRow
	}
	if approve {
		if _, err := tx.ExecContext(ctx,
			`UPDATE bench_approvals SET state = 'withdrawn', decided_at = ?
			  WHERE tie_group = (SELECT tie_group FROM bench_approvals WHERE id = ?)
			    AND id <> ? AND state IN ('pending','approved')`, now, id, id); err != nil {
			return err
		}
	}
	if approve && graphChange {
		if _, err := tx.ExecContext(ctx,
			`INSERT OR REPLACE INTO bench_org_edges (org_id, manager_id, report_id, scope)
			 VALUES (?, ?, ?, ?)`, benchOrgID, "graph-change", id, "budget"); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE bench_society_meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'graph_version'`); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// societySpend redeems an approval's token: the nonce is spent once, and only
// while the approval is live.
func societySpend(ctx context.Context, db *sql.DB, id string) error {
	now := time.Now().UTC()
	res, err := db.ExecContext(ctx,
		`UPDATE bench_approvals SET state = 'spent', decided_at = ?
		  WHERE nonce = ? AND state = 'approved' AND expires_at > ?`, now, "nonce-"+id, now)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return errBenchNoRow
	}
	return nil
}

// societyExpireSweep expires every pending or approved row past its
// deadline, as A4's sweep does, so an approval never spent ends too.
func societyExpireSweep(ctx context.Context, db *sql.DB) error {
	now := time.Now().UTC()
	_, err := db.ExecContext(ctx,
		`UPDATE bench_approvals SET state = 'expired', decided_at = ?
		  WHERE state IN ('pending','approved') AND expires_at <= ?`, now, now)
	return err
}
