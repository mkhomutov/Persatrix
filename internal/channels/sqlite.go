package channels

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"strings"
	"sync"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"

	_ "modernc.org/sqlite" // pure-Go SQLite driver; matches CGO_ENABLED=0 build (Dockerfile.orchestrator)
)

// DefaultSessionID is the synthetic carve-out applied to every channel /
// message write that arrives without an explicit session_id (RFC 0031
// Phase 1). Phase 2 makes this carve-out a tested invariant of the recall
// path; Phase 1 only pins the storage shape.
//
// Exported so callers outside this package (notably the orchestrator's
// boot-time `resolveSessionID` fallback) reference a single source of
// truth — PR #335 review L2.
const DefaultSessionID = "legacy"

// DefaultEpochID is the run/test-isolation epoch applied to every channel /
// message row that arrives without an explicit epoch (ISSUE-0085 PR 2 —
// channel-store schema v6). Unlike [DefaultSessionID] it is NOT a carve-out:
// the epoch recall predicate (forthcoming) is strict equality with no
// `legacy`-style "always visible" escape, so a fresh epoch sees nothing. PR 2
// only lands the column (backfilled here via the SQL DEFAULT); no writer sets
// a non-default epoch until the gRPC rail (PR 4) lights up the producer.
//
// Cross-language contract: mirrors `agents.epoch_id.DEFAULT_EPOCH_ID` — the
// `'live'` literal the persona-memory migration v12 backfills onto its five
// tiers. A rename here is a conscious break that must move in lock-step with
// the Python leaf.
const DefaultEpochID = "live"

// DefaultPrincipalID is the shared single-tenant principal every `messages`
// row carries when the publish resolved no authenticated identity
// (ISSUE-0130 shape (b) — channel-store schema v12). It is the *absence* of a
// tenant, not a tenant: the whole persona fleet resolves it (agents hold no
// accounts, RFC 0039 §Non-Goals), every autonomous turn resolves it, and
// every caller under `auth.mode: disabled` resolves it.
//
// Cross-language contract: mirrors `agents.principal_id.DEFAULT_PRINCIPAL_ID`
// — the `'local'` literal the persona-memory migration v11 backfills onto its
// tiers, and the value a persona resolves when no `persatrix-principal`
// header arrives. The two stores are disjoint (nothing in `agents/` queries
// `messages`); they meet at the wire, where B2 seeds the replayed event from
// the column this constant defaults. A rename here is a conscious break that
// must move in lock-step with the Python leaf.
//
// Go has no lock-step guard for it — unlike [DefaultEpochID]'s Python twin
// there is no shared config knob to parse — so the migration SQL spells the
// literal out (see migrateV11ToV12) and this constant is the *write-side*
// source of truth only.
const DefaultPrincipalID = "local"

// SessionMetrics is the subset of orchestrator OTEL handles the channel
// store needs for the RFC 0031 `sessions.writes` counter. Defined locally
// so the channels package does not take a dependency on the orchestrator-
// wide instrument struct.
//
// Nil-safe: a nil SessionMetrics value disables emission so unit tests and
// minimal deployments run without OTEL wiring.
type SessionMetrics struct {
	// Writes counts each `CreateChannel` / `CreateChannelWithMembers` /
	// `GetOrCreateDM` / `PublishMessage` write attributed to a
	// session_id. Phase 1 inserts the env-var value verbatim, so
	// cardinality is bounded by the operator-controlled session count.
	Writes metric.Int64Counter
}

// DefaultMaxMessagesPerChannel is the per-channel oldest-first prune cap when
// a config value is not supplied (RFC 0011 §B).
const DefaultMaxMessagesPerChannel = 10_000

// DefaultMaxChannels is the global named-group-channel cap when a config
// value is not supplied (RFC 0011 §B).
const DefaultMaxChannels = 50

// SQLiteOptions configures [NewSQLiteStore].
//
// `MaxChannels` is checked against the named-group population by
// `CreateChannel`. `MaxMessagesPerChannel` is checked inside `PublishMessage`
// after the insert; oldest-first pruning runs in the same transaction so the
// thread-FK cascade resolves atomically with the new write.
//
// `Logger` is optional. When nil the store uses `zap.NewNop()` so callers in
// tests can omit it without losing the orchestrator's structured-logging
// convention (PR #231 review Should-Fix #4: previously every cap-enforcement
// and channel-lifecycle event was silent in production).
type SQLiteOptions struct {
	MaxChannels           int
	MaxMessagesPerChannel int
	Logger                *zap.Logger
	// SessionMetrics is optional; nil disables `sessions.writes`
	// emission. RFC 0031 Phase 1.
	SessionMetrics *SessionMetrics
	// DMDefaultClassification is the RFC 0037 §B `dm_default_classification`
	// knob: the §A confidentiality level [sqliteStore.GetOrCreateDM] stamps
	// onto a DM channel row at creation (DMs open on demand and have no
	// config block to declare one). Wired from [Config.DMDefaultClassification]
	// at startup. Absent or unknown normalizes to `internal` at construction
	// via [NormalizeForStamp] — §A rule (a), so a store built without the
	// option behaves exactly as before this knob existed.
	DMDefaultClassification Classification
}

// NewSQLiteStore opens (or creates) the channel database at `path` and
// applies the schema migration. WAL mode and foreign-key enforcement are
// turned on at connection time — the latter is required for both the
// `messages.thread_id` self-cascade and the `memberships`/`messages` →
// `channels(id)` cascades to fire.
//
// Pass `:memory:` (or `file::memory:?cache=shared`) for unit tests; pass an
// absolute filesystem path for production.
func NewSQLiteStore(path string, opts SQLiteOptions) (ChannelStore, error) {
	if opts.MaxChannels <= 0 {
		opts.MaxChannels = DefaultMaxChannels
	}
	if opts.MaxMessagesPerChannel <= 0 {
		opts.MaxMessagesPerChannel = DefaultMaxMessagesPerChannel
	}
	logger := opts.Logger
	if logger == nil {
		logger = zap.NewNop()
	}

	dsn := buildDSN(path)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("channels: open %s: %w", path, err)
	}
	// MaxOpenConns is intentionally pinned to 1 for v0.3.0.
	//
	// Rationale (per PR #231 review): WAL mode permits concurrent readers and
	// modernc.org/sqlite is connection-safe, so the file is technically
	// capable of more. We pin to 1 anyway because:
	//
	//  1. Every write path in this PR (CreateChannel cap check, PublishMessage
	//     publish+prune, GetOrCreateDM existence check + insert) wraps a
	//     read-then-write transaction whose correctness depends on the lack
	//     of an interleaved writer. A pool of >1 + WAL would still serialise
	//     writers via SQLITE_BUSY, but the cap-check TOCTOU window in
	//     CreateChannel widens.
	//  2. PR 1 ships persistence only — no REST traffic is layered on top yet,
	//     so the read-throughput penalty is not measurable.
	//
	// TODO(rfc0011-pr2): when the REST router lands and concurrent reads
	// matter, lift the cap to a small bounded value (4–8), gate writers via
	// busy_timeout/sqlite-side serialisation, and replace the cap-check
	// SELECT+INSERT with an idempotent INSERT-or-fail.
	db.SetMaxOpenConns(1)

	if err := applySchema(db); err != nil {
		_ = db.Close()
		return nil, err
	}

	store := &sqliteStore{
		db:                    db,
		maxChannels:           opts.MaxChannels,
		maxMessagesPerChannel: opts.MaxMessagesPerChannel,
		logger:                logger,
		sessionMetrics:        opts.SessionMetrics,
		// §A rule (a) at the construction boundary: the DM stamp value is
		// normalized once here so GetOrCreateDM writes a known lattice level
		// unconditionally (absent/unknown → internal, never public).
		dmDefaultClassification: NormalizeForStamp(opts.DMDefaultClassification),
	}
	// RFC 0036: settle `messages_fts` existence once, now that applySchema has
	// run migration v10. Cached so recall never re-probes `sqlite_master` per
	// call against the single pinned connection. See [sqliteStore.probeMessagesFTS].
	store.ftsAvailable = store.probeMessagesFTS()
	return store, nil
}

// buildDSN attaches the PRAGMA flags every channel-store connection needs:
// foreign keys ON (cascade enforcement), WAL mode (consistent with the rest
// of the project), and a generous busy timeout so concurrent tests don't
// flake under SQLite's writer-exclusion contract.
//
// `path` may itself carry a query string — the doc-comment on
// [NewSQLiteStore] advertises `file::memory:?cache=shared` as a supported
// form. We split the existing query off, merge the caller's params into the
// `_pragma` Values, and emit a single `?`-separated DSN. Concatenating
// `path + "?" + q.Encode()` (the pre-ISSUE-0049 form) produced two `?`
// separators on a `file:` URI, which the SQLite driver parsed as a single
// malformed `cache` value and rejected with "no such cache mode" — every
// PRAGMA was dropped on the floor (or the open failed outright).
func buildDSN(path string) string {
	base, existing := path, ""
	if i := strings.Index(path, "?"); i >= 0 {
		base, existing = path[:i], path[i+1:]
	}
	// url.ParseQuery on an empty string returns an empty Values + nil error;
	// on a non-empty malformed string it returns a partial Values + a non-nil
	// error. We accept partials silently because the SQLite driver itself is
	// the authoritative parser of the resulting DSN — surfacing a parse error
	// here would block paths the driver would have accepted.
	q, _ := url.ParseQuery(existing)
	q.Add("_pragma", "foreign_keys(1)")
	q.Add("_pragma", "journal_mode(WAL)")
	q.Add("_pragma", "busy_timeout(5000)")
	return base + "?" + q.Encode()
}

// sqliteStore is the concrete [ChannelStore] backed by SQLite.
//
// `dmMu` serialises [GetOrCreateDM] for a given canonical id so a publish-
// against-new-DM race cannot insert the channel row twice. The lock is
// process-local — multi-process deployments are out of scope for v0.3.0
// (Non-Goal: cross-node federation).
type sqliteStore struct {
	db                    *sql.DB
	maxChannels           int
	maxMessagesPerChannel int
	logger                *zap.Logger
	sessionMetrics        *SessionMetrics

	// ftsAvailable records whether the `messages_fts` index exists, probed once
	// at construction (RFC 0036). Write-once before the store is handed out, then
	// read-only — so no synchronisation is needed alongside dmMu.
	ftsAvailable bool

	// dmDefaultClassification is the §A level GetOrCreateDM stamps onto a DM
	// row at creation (RFC 0037 §B). Normalized to a known lattice level at
	// construction ([NormalizeForStamp]); write-once then read-only, like
	// ftsAvailable.
	dmDefaultClassification Classification

	dmMu sync.Mutex
}

// recordSessionWrite increments the `sessions.writes` counter with the
// `session_id` attribute set. No-op when SessionMetrics is unset (test
// fixtures / channels-disabled deployments).
func (s *sqliteStore) recordSessionWrite(ctx context.Context, sessionID string) {
	if s.sessionMetrics == nil || s.sessionMetrics.Writes == nil {
		return
	}
	s.sessionMetrics.Writes.Add(ctx, 1, metric.WithAttributes(attribute.String("session_id", sessionID)))
}

// Close implements [ChannelStore.Close].
func (s *sqliteStore) Close() error { return s.db.Close() }
