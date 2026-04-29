package channels

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"sync"
	"time"

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
type SQLiteOptions struct {
	MaxChannels           int
	MaxMessagesPerChannel int
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

	dsn := buildDSN(path)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("channels: open %s: %w", path, err)
	}
	// modernc.org/sqlite is connection-safe but a single connection avoids
	// surprising lock-stepping with the file-backed file. WAL mode is set per
	// connection via the DSN above so it survives reconnects.
	db.SetMaxOpenConns(1)

	if err := applySchema(db); err != nil {
		_ = db.Close()
		return nil, err
	}

	return &sqliteStore{
		db:                    db,
		maxChannels:           opts.MaxChannels,
		maxMessagesPerChannel: opts.MaxMessagesPerChannel,
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

const schemaSQL = `
CREATE TABLE IF NOT EXISTS channels (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    channel_type TEXT NOT NULL CHECK (channel_type IN ('group', 'dm', 'thread')),
    description  TEXT NOT NULL DEFAULT '',
    created_at   DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    channel_id     TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL,
    respond_policy TEXT NOT NULL DEFAULT 'when_mentioned'
        CHECK (respond_policy IN ('when_mentioned', 'always', 'never')),
    joined_at      DATETIME NOT NULL,
    PRIMARY KEY (channel_id, participant_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    sender_id  TEXT NOT NULL,
    content    TEXT NOT NULL,
    timestamp  DATETIME NOT NULL,
    thread_id  TEXT REFERENCES messages(id) ON DELETE CASCADE,
    mentions   TEXT NOT NULL DEFAULT '[]',
    metadata   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages(channel_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_thread     ON messages(thread_id) WHERE thread_id IS NOT NULL;
`

func applySchema(db *sql.DB) error {
	if _, err := db.Exec(schemaSQL); err != nil {
		return fmt.Errorf("channels: apply schema: %w", err)
	}
	// Defensive PRAGMA — DSN sets it, but a future caller wiring a
	// pre-existing *sql.DB through a yet-to-be-added constructor should
	// still see foreign keys enforced.
	if _, err := db.Exec(`PRAGMA foreign_keys = ON;`); err != nil {
		return fmt.Errorf("channels: enable foreign_keys: %w", err)
	}
	return nil
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

	// `name` is UNIQUE NOT NULL — insert a deterministic placeholder for non-
	// group channels (DM canonical id, thread parent message id) so the
	// constraint still distinguishes rows but callers see Name="" via the
	// scan path below.
	storedName := ch.Name
	if storedName == "" {
		storedName = ch.ID
	}

	_, err = tx.ExecContext(ctx,
		`INSERT INTO channels (id, name, channel_type, description, created_at)
		 VALUES (?, ?, ?, ?, ?)`,
		ch.ID, storedName, string(ch.Type), ch.Description, ch.CreatedAt,
	)
	if err != nil {
		if isUniqueViolation(err) {
			return fmt.Errorf("%w: %s", ErrChannelExists, ch.ID)
		}
		return fmt.Errorf("channels: insert channel: %w", err)
	}

	return tx.Commit()
}

// GetChannel implements [ChannelStore.GetChannel].
func (s *sqliteStore) GetChannel(ctx context.Context, id string) (Channel, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT id, name, channel_type, description, created_at
		   FROM channels WHERE id = ?`, id)
	var ch Channel
	var typ string
	var name string
	if err := row.Scan(&ch.ID, &name, &typ, &ch.Description, &ch.CreatedAt); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return Channel{}, fmt.Errorf("%w: %s", ErrChannelNotFound, id)
		}
		return Channel{}, fmt.Errorf("channels: scan channel: %w", err)
	}
	ch.Type = ChannelType(typ)
	if name != ch.ID { // group channels store the friendly name
		ch.Name = name
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
		var name string
		if err := rows.Scan(&ch.ID, &name, &typ, &ch.Description, &ch.CreatedAt); err != nil {
			return nil, fmt.Errorf("channels: scan list: %w", err)
		}
		ch.Type = ChannelType(typ)
		if name != ch.ID {
			ch.Name = name
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
		 VALUES (?, ?, ?, '', ?)`,
		id, id, string(ChannelTypeDM), now); err != nil {
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
