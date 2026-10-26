package society

// Approval rows (RFC 0029 Phase 3 plan S-3 and S-6): the store opens a row
// with its nonce, keeps one open row per actor and action, moves a row only
// from the state its caller names and only along the queue's lifecycle, and
// clears a settled row's payload.

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// action is a persona's hold on a publish, with the turn it releases.
func action(id, actor string) Approval {
	return Approval{ID: id, Kind: KindAction, ActorID: actor, ActionHash: "hash-" + id,
		ExpiresAt: testClock.Add(24 * time.Hour),
		Held:      &HeldTurn{ProtectionLevel: "internal", CascadeDepth: 1, Origin: "group:lab"},
		Payload:   json.RawMessage(`{"class":"respond","text":"the draft"}`)}
}

// graphChange is an operator's proposed organization change.
func graphChange(id string) Approval {
	return Approval{ID: id, Kind: KindGraphChange, ActorID: "operator-1", ActionHash: "hash-" + id,
		ExpiresAt: testClock.Add(24 * time.Hour), Payload: json.RawMessage(`{"add_edge":["moss-fox","tide-wren"]}`)}
}

func TestOpenApproval_OpensAPendingRowWithAFreshNonce(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	a, err := s.OpenApproval(ctx, action("hold-1", "moss-fox"))
	require.NoError(t, err)
	b, err := s.OpenApproval(ctx, action("hold-2", "moss-fox"))
	require.NoError(t, err)

	assert.Equal(t, StatePending, a.State)
	assert.Equal(t, testClock, a.CreatedAt)
	assert.GreaterOrEqual(t, len(a.Nonce), 32, "the nonce is a random token, not a counter")
	assert.NotEqual(t, a.Nonce, b.Nonce)

	got, err := s.Approval(ctx, "hold-1")
	require.NoError(t, err)
	assert.Equal(t, a, got, "the row reads back as it was opened")
	assert.JSONEq(t, `{"class":"respond","text":"the draft"}`, string(got.Payload))
}

func TestOpenApproval_KeepsOneOpenRowPerActorAndAction(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	first, err := s.OpenApproval(ctx, action("hold-1", "moss-fox"))
	require.NoError(t, err)

	retry := action("hold-2", "moss-fox")
	retry.ActionHash = first.ActionHash
	_, err = s.OpenApproval(ctx, retry)
	require.ErrorIs(t, err, ErrOpenApprovalExists)

	open, err := s.ListApprovals(ctx, ApprovalQuery{Open: true, ActorID: "moss-fox", ActionHash: first.ActionHash})
	require.NoError(t, err)
	require.Len(t, open, 1, "the retry finds the open row it should reuse (E-11)")
	assert.Equal(t, "hold-1", open[0].ID)

	_, err = s.MoveApproval(ctx, "hold-1", Move{From: StatePending, To: StateDenied,
		DecidedBy: "operator-1", DecidedAt: testClock})
	require.NoError(t, err)
	_, err = s.OpenApproval(ctx, retry)
	assert.NoError(t, err, "a settled row frees its hash")
}

func TestOpenApproval_RefusesARowTheQueueCannotHold(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	for name, bad := range map[string]func(*Approval){
		"no id":             func(a *Approval) { a.ID = "" },
		"no actor":          func(a *Approval) { a.ActorID = "" },
		"no action hash":    func(a *Approval) { a.ActionHash = "" },
		"no expiry":         func(a *Approval) { a.ExpiresAt = time.Time{} },
		"unknown kind":      func(a *Approval) { a.Kind = "vote" },
		"payload not JSON":  func(a *Approval) { a.Payload = json.RawMessage(`{"class":`) },
		"caller's nonce":    func(a *Approval) { a.Nonce = "chosen" },
		"caller's state":    func(a *Approval) { a.State = StateApproved },
		"decided already":   func(a *Approval) { a.DecidedBy, a.DecidedAt = "operator-1", testClock },
		"tie with no group": func(a *Approval) { a.Kind, a.Held = KindTie, nil },
		"held graph change": func(a *Approval) { a.Kind = KindGraphChange },
		"negative depth":    func(a *Approval) { a.Held.CascadeDepth = -1 },
	} {
		t.Run(name, func(t *testing.T) {
			a := action("hold-"+strings.ReplaceAll(name, " ", "-"), "moss-fox")
			bad(&a)
			_, err := s.OpenApproval(ctx, a)
			require.Error(t, err)
			_, err = s.Approval(ctx, a.ID)
			assert.ErrorIs(t, err, ErrNotFound, "a refused row must not be stored")
		})
	}
}

func TestMoveApproval_FollowsTheQueuesLifecycle(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	_, err := s.OpenApproval(ctx, action("hold-1", "moss-fox"))
	require.NoError(t, err)

	approved, err := s.MoveApproval(ctx, "hold-1", Move{From: StatePending, To: StateApproved,
		DecidedBy: "operator-1", DecidedAt: testClock})
	require.NoError(t, err)
	assert.Equal(t, StateApproved, approved.State)
	assert.Equal(t, "operator-1", approved.DecidedBy)
	assert.NotEmpty(t, approved.Payload, "an approved row keeps what its release runs")

	spent, err := s.MoveApproval(ctx, "hold-1", Move{From: StateApproved, To: StateSpent})
	require.NoError(t, err)
	assert.Equal(t, StateSpent, spent.State)
	assert.Equal(t, "operator-1", spent.DecidedBy, "a spend keeps who approved")
	assert.Nil(t, spent.Payload, "a settled row keeps no text (G-11)")
}

func TestMoveApproval_ReportsALostRace(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	_, err := s.OpenApproval(ctx, action("hold-1", "moss-fox"))
	require.NoError(t, err)
	_, err = s.MoveApproval(ctx, "hold-1", Move{From: StatePending, To: StateExpired})
	require.NoError(t, err)

	_, err = s.MoveApproval(ctx, "hold-1", Move{From: StatePending, To: StateApproved,
		DecidedBy: "operator-1", DecidedAt: testClock})
	require.ErrorIs(t, err, ErrWrongState, "the sweep got there first")
	var wrong *WrongStateError
	require.ErrorAs(t, err, &wrong)
	assert.Equal(t, StateExpired, wrong.Got)
	got, err := s.Approval(ctx, "hold-1")
	require.NoError(t, err)
	assert.Equal(t, StateExpired, got.State, "the lost race must not overwrite the winner")

	_, err = s.MoveApproval(ctx, "no-such-row", Move{From: StatePending, To: StateDenied})
	assert.ErrorIs(t, err, ErrNotFound)
}

func TestMoveApproval_RefusesAMoveOffTheLifecycle(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	for _, m := range []Move{
		{From: StatePending, To: StateSpent},
		{From: StatePending, To: StatePending},
		{From: StateApproved, To: StateDenied},
		{From: StateApproved, To: StatePending},
		{From: StateSpent, To: StateApproved},
		{From: StateDenied, To: StatePending},
		{From: StatePending, To: "lost"},
		{From: StatePending, To: StateApproved, DecidedBy: "operator-1"},
		{From: StatePending, To: StateApproved, DecidedAt: testClock},
	} {
		_, err := s.OpenApproval(ctx, action("hold-1", "moss-fox"))
		require.NoError(t, err)
		_, err = s.MoveApproval(ctx, "hold-1", m)
		assert.ErrorIs(t, err, ErrInvalid, "%s → %s", m.From, m.To)
		_, err = s.db.Exec(`DELETE FROM society_approvals`)
		require.NoError(t, err)
	}
}

func TestMoveApproval_MarksTheRowUndelivered(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	_, err := s.OpenApproval(ctx, action("hold-1", "moss-fox"))
	require.NoError(t, err)
	// RFC 0009 PR 14 writes delivered_at; until then only a test can.
	_, err = s.db.Exec(`UPDATE society_approvals SET delivered_at = ?`, testClock.Unix())
	require.NoError(t, err)

	got, err := s.MoveApproval(ctx, "hold-1", Move{From: StatePending, To: StateApproved,
		DecidedBy: "operator-1", DecidedAt: testClock})
	require.NoError(t, err)
	assert.True(t, got.DeliveredAt.IsZero(), "each new state is owed its own decision event")
}

func TestListApprovals_SelectsByEachField(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	open := func(a Approval, created time.Time) {
		t.Helper()
		s.now = func() time.Time { return created }
		_, err := s.OpenApproval(ctx, a)
		require.NoError(t, err)
	}
	soon := action("hold-soon", "moss-fox")
	soon.ExpiresAt = testClock.Add(time.Minute)
	open(soon, testClock)
	open(action("hold-late", "moss-fox"), testClock.Add(time.Second))
	open(action("hold-other", "tide-wren"), testClock.Add(2*time.Second))
	tie := Approval{ID: "tie-a", Kind: KindTie, ActorID: "tide-wren", ActionHash: "directive-7",
		TieGroup: "tie-7", ExpiresAt: testClock.Add(time.Hour)}
	open(tie, testClock.Add(3*time.Second))
	_, err := s.MoveApproval(ctx, "hold-late", Move{From: StatePending, To: StateApproved,
		DecidedBy: "operator-1", DecidedAt: testClock})
	require.NoError(t, err)
	_, err = s.MoveApproval(ctx, "hold-other", Move{From: StatePending, To: StateWithdrawn})
	require.NoError(t, err)

	ids := func(q ApprovalQuery) []string {
		t.Helper()
		rows, err := s.ListApprovals(ctx, q)
		require.NoError(t, err)
		var out []string
		for _, r := range rows {
			out = append(out, r.ID)
		}
		return out
	}
	assert.Equal(t, []string{"hold-soon", "hold-late", "hold-other", "tie-a"}, ids(ApprovalQuery{}), "oldest first")
	assert.Equal(t, []string{"hold-soon", "tie-a"}, ids(ApprovalQuery{State: StatePending}))
	assert.Equal(t, []string{"hold-soon", "hold-late", "tie-a"}, ids(ApprovalQuery{Open: true}))
	assert.Equal(t, []string{"hold-soon", "hold-late"}, ids(ApprovalQuery{ActorID: "moss-fox"}))
	assert.Equal(t, []string{"tie-a"}, ids(ApprovalQuery{TieGroup: "tie-7"}))
	assert.Equal(t, []string{"hold-soon"},
		ids(ApprovalQuery{Open: true, ExpiredBy: testClock.Add(time.Minute)}), "what the sweep ends")
	assert.Equal(t, []string{"hold-soon", "hold-late"}, ids(ApprovalQuery{Limit: 2}))

	_, err = s.ListApprovals(ctx, ApprovalQuery{State: StatePending, Open: true})
	assert.ErrorIs(t, err, ErrInvalid, "a state and the open pair cannot both be asked for")
}

// The open-row lookup a retried hold makes must use the open-row index, which
// SQLite takes only when the query repeats the index's condition (PR 2 F-16).
func TestListApprovals_TheOpenRowLookupUsesTheOpenRowIndex(t *testing.T) {
	s, _ := newTestStore(t)
	query, args, err := approvalQuerySQL(ApprovalQuery{Open: true, ActorID: "moss-fox", ActionHash: "h"})
	require.NoError(t, err)
	rows, err := s.db.Query("EXPLAIN QUERY PLAN "+query, args...)
	require.NoError(t, err)
	defer func() { _ = rows.Close() }()
	var plan []string
	for rows.Next() {
		var id, parent, unused int
		var detail string
		require.NoError(t, rows.Scan(&id, &parent, &unused, &detail))
		plan = append(plan, detail)
	}
	require.NoError(t, rows.Err())
	assert.Contains(t, strings.Join(plan, "\n"), "ux_society_approvals_open")
}
