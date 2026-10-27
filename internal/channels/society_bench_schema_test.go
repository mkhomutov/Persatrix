package channels

// Society-store benchmark, part 1 of 3: the society calls a run makes (v0.4.0
// row R2).
//
// Since RFC 0029 Phase 3 PR 3 they run on the real society store over the
// v15 society tables, where R2 ran them on a stand-in schema. Each call is
// what the queue and the organization read will do on the store: the
// approvals package moves a row from the state it read, and the store
// refuses the move if the row has left that state or, for a decide or a
// spend, passed its expiry, so either refusal is a lost race; the authority
// read loads the graph and finds the agent's role and managers in Go, as
// RFC 0012 D-5 computes `directs`.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/mkhomutov/persatrix/internal/society"
)

const benchOrgID = "bench-org"

// benchOperator decides every approval and proposes every graph change.
const benchOperator = "operator"

// benchGraph is one organization over every agent: a lead for each block of
// five, the rest reporting to their block's lead.
func benchGraph(agents []string, version int64) society.Graph {
	org := society.Organization{ID: benchOrgID, Name: "Bench", Topology: "hierarchy",
		Roles: []society.Role{{ID: "lead", Authority: "directive"}, {ID: "member", Authority: "operational"}}}
	for i, agent := range agents {
		role := "member"
		if i%5 == 0 {
			role = "lead"
		}
		org.Members = append(org.Members, society.Member{AgentID: agent, RoleID: role})
		if role == "member" {
			org.Manages = append(org.Manages, society.Manages{ManagerID: agents[i-i%5], ReportID: agent})
		}
	}
	return society.Graph{Version: version, Organizations: []society.Organization{org}}
}

// seedSociety fills the society tables: the graph, the signing key, and
// `history` settled approvals so the indexes are not empty.
func seedSociety(ctx context.Context, st society.Store, agents []string, history int) error {
	if err := st.ReplaceGraph(ctx, benchGraph(agents, 1)); err != nil {
		return fmt.Errorf("society graph: %w", err)
	}
	if _, err := st.Key(ctx, society.KeyApprovalSigning); err != nil {
		return err
	}
	settled := []society.State{society.StateSpent, society.StateDenied, society.StateExpired, society.StateWithdrawn}
	// The store stamps each row's creation as it opens, so each settles then
	// too, as a real row would after it: a decision or a spend before its
	// expiry, and an expiry once the row is due.
	return st.InTx(ctx, func(ctx context.Context, tx society.Tx) error {
		for i := 0; i < history; i++ {
			to := settled[i%len(settled)]
			expires := time.Now().Add(15 * time.Minute)
			if to == society.StateExpired {
				expires = time.Now()
			}
			a := benchHold(fmt.Sprintf("hist-%d", i), agents[i%len(agents)], expires)
			if _, err := tx.OpenApproval(ctx, a); err != nil {
				return err
			}
			from := society.StatePending
			if to == society.StateSpent {
				if _, err := tx.MoveApproval(ctx, a.ID, benchDecision(from, society.StateApproved, time.Now())); err != nil {
					return err
				}
				from = society.StateApproved
			}
			m := society.Move{From: from, To: to}
			if to == society.StateDenied {
				m = benchDecision(from, to, time.Now())
			}
			if _, err := tx.MoveApproval(ctx, a.ID, m); err != nil {
				return err
			}
		}
		return nil
	})
}

// benchHold is a persona's hold on a reply, with the turn it releases and the
// reply's text, as RFC 0028 PR 14 will open it.
func benchHold(id, actor string, expires time.Time) society.Approval {
	payload, _ := json.Marshal(map[string]string{"class": "respond", "target": "group:team",
		"text": "a held reply of a few sentences, kept only while it waits for a person"})
	return society.Approval{ID: id, Kind: society.KindAction, ActorID: actor, ActionHash: "action-" + id,
		ExpiresAt: expires, Payload: payload,
		Held: &society.HeldTurn{ProtectionLevel: "internal", CascadeDepth: 1, Origin: "group:team"}}
}

func benchDecision(from, to society.State, at time.Time) society.Move {
	return society.Move{From: from, To: to, DecidedBy: benchOperator, DecidedAt: at}
}

// societyOrgRead is the read an authority check makes: the graph, then the
// agent's role and authority and its managers with theirs.
func societyOrgRead(ctx context.Context, st society.Store, agent string) error {
	g, err := st.Graph(ctx)
	if err != nil {
		return err
	}
	for _, org := range g.Organizations {
		authority := map[string]string{}
		for _, r := range org.Roles {
			authority[r.ID] = r.Authority
		}
		roles := map[string]string{}
		for _, m := range org.Members {
			roles[m.AgentID] = authority[m.RoleID]
		}
		if _, ok := roles[agent]; !ok {
			continue
		}
		for _, e := range org.Manages {
			if e.ReportID == agent && roles[e.ManagerID] == "" {
				return fmt.Errorf("manager %q has no authority", e.ManagerID)
			}
		}
		return nil
	}
	return fmt.Errorf("agent %q is in no organization", agent)
}

// societyListPending is the operator's approval-queue page.
func societyListPending(ctx context.Context, st society.Store) error {
	_, err := st.ListApprovals(ctx, society.ApprovalQuery{State: society.StatePending, Limit: 50})
	return err
}

// societyOpenApproval holds an action: one pending row with its nonce.
func societyOpenApproval(ctx context.Context, st society.Store, id, actor string, ttl time.Duration) error {
	_, err := st.OpenApproval(ctx, benchHold(id, actor, time.Now().Add(ttl)))
	return err
}

// societyOpenGraphChange holds an operator's organization change.
func societyOpenGraphChange(ctx context.Context, st society.Store, id string, ttl time.Duration) error {
	_, err := st.OpenApproval(ctx, society.Approval{ID: id, Kind: society.KindGraphChange,
		ActorID: benchOperator, ActionHash: "change-" + id, ExpiresAt: time.Now().Add(ttl),
		Payload: json.RawMessage(`{"add_edge":{"scope":"budget"}}`)})
	return err
}

// societyOpenTieGroup opens a tie group whole, every row in one transaction,
// as A4 does, so nothing joins the group later.
func societyOpenTieGroup(ctx context.Context, st society.Store, ids []string, actor, tieGroup string, ttl time.Duration) error {
	expires := time.Now().Add(ttl)
	return st.InTx(ctx, func(ctx context.Context, tx society.Tx) error {
		for _, id := range ids {
			if _, err := tx.OpenApproval(ctx, society.Approval{ID: id, Kind: society.KindTie, ActorID: actor,
				ActionHash: "directive-" + id, TieGroup: tieGroup, ExpiresAt: expires}); err != nil {
				return err
			}
		}
		return nil
	})
}

// errBenchNoRow marks a move that found its row elsewhere or past its
// expiry — a lost race the lifecycle expects (the sweeper expired the row
// first), not a failure.
var errBenchNoRow = errors.New("no row in the expected state")

// lostRace maps a move the store refused for the row's state or its expiry
// to errBenchNoRow.
func lostRace(err error) error {
	if errors.Is(err, society.ErrWrongState) || errors.Is(err, society.ErrExpired) {
		return errBenchNoRow
	}
	return err
}

// societyDecide approves or denies a pending row. Approving withdraws the
// rest of its tie group in the same transaction; with graphChange set it also
// adds an edge to the organization graph and raises its version, so the
// approval and the change commit together or not at all.
func societyDecide(ctx context.Context, st society.Store, id string, approve, graphChange bool) error {
	return lostRace(st.InTx(ctx, func(ctx context.Context, tx society.Tx) error {
		to := society.StateDenied
		if approve {
			to = society.StateApproved
		}
		row, err := tx.MoveApproval(ctx, id, benchDecision(society.StatePending, to, time.Now()))
		if err != nil || !approve {
			return err
		}
		if row.TieGroup != "" {
			if _, err := tx.MoveApprovals(ctx, society.ApprovalQuery{State: society.StatePending, TieGroup: row.TieGroup},
				society.StateWithdrawn); err != nil {
				return err
			}
		}
		if !graphChange {
			return nil
		}
		g, err := tx.Graph(ctx)
		if err != nil {
			return err
		}
		org := &g.Organizations[0]
		org.Manages = append(org.Manages, society.Manages{ManagerID: org.Members[0].AgentID,
			ReportID: org.Members[1].AgentID, Scope: "budget-" + id})
		g.Version++
		return tx.ReplaceGraph(ctx, g)
	}))
}

// societySpend redeems an approval's token: the row is spent once, and only
// while the approval is live.
func societySpend(ctx context.Context, st society.Store, id string) error {
	_, err := st.MoveApproval(ctx, id, society.Move{From: society.StateApproved, To: society.StateSpent})
	return lostRace(err)
}

// societyExpireSweep expires every pending or approved row past its
// deadline, as A4's sweep does, so an approval never spent ends too.
func societyExpireSweep(ctx context.Context, st society.Store) error {
	_, err := st.MoveApprovals(ctx, society.ApprovalQuery{Open: true, ExpiredBy: time.Now()}, society.StateExpired)
	return err
}
