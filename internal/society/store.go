// Package society is the society store: the state v0.4.0's organizations
// share rather than one persona holds — the organization graph and its
// version, the approvals waiting for a person, and the keys that sign them
// (RFC 0029 Phase 3, in the SQLite form its 2026-10-20 amendment sets).
//
// The rows live in the channels store's society tables (channels-store v15),
// and the store runs on the channels store's own single connection, handed
// over once at startup; it never opens one of its own (plan S-2). The
// interface is narrow and transactional (S-6): replace and read the graph;
// open, read, list and move an approval row; read or create a key; and one
// call that runs a function inside a single transaction over all of them.
package society

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

// Ops is every read and write the society store makes. A [Store] runs each
// call in a transaction of its own; a [Tx] runs it inside the caller's.
type Ops interface {
	// Graph reads the whole organization graph and its version, in
	// canonical order: organizations and roles by id, members by agent, and
	// edges by manager, report and scope. A store that never had a graph
	// reads as Graph{}.
	Graph(ctx context.Context) (Graph, error)

	// ReplaceGraph replaces the whole graph with g and stores g.Version,
	// which the caller raises in the same transaction (RFC 0012 OQ 2); the
	// store never raises it itself. A version that does not go up is
	// refused with [ErrGraphVersion]. A graph the tables refuse — a member
	// with an unknown role, an edge to a non-member, an agent twice in one
	// organization — fails whole, leaving the old graph. The loader
	// validates the graph first; the store does not repeat it.
	ReplaceGraph(ctx context.Context, g Graph) error

	// OpenApproval stores a new pending row and returns it as stored. The
	// store makes its nonce and its creation time; the caller gives the
	// rest, undecided. An open row for the same actor and action hash fails
	// it with [ErrOpenApprovalExists], so a retried hold looks that row up
	// and reuses it (RFC 0028 E-11).
	OpenApproval(ctx context.Context, a Approval) (Approval, error)

	// Approval reads one row by id, or fails with [ErrNotFound].
	Approval(ctx context.Context, id string) (Approval, error)

	// ListApprovals returns the rows q selects, oldest first.
	ListApprovals(ctx context.Context, q ApprovalQuery) ([]Approval, error)

	// MoveApproval moves a row from m.From to m.To and returns it as
	// stored. A row no longer in m.From fails with a [*WrongStateError],
	// so a lost race is reported, never overwritten; an unknown id fails
	// with [ErrNotFound]; a move off the queue's lifecycle fails with
	// [ErrInvalid]. A move to a settled state clears the payload (RFC 0009
	// G-11), and every move clears the delivery time, since each new state
	// is owed its own decision event (G-10).
	MoveApproval(ctx context.Context, id string, m Move) (Approval, error)

	// Key reads the key kept for purpose, making it on the first call. Two
	// first calls get the same key (S-5).
	Key(ctx context.Context, purpose string) (Key, error)
}

// Tx is the handle a function run by [Store.InTx] reads and writes through.
// Every society read the function needs, the key and the graph included,
// goes through it.
type Tx interface {
	Ops
}

// Store is the society store.
type Store interface {
	Ops

	// InTx runs fn inside one transaction over every society table and
	// commits what it wrote only if it returns nil; any error, or a panic,
	// rolls all of it back. RFC 0009's approve hook and RFC 0012's graph
	// change run through it, so an approval and the change it releases
	// commit together or not at all.
	//
	// The store has one connection, which fn's transaction holds. So fn
	// makes no call on the Store itself, nor on the channels store, nor any
	// network call: each would wait for the connection forever, or hold it
	// across the wait. Events and audit lines go out after InTx returns.
	InTx(ctx context.Context, fn func(Tx) error) error
}

// ConnSource hands the society store the connection it runs on. The SQLite
// channel store is the one source: the society tables share its single
// connection rather than open a second to the same file, where a society
// write can fail a publish with SQLITE_BUSY (plan S-2).
type ConnSource interface {
	SocietyConn() *sql.DB
}

// Graph is the organization graph (RFC 0012 §B) with its version.
type Graph struct {
	// Version goes up with every write that changes the graph; 0 means no
	// graph has been loaded.
	Version       int64
	Organizations []Organization
}

// Organization is one organization with its roles, members and edges. The
// rule that an agent belongs to one organization is the loader's, not the
// store's, so RFC 0012 Phase 5 lifts it without a migration.
type Organization struct {
	ID       string
	Name     string
	Topology string
	Roles    []Role
	Members  []Member
	Manages  []Manages
}

// Role is a role and the highest channel authority it can wield.
type Role struct {
	ID        string
	Authority string
}

// Member is an agent and the role it holds in its organization.
type Member struct {
	AgentID string
	RoleID  string
}

// Manages is one `manages` edge. Scope is empty when the topology has none.
type Manages struct {
	ManagerID string
	ReportID  string
	Scope     string
}

// Kind is what an approval row holds (RFC 0009 G-9).
type Kind string

const (
	// KindAction is a held action or tool call, spent with a token.
	KindAction Kind = "action"
	// KindGraphChange is an organization change an operator proposed.
	KindGraphChange Kind = "graph_change"
	// KindTie is one of a directive tie's rows, always in a tie group.
	KindTie Kind = "tie"
)

// State is where an approval row is in the queue's lifecycle. Pending and
// approved rows are open; the other four are settled and final. The state is
// the used-nonce record: a spend moves an approved row to spent (RFC 0028
// E-8).
type State string

const (
	StatePending   State = "pending"
	StateApproved  State = "approved"
	StateDenied    State = "denied"
	StateExpired   State = "expired"
	StateWithdrawn State = "withdrawn"
	StateSpent     State = "spent"
)

// moves is the queue's lifecycle: every move a row can make.
var moves = map[State][]State{
	StatePending:  {StateApproved, StateDenied, StateExpired, StateWithdrawn},
	StateApproved: {StateSpent, StateExpired, StateWithdrawn},
}

// HeldTurn is the turn a persona's hold releases (RFC 0028 E-13). Only an
// `action` row keeps one, and a task agent's hold, which waits inside its
// step, keeps none.
type HeldTurn struct {
	ProtectionLevel string
	CascadeDepth    int
	Origin          string
}

// Approval is one row of the pending-approval queue. Times are kept to the
// second; a zero time is one not set.
type Approval struct {
	ID         string
	Kind       Kind
	ActorID    string
	ActionHash string
	// Nonce is made by the store when the row opens; the token an approval
	// mints carries it.
	Nonce     string
	State     State
	ExpiresAt time.Time
	Held      *HeldTurn
	// TieGroup is a `tie` row's group, and empty for every other kind.
	TieGroup string
	// Payload is the held action as JSON, kept only while the row is open.
	Payload     json.RawMessage
	DecidedBy   string
	DecidedAt   time.Time
	DeliveredAt time.Time
	CreatedAt   time.Time
}

// Move is one change of an approval row's state. From is the state the
// caller read the row in. DecidedBy and DecidedAt, both or neither, record a
// person's decision; a move without them keeps the row's earlier one, so a
// spend keeps who approved.
type Move struct {
	From      State
	To        State
	DecidedBy string
	DecidedAt time.Time
}

// ApprovalQuery selects approval rows; each field set must match.
type ApprovalQuery struct {
	// State selects one state. Open selects pending and approved rows, and
	// is the form the open-row lookup and the expiry sweep use; it cannot
	// be set with State.
	State State
	Open  bool

	ActorID    string
	ActionHash string
	TieGroup   string

	// ExpiredBy selects rows whose expiry is at or before it.
	ExpiredBy time.Time

	// Limit caps the rows returned; 0 returns all.
	Limit int
}

// KeyApprovalSigning is the purpose of the key that signs approval tokens
// (RFC 0028 OQ 9).
const KeyApprovalSigning = "approval_signing"

// KeySize is the length of every key the store makes: an HMAC-SHA256 key.
const KeySize = 32

// Key is a secret the store keeps. It formats, logs and marshals as
// "[redacted]", so it reaches no log and no route by accident (S-5); only
// its bytes sign.
type Key []byte

const redacted = "[redacted]"

// String serves loggers that take a [fmt.Stringer], as zap.Any does.
func (Key) String() string { return redacted }

// Format serves every fmt verb, %x and %#v included.
func (Key) Format(f fmt.State, _ rune) { _, _ = f.Write([]byte(redacted)) }

// MarshalText serves JSON and every text encoder.
func (Key) MarshalText() ([]byte, error) { return []byte(redacted), nil }

var (
	// ErrNotFound is returned for an approval id the store does not hold.
	ErrNotFound = errors.New("society: no such approval")
	// ErrOpenApprovalExists is returned when an actor already has an open
	// row for the same action hash.
	ErrOpenApprovalExists = errors.New("society: an open approval already holds this actor and action")
	// ErrWrongState is matched by every [*WrongStateError].
	ErrWrongState = errors.New("society: approval is not in the expected state")
	// ErrGraphVersion is returned when a graph write does not raise the
	// version.
	ErrGraphVersion = errors.New("society: a graph write must raise the graph version")
	// ErrInvalid is returned for a call the store refuses before touching
	// a table.
	ErrInvalid = errors.New("society: invalid")
)

// WrongStateError reports a move that lost a race: the row is in Got, not in
// the Want its caller named.
type WrongStateError struct {
	ID   string
	Want State
	Got  State
}

func (e *WrongStateError) Error() string {
	return fmt.Sprintf("society: approval %q is %s, not %s", e.ID, e.Got, e.Want)
}

// Is makes every WrongStateError match [ErrWrongState].
func (e *WrongStateError) Is(target error) bool { return target == ErrWrongState }
