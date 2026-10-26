package society

// The organization graph on the society tables (RFC 0029 Phase 3 plan S-4):
// read whole in canonical order, replaced whole with its version.

import (
	"context"
	"database/sql"
	"fmt"
)

// Graph implements [Ops].
func (t *sqliteTx) Graph(ctx context.Context) (Graph, error) {
	var g Graph
	if err := t.tx.QueryRowContext(ctx,
		`SELECT graph_version FROM society_meta WHERE id = 1`).Scan(&g.Version); err != nil {
		return Graph{}, fmt.Errorf("society: read graph version: %w", err)
	}
	at := map[string]int{} // organization id → its index in g.Organizations
	org := func(id string) *Organization { return &g.Organizations[at[id]] }

	if err := t.each(ctx, `SELECT id, name, topology FROM society_organizations ORDER BY id`,
		func(rows *sql.Rows) error {
			var o Organization
			if err := rows.Scan(&o.ID, &o.Name, &o.Topology); err != nil {
				return err
			}
			at[o.ID] = len(g.Organizations)
			g.Organizations = append(g.Organizations, o)
			return nil
		}); err != nil {
		return Graph{}, err
	}
	if err := t.each(ctx, `SELECT organization_id, id, authority FROM society_roles
		ORDER BY organization_id, id`, func(rows *sql.Rows) error {
		var id string
		var r Role
		if err := rows.Scan(&id, &r.ID, &r.Authority); err != nil {
			return err
		}
		org(id).Roles = append(org(id).Roles, r)
		return nil
	}); err != nil {
		return Graph{}, err
	}
	if err := t.each(ctx, `SELECT organization_id, agent_id, role_id FROM society_members
		ORDER BY organization_id, agent_id`, func(rows *sql.Rows) error {
		var id string
		var m Member
		if err := rows.Scan(&id, &m.AgentID, &m.RoleID); err != nil {
			return err
		}
		org(id).Members = append(org(id).Members, m)
		return nil
	}); err != nil {
		return Graph{}, err
	}
	if err := t.each(ctx, `SELECT organization_id, manager_id, report_id, scope FROM society_manages
		ORDER BY organization_id, manager_id, report_id, scope`, func(rows *sql.Rows) error {
		var id string
		var e Manages
		if err := rows.Scan(&id, &e.ManagerID, &e.ReportID, &e.Scope); err != nil {
			return err
		}
		org(id).Manages = append(org(id).Manages, e)
		return nil
	}); err != nil {
		return Graph{}, err
	}
	return g, nil
}

// ReplaceGraph implements [Ops]. It runs under a savepoint, so a graph the
// tables refuse leaves the old one even inside a caller's transaction that
// goes on to commit. Every row is deleted and inserted again, never replaced
// in place: `INSERT OR REPLACE` on a member would delete their edges with
// the row (PR 2 F-16).
func (t *sqliteTx) ReplaceGraph(ctx context.Context, g Graph) (err error) {
	if _, err := t.tx.ExecContext(ctx, `SAVEPOINT society_replace_graph`); err != nil {
		return fmt.Errorf("society: replace graph: %w", err)
	}
	defer func() {
		if err != nil {
			_, _ = t.tx.ExecContext(ctx, `ROLLBACK TO society_replace_graph`)
		}
		_, _ = t.tx.ExecContext(ctx, `RELEASE society_replace_graph`)
	}()

	if err := t.raiseGraphVersion(ctx, g.Version); err != nil {
		return err
	}
	// Deleting an organization deletes its roles, members and edges (v15).
	if _, err := t.tx.ExecContext(ctx, `DELETE FROM society_organizations`); err != nil {
		return fmt.Errorf("society: clear the graph: %w", err)
	}
	for _, o := range g.Organizations {
		if err := t.insertOrganization(ctx, o); err != nil {
			return err
		}
	}
	return nil
}

// raiseGraphVersion stores version if it is above the stored one. The update
// must change the one meta row, so a missing row fails as loudly as a
// version that did not go up.
func (t *sqliteTx) raiseGraphVersion(ctx context.Context, version int64) error {
	res, err := t.tx.ExecContext(ctx,
		`UPDATE society_meta SET graph_version = ? WHERE id = 1 AND graph_version < ?`, version, version)
	if err != nil {
		return fmt.Errorf("society: write graph version: %w", err)
	}
	if n, err := res.RowsAffected(); err != nil || n == 1 {
		return err
	}
	var stored int64
	if err := t.tx.QueryRowContext(ctx,
		`SELECT graph_version FROM society_meta WHERE id = 1`).Scan(&stored); err != nil {
		return fmt.Errorf("society: read graph version: %w", err)
	}
	return fmt.Errorf("%w: %d is not above %d", ErrGraphVersion, version, stored)
}

func (t *sqliteTx) insertOrganization(ctx context.Context, o Organization) error {
	if _, err := t.tx.ExecContext(ctx,
		`INSERT INTO society_organizations (id, name, topology) VALUES (?, ?, ?)`,
		o.ID, o.Name, o.Topology); err != nil {
		return fmt.Errorf("society: organization %q: %w", o.ID, err)
	}
	for _, r := range o.Roles {
		if _, err := t.tx.ExecContext(ctx,
			`INSERT INTO society_roles (organization_id, id, authority) VALUES (?, ?, ?)`,
			o.ID, r.ID, r.Authority); err != nil {
			return fmt.Errorf("society: organization %q role %q: %w", o.ID, r.ID, err)
		}
	}
	for _, m := range o.Members {
		if _, err := t.tx.ExecContext(ctx,
			`INSERT INTO society_members (organization_id, agent_id, role_id) VALUES (?, ?, ?)`,
			o.ID, m.AgentID, m.RoleID); err != nil {
			return fmt.Errorf("society: organization %q member %q: %w", o.ID, m.AgentID, err)
		}
	}
	for _, e := range o.Manages {
		if _, err := t.tx.ExecContext(ctx,
			`INSERT INTO society_manages (organization_id, manager_id, report_id, scope) VALUES (?, ?, ?, ?)`,
			o.ID, e.ManagerID, e.ReportID, e.Scope); err != nil {
			return fmt.Errorf("society: organization %q edge %q → %q: %w", o.ID, e.ManagerID, e.ReportID, err)
		}
	}
	return nil
}

// each runs query and hands every row to scan.
func (t *sqliteTx) each(ctx context.Context, query string, scan func(*sql.Rows) error, args ...any) error {
	rows, err := t.tx.QueryContext(ctx, query, args...)
	if err != nil {
		return fmt.Errorf("society: %w", err)
	}
	defer func() { _ = rows.Close() }()
	for rows.Next() {
		if err := scan(rows); err != nil {
			return fmt.Errorf("society: %w", err)
		}
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("society: %w", err)
	}
	return nil
}
