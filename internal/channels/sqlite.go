package channels

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"sync"
	"time"

	"go.uber.org/zap"

	_ "modernc.org/sqlite" // pure-Go SQLite driver; matches CGO_ENABLED=0 build (Dockerfile.orchestrator)
)

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

	return &sqliteStore{
		db:                    db,
		maxChannels:           opts.MaxChannels,
		maxMessagesPerChannel: opts.MaxMessagesPerChannel,
		logger:                logger,
	}, nil
}

// buildDSN attaches the PRAGMA flags every channel-store connection needs:
// foreign keys ON (cascade enforcement), WAL mode (consistent with the rest
// of the project), and a generous busy timeout so concurrent tests don't
// flake under SQLite's writer-exclusion contract.
func buildDSN(path string) string {
	q := url.Values{}
	q.Set("_pragma", "foreign_keys(1)")
	q.Add("_pragma", "journal_mode(WAL)")
	q.Add("_pragma", "busy_timeout(5000)")
	return path + "?" + q.Encode()
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

	dmMu sync.Mutex
}

// Close implements [ChannelStore.Close].
func (s *sqliteStore) Close() error { return s.db.Close() }

// CreateChannel implements [ChannelStore.CreateChannel].
func (s *sqliteStore) CreateChannel(ctx context.Context, ch Channel) error {
	if !ch.Type.Valid() {
		return fmt.Errorf("%w: %q", ErrInvalidChannelType, ch.Type)
	}
	if ch.ID == "" {
		return errors.New("channels: channel id is required")
	}
	if ch.Type == ChannelTypeGroup && ch.Name == "" {
		return errors.New("channels: group channel requires a name")
	}
	// PR #231 review-2 Should-Fix #1: enforce channelNamePattern at the
	// store boundary too, not only in the loader (Config.Validate). The REST
	// surface in PR 2 will route through CreateChannel and would otherwise
	// accept group names that the next restart's LoadConfig would reject,
	// breaking the config-vs-store parity invariant from RFC 0011 §B.
	// DM and thread channels store their canonical id in the `name` column
	// as a placeholder (see storedName below) and are exempt — only
	// user-declared group names are user-visible.
	if ch.Type == ChannelTypeGroup && !channelNamePattern.MatchString(ch.Name) {
		return fmt.Errorf("channels: group channel name %q does not match %s",
			ch.Name, channelNamePattern.String())
	}
	// PR #231 review SF-2: make CreateChannel the canonical-id authority for
	// group channels. The previous implementation accepted any
	// `(ID, Name)` pair so a REST handler could insert a row whose PK
	// disagreed with its display name (e.g. ID=`group:foo`, Name=`bar`).
	// Future memory and observability rows reference the canonical id; a
	// drift here would mis-route every downstream lookup. We require the
	// caller to supply the exact PK they intend to use rather than mutate
	// the input — the wrapper signature stays value-receiver so a
	// surprising in-place rewrite cannot happen at higher layers.
	if ch.Type == ChannelTypeGroup && ch.ID != "group:"+ch.Name {
		return fmt.Errorf("%w: group channel id %q must be \"group:\"+name (name=%q)",
			ErrInvalidChannelType, ch.ID, ch.Name)
	}
	if ch.CreatedAt.IsZero() {
		ch.CreatedAt = time.Now().UTC()
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	if ch.Type == ChannelTypeGroup {
		var groupCount int
		if err := tx.QueryRowContext(ctx,
			`SELECT COUNT(1) FROM channels WHERE channel_type = 'group'`).
			Scan(&groupCount); err != nil {
			return fmt.Errorf("channels: count groups: %w", err)
		}
		if groupCount >= s.maxChannels {
			return fmt.Errorf("%w: cap=%d", ErrChannelCapExceeded, s.maxChannels)
		}
	}

	// SF-4 (PR 2 of RFC 0011): channels.name is now nullable. Group
	// channels store their declared display name; DM and thread channels
	// store NULL. Uniqueness is enforced by the partial index
	// `ux_channels_name_group` (group rows only) — see migrateV1ToV2.
	var nameArg any
	if ch.Type == ChannelTypeGroup {
		nameArg = ch.Name
	} else {
		nameArg = nil
	}

	_, err = tx.ExecContext(ctx,
		`INSERT INTO channels (id, name, channel_type, description, created_at)
		 VALUES (?, ?, ?, ?, ?)`,
		ch.ID, nameArg, string(ch.Type), ch.Description, ch.CreatedAt,
	)
	if err != nil {
		if isUniqueViolation(err) {
			return fmt.Errorf("%w: %s", ErrChannelExists, ch.ID)
		}
		return fmt.Errorf("channels: insert channel: %w", err)
	}

	if err := tx.Commit(); err != nil {
		return err
	}
	// PR #231 review Should-Fix #4: surface lifecycle events through the
	// orchestrator's structured logger. Info-level matches the cardinality
	// (one event per declared channel + one per implicit DM/thread).
	s.logger.Info("channels: channel created",
		zap.String("channel_id", ch.ID),
		zap.String("channel_type", string(ch.Type)))
	return nil
}

// CreateChannelWithMembers implements [ChannelStore.CreateChannelWithMembers].
//
// PR #245 review (High): the REST `POST /api/v1/channels` handler used
// to call CreateChannel followed by an N-call AddMember loop, all
// outside any transaction. A failure mid-loop left the channel row
// committed but with only a prefix of the requested membership; the
// client's natural retry then hit ErrChannelExists \u2192 409 and the
// remaining members were never added. This helper makes the bundle
// atomic at the store boundary so handlers no longer need to compose a
// rollback path of their own.
//
// Member validation runs inside the transaction so the same
// `ErrInvalidParticipantID` / `ErrInvalidRespondPolicy` sentinels surface
// as before \u2014 only the persistence side becomes all-or-nothing.
func (s *sqliteStore) CreateChannelWithMembers(ctx context.Context, ch Channel, members []Member) error {
	// Pre-flight validation duplicates the checks in [CreateChannel] /
	// [AddMember] so we can fail fast before touching the database. Any
	// future change to those rules MUST be mirrored here \u2014 the unit test
	// TestSQLiteStore_CreateChannelWithMembers_AtomicOnPartialFailure
	// pins the rollback contract for one concrete invalid-member case.
	if !ch.Type.Valid() {
		return fmt.Errorf("%w: %q", ErrInvalidChannelType, ch.Type)
	}
	if ch.ID == "" {
		return errors.New("channels: channel id is required")
	}
	if ch.Type == ChannelTypeGroup && ch.Name == "" {
		return errors.New("channels: group channel requires a name")
	}
	if ch.Type == ChannelTypeGroup && !channelNamePattern.MatchString(ch.Name) {
		return fmt.Errorf("channels: group channel name %q does not match %s",
			ch.Name, channelNamePattern.String())
	}
	if ch.Type == ChannelTypeGroup && ch.ID != "group:"+ch.Name {
		return fmt.Errorf("%w: group channel id %q must be \"group:\"+name (name=%q)",
			ErrInvalidChannelType, ch.ID, ch.Name)
	}
	if ch.CreatedAt.IsZero() {
		ch.CreatedAt = time.Now().UTC()
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	if ch.Type == ChannelTypeGroup {
		var groupCount int
		if err := tx.QueryRowContext(ctx,
			`SELECT COUNT(1) FROM channels WHERE channel_type = 'group'`).
			Scan(&groupCount); err != nil {
			return fmt.Errorf("channels: count groups: %w", err)
		}
		if groupCount >= s.maxChannels {
			return fmt.Errorf("%w: cap=%d", ErrChannelCapExceeded, s.maxChannels)
		}
	}

	var nameArg any
	if ch.Type == ChannelTypeGroup {
		nameArg = ch.Name
	} else {
		nameArg = nil
	}

	if _, err := tx.ExecContext(ctx,
		`INSERT INTO channels (id, name, channel_type, description, created_at)
		 VALUES (?, ?, ?, ?, ?)`,
		ch.ID, nameArg, string(ch.Type), ch.Description, ch.CreatedAt,
	); err != nil {
		if isUniqueViolation(err) {
			return fmt.Errorf("%w: %s", ErrChannelExists, ch.ID)
		}
		return fmt.Errorf("channels: insert channel: %w", err)
	}

	now := time.Now().UTC()
	for _, m := range members {
		if err := validateParticipantID(m.ParticipantID); err != nil {
			return err
		}
		policy := m.RespondPolicy
		if policy == "" {
			policy = RespondWhenMentioned
		}
		if !policy.Valid() {
			return fmt.Errorf("%w: %q", ErrInvalidRespondPolicy, policy)
		}
		joinedAt := m.JoinedAt
		if joinedAt.IsZero() {
			joinedAt = now
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
			 VALUES (?, ?, ?, ?)
			 ON CONFLICT(channel_id, participant_id) DO NOTHING`,
			ch.ID, m.ParticipantID, string(policy), joinedAt,
		); err != nil {
			// FK violations are unexpected here \u2014 we just inserted the
			// parent row in the same transaction \u2014 but classify them the
			// same way [AddMember] does for symmetry.
			if isForeignKeyViolation(err) {
				return fmt.Errorf("%w: %s", ErrChannelNotFound, ch.ID)
			}
			return fmt.Errorf("channels: add member %s: %w", m.ParticipantID, err)
		}
	}

	if err := tx.Commit(); err != nil {
		return err
	}
	s.logger.Info("channels: channel created with members",
		zap.String("channel_id", ch.ID),
		zap.String("channel_type", string(ch.Type)),
		zap.Int("member_count", len(members)))
	return nil
}

// GetChannel implements [ChannelStore.GetChannel].
func (s *sqliteStore) GetChannel(ctx context.Context, id string) (Channel, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT id, name, channel_type, description, created_at
		   FROM channels WHERE id = ?`, id)
	var ch Channel
	var typ string
	// SF-4: name is nullable post-v2 — DM/thread rows hold NULL.
	var name sql.NullString
	if err := row.Scan(&ch.ID, &name, &typ, &ch.Description, &ch.CreatedAt); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Channel{}, fmt.Errorf("%w: %s", ErrChannelNotFound, id)
		}
		return Channel{}, fmt.Errorf("channels: scan channel: %w", err)
	}
	ch.Type = ChannelType(typ)
	if name.Valid {
		ch.Name = name.String
	}
	return ch, nil
}

// ListChannels implements [ChannelStore.ListChannels].
func (s *sqliteStore) ListChannels(ctx context.Context) ([]Channel, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, name, channel_type, description, created_at
		   FROM channels ORDER BY created_at ASC`)
	if err != nil {
		return nil, fmt.Errorf("channels: list: %w", err)
	}
	defer func() { _ = rows.Close() }()

	out := make([]Channel, 0)
	for rows.Next() {
		var ch Channel
		var typ string
		var name sql.NullString
		if err := rows.Scan(&ch.ID, &name, &typ, &ch.Description, &ch.CreatedAt); err != nil {
			return nil, fmt.Errorf("channels: scan list: %w", err)
		}
		ch.Type = ChannelType(typ)
		if name.Valid {
			ch.Name = name.String
		}
		out = append(out, ch)
	}
	return out, rows.Err()
}

// DeleteChannel implements [ChannelStore.DeleteChannel].
func (s *sqliteStore) DeleteChannel(ctx context.Context, id string) error {
	res, err := s.db.ExecContext(ctx, `DELETE FROM channels WHERE id = ?`, id)
	if err != nil {
		return fmt.Errorf("channels: delete: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("channels: delete rowsaffected: %w", err)
	}
	if n == 0 {
		return fmt.Errorf("%w: %s", ErrChannelNotFound, id)
	}
	s.logger.Info("channels: channel deleted", zap.String("channel_id", id))
	return nil
}

// AddMember implements [ChannelStore.AddMember].
func (s *sqliteStore) AddMember(ctx context.Context, channelID, participantID string, policy RespondPolicy) error {
	if err := validateParticipantID(participantID); err != nil {
		return err
	}
	if !policy.Valid() {
		return fmt.Errorf("%w: %q", ErrInvalidRespondPolicy, policy)
	}
	// Idempotent re-add: keep the existing joined_at and respond_policy.
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
		 VALUES (?, ?, ?, ?)
		 ON CONFLICT(channel_id, participant_id) DO NOTHING`,
		channelID, participantID, string(policy), time.Now().UTC(),
	)
	if err != nil {
		if isForeignKeyViolation(err) {
			return fmt.Errorf("%w: %s", ErrChannelNotFound, channelID)
		}
		return fmt.Errorf("channels: add member: %w", err)
	}
	return nil
}

// GetMembers implements [ChannelStore.GetMembers].
func (s *sqliteStore) GetMembers(ctx context.Context, channelID string) ([]Member, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT participant_id, respond_policy, joined_at
		   FROM memberships
		  WHERE channel_id = ?
		  ORDER BY joined_at ASC, participant_id ASC`, channelID)
	if err != nil {
		return nil, fmt.Errorf("channels: get members: %w", err)
	}
	defer func() { _ = rows.Close() }()

	out := make([]Member, 0)
	for rows.Next() {
		var m Member
		var policy string
		if err := rows.Scan(&m.ParticipantID, &policy, &m.JoinedAt); err != nil {
			return nil, fmt.Errorf("channels: scan member: %w", err)
		}
		m.RespondPolicy = RespondPolicy(policy)
		out = append(out, m)
	}
	return out, rows.Err()
}

// GetMember implements [ChannelStore.GetMember].
func (s *sqliteStore) GetMember(ctx context.Context, channelID, participantID string) (Member, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT participant_id, respond_policy, joined_at
		   FROM memberships WHERE channel_id = ? AND participant_id = ?`,
		channelID, participantID)
	var m Member
	var policy string
	if err := row.Scan(&m.ParticipantID, &policy, &m.JoinedAt); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Member{}, fmt.Errorf("%w: channel=%s participant=%s",
				ErrNotMember, channelID, participantID)
		}
		return Member{}, fmt.Errorf("channels: get member: %w", err)
	}
	m.RespondPolicy = RespondPolicy(policy)
	return m, nil
}

// IsMember implements [ChannelStore.IsMember].
func (s *sqliteStore) IsMember(ctx context.Context, channelID, participantID string) (bool, error) {
	var exists int
	err := s.db.QueryRowContext(ctx,
		`SELECT 1 FROM memberships WHERE channel_id = ? AND participant_id = ?`,
		channelID, participantID).Scan(&exists)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("channels: is member: %w", err)
	}
	return true, nil
}

// GetOrCreateDM implements [ChannelStore.GetOrCreateDM].
func (s *sqliteStore) GetOrCreateDM(ctx context.Context, a, b string) (Channel, error) {
	id, err := CanonicalDMID(a, b)
	if err != nil {
		return Channel{}, err
	}

	s.dmMu.Lock()
	defer s.dmMu.Unlock()

	ch, err := s.GetChannel(ctx, id)
	if err == nil {
		return ch, nil
	}
	if !errors.Is(err, ErrChannelNotFound) {
		return Channel{}, err
	}

	// Lexicographically sort once to mirror CanonicalDMID's ordering for the
	// membership inserts.
	pa, pb := a, b
	if pa > pb {
		pa, pb = pb, pa
	}

	now := time.Now().UTC()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Channel{}, err
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.ExecContext(ctx,
		`INSERT INTO channels (id, name, channel_type, description, created_at)
		 VALUES (?, NULL, ?, '', ?)`,
		id, string(ChannelTypeDM), now); err != nil {
		return Channel{}, fmt.Errorf("channels: create dm: %w", err)
	}
	for _, p := range []string{pa, pb} {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
			 VALUES (?, ?, 'always', ?)`,
			id, p, now); err != nil {
			return Channel{}, fmt.Errorf("channels: add dm member %s: %w", p, err)
		}
	}
	if err := tx.Commit(); err != nil {
		return Channel{}, err
	}

	return Channel{
		ID:        id,
		Type:      ChannelTypeDM,
		CreatedAt: now,
	}, nil
}
