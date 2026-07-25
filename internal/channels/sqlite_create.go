// sqlite_create.go — sqliteStore channel-creation writes: CreateChannel and
// CreateChannelWithMembers. Split from sqlite.go (constructor + shared store
// plumbing) when RFC 0037 PR 2's classification column pushed that file past
// the 500-line review cap (the sqlite_query.go / sqlite_dm.go extraction
// pattern, ISSUE-0008). Verbatim move plus the classification additions; both
// methods share the `sqliteStore` receiver defined in sqlite.go.
package channels

import (
	"context"
	"errors"
	"fmt"
	"time"

	"go.uber.org/zap"
)

// CreateChannel implements [ChannelStore.CreateChannel].
func (s *sqliteStore) CreateChannel(ctx context.Context, ch Channel) error {
	if !ch.Type.Valid() {
		return fmt.Errorf("%w: %q", ErrInvalidChannelType, ch.Type)
	}
	if ch.ID == "" {
		return errors.New("channels: channel id is required")
	}
	// PR #245 re-review (Low/Med): the name-required and name-pattern
	// errors used to be plain (un-wrapped) errors. writeChannelError in
	// internal/server falls through to 500 INTERNAL for any error that
	// does not match a known sentinel, so an operator typing
	// `Name: "Sprint 1"` got a 500 instead of the correct 400. Wrapping
	// with ErrInvalidChannelType (the closest existing 400-class
	// sentinel — these ARE invalid channel attributes) reclassifies the
	// status code without changing the human-readable message.
	if ch.Type == ChannelTypeGroup && ch.Name == "" {
		return fmt.Errorf("%w: group channel requires a name", ErrInvalidChannelType)
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
		return fmt.Errorf("%w: group channel name %q does not match %s",
			ErrInvalidChannelType, ch.Name, channelNamePattern.String())
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
	// RFC 0031 Phase 1: rewrite the empty default at the store boundary
	// so session-unaware callers persist a queryable row.
	if ch.SessionID == "" {
		ch.SessionID = DefaultSessionID
	}
	// RFC 0037 §A rule (a) at the store boundary (v0.3.12 PR 2): an absent
	// or out-of-lattice classification stamps `internal`, never `public` —
	// the SessionID rewrite precedent above. Classification-aware callers
	// (the config reconcile, threading the PR 1 declaration) pass a
	// validated level through verbatim; classification-unaware callers (the
	// REST create handler) keep the pre-RFC-0037 behaviour byte-for-byte.
	ch.Classification = NormalizeForStamp(ch.Classification)

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
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id, classification)
		 VALUES (?, ?, ?, ?, ?, ?, ?)`,
		ch.ID, nameArg, string(ch.Type), ch.Description, ch.CreatedAt, ch.SessionID,
		string(ch.Classification),
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
		zap.String("channel_type", string(ch.Type)),
		zap.String("session_id", ch.SessionID))
	s.recordSessionWrite(ctx, ch.SessionID)
	return nil
}

// CreateChannelWithMembers implements [ChannelStore.CreateChannelWithMembers].
//
// PR #245 review (High): the REST `POST /api/v1/channels` handler used
// to call CreateChannel followed by an N-call AddMember loop, all
// outside any transaction. A failure mid-loop left the channel row
// committed but with only a prefix of the requested membership; the
// client's natural retry then hit ErrChannelExists → 409 and the
// remaining members were never added. This helper makes the bundle
// atomic at the store boundary so handlers no longer need to compose a
// rollback path of their own.
//
// Member validation runs inside the transaction so the same
// `ErrInvalidParticipantID` / `ErrInvalidRespondPolicy` sentinels surface
// as before — only the persistence side becomes all-or-nothing.
func (s *sqliteStore) CreateChannelWithMembers(ctx context.Context, ch Channel, members []Member) error {
	// Pre-flight validation duplicates the checks in [CreateChannel] /
	// [AddMember] so we can fail fast before touching the database. Any
	// future change to those rules MUST be mirrored here — the unit test
	// TestSQLiteStore_CreateChannelWithMembers_AtomicOnPartialFailure
	// pins the rollback contract for one concrete invalid-member case.
	if !ch.Type.Valid() {
		return fmt.Errorf("%w: %q", ErrInvalidChannelType, ch.Type)
	}
	if ch.ID == "" {
		return errors.New("channels: channel id is required")
	}
	// PR #245 re-review (Low/Med): mirror the wrapping fix from
	// CreateChannel above so REST callers see 400 BAD_REQUEST instead of
	// 500 INTERNAL when they submit an invalid `name`.
	if ch.Type == ChannelTypeGroup && ch.Name == "" {
		return fmt.Errorf("%w: group channel requires a name", ErrInvalidChannelType)
	}
	if ch.Type == ChannelTypeGroup && !channelNamePattern.MatchString(ch.Name) {
		return fmt.Errorf("%w: group channel name %q does not match %s",
			ErrInvalidChannelType, ch.Name, channelNamePattern.String())
	}
	if ch.Type == ChannelTypeGroup && ch.ID != "group:"+ch.Name {
		return fmt.Errorf("%w: group channel id %q must be \"group:\"+name (name=%q)",
			ErrInvalidChannelType, ch.ID, ch.Name)
	}
	if ch.CreatedAt.IsZero() {
		ch.CreatedAt = time.Now().UTC()
	}
	if ch.SessionID == "" {
		ch.SessionID = DefaultSessionID
	}
	// RFC 0037 §A rule (a): mirror the CreateChannel boundary rewrite above.
	ch.Classification = NormalizeForStamp(ch.Classification)

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
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id, classification)
		 VALUES (?, ?, ?, ?, ?, ?, ?)`,
		ch.ID, nameArg, string(ch.Type), ch.Description, ch.CreatedAt, ch.SessionID,
		string(ch.Classification),
	); err != nil {
		if isUniqueViolation(err) {
			return fmt.Errorf("%w: %s", ErrChannelExists, ch.ID)
		}
		return fmt.Errorf("channels: insert channel: %w", err)
	}

	now := time.Now().UTC()
	for _, m := range members {
		if err := ValidateParticipantID(m.ParticipantID); err != nil {
			return err
		}
		policy := m.RespondPolicy
		if policy == "" {
			policy = RespondWhenMentioned
		}
		// Normalize the disposition vocabulary to the legacy triple before
		// validating/persisting (see [canonicalRespondPolicy]). This is the
		// REST create path; the membership CHECK constraint only allows the
		// legacy values, so an un-normalized disposition would otherwise 500
		// instead of working.
		policy, err := canonicalRespondPolicy(policy)
		if err != nil {
			return err
		}
		joinedAt := m.JoinedAt
		if joinedAt.IsZero() {
			joinedAt = now
		}
		// RFC 0030 Tier B (v0.3.8): persist the per-member salience-bid signals
		// verbatim. Unlike `respond_policy` (normalized here from the
		// disposition), `SalienceGated`/`Threshold` are resolved by the caller —
		// the config loader off the declared disposition ([ResolveSalienceSignal]
		// in [MemberConfig.UnmarshalYAML]) or the REST create handler
		// ([ResolveMemberPolicy]) — because the config reconcile path passes an
		// already-normalized `always` policy, so deriving them here would lose
		// a participant's bid-ness.
		res, err := tx.ExecContext(ctx,
			`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at, threshold, salience_gated)
			 VALUES (?, ?, ?, ?, ?, ?)
			 ON CONFLICT(channel_id, participant_id) DO NOTHING`,
			ch.ID, m.ParticipantID, string(policy), joinedAt, m.Threshold, boolToInt(m.SalienceGated),
		)
		if err != nil {
			// FK violations are unexpected here — we just inserted the
			// parent row in the same transaction — but classify them the
			// same way [AddMember] does for symmetry.
			if isForeignKeyViolation(err) {
				return fmt.Errorf("%w: %s", ErrChannelNotFound, ch.ID)
			}
			return fmt.Errorf("channels: add member %s: %w", m.ParticipantID, err)
		}
		// RFC 0035 §C (fourth hook): seed an OPEN interval for each genuinely
		// inserted member in the same tx, so a channel created with initial
		// members — the REST atomic-create and config-reconcile path — feeds the
		// ledger exactly like AddMember would. joinedAt is the SAME instant
		// written to memberships.joined_at. RowsAffected gates a participant
		// repeated in the input slice (the ON CONFLICT no-op), mirroring AddMember.
		n, err := res.RowsAffected()
		if err != nil {
			return fmt.Errorf("channels: add member %s rowsaffected: %w", m.ParticipantID, err)
		}
		if n == 1 {
			if err := openMembershipInterval(ctx, tx, ch.ID, m.ParticipantID, joinedAt); err != nil {
				return fmt.Errorf("channels: open interval %s: %w", m.ParticipantID, err)
			}
		}
	}

	if err := tx.Commit(); err != nil {
		return err
	}
	s.logger.Info("channels: channel created with members",
		zap.String("channel_id", ch.ID),
		zap.String("channel_type", string(ch.Type)),
		zap.String("session_id", ch.SessionID),
		zap.Int("member_count", len(members)))
	s.recordSessionWrite(ctx, ch.SessionID)
	return nil
}
