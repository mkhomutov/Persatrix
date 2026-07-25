// sqlite_dm.go — sqliteStore direct-message (DM) resolution: LookupDM (read-only
// resolve) and GetOrCreateDM (idempotent create). Split from sqlite_query.go to
// keep that file under the 500-line review-friendly cap; the two DM methods are
// a cohesive unit and GetOrCreateDM is one of RFC 0035's interval-open hooks, so
// they sit together here. All methods share the `sqliteStore` receiver defined
// in sqlite.go.
package channels

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// LookupDM implements [ChannelStore.LookupDM]: a read-only resolve of the
// canonical DM between `a` and `b`. It derives the canonical id (which validates
// the pair and is the same access boundary GetOrCreateDM uses) and returns the
// existing channel, or [ErrChannelNotFound] when the DM has never been created.
// No mutation, no membership insert — the fresh-start case is a clean not-found,
// not a side-effecting create.
func (s *sqliteStore) LookupDM(ctx context.Context, a, b string) (Channel, error) {
	id, err := CanonicalDMID(a, b)
	if err != nil {
		return Channel{}, err
	}
	return s.GetChannel(ctx, id)
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

	// RFC 0031 Phase 1: DM rows created implicitly carry the legacy
	// carve-out. Operators can promote a DM into a named session via
	// Phase 3 CLI's `persatrix session use <id>` after the fact.
	//
	// RFC 0037 §B: stamp the operator's `dm_default_classification` knob
	// (normalized to a known §A level at store construction; `internal`
	// when unset). DMs open on demand with no config block, so creation is
	// the only stamping point; an existing DM is reclassified through the
	// same audited machinery as any channel (later RFC 0037 PR). Thread
	// replies need no stamp: no production path creates a `thread:` channel
	// row — replies are `messages` rows in the PARENT channel (`thread_id`
	// FK) and carry its classification by construction (pinned by
	// TestThreadReplies_NoThreadChannelRow_InheritByConstruction).
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO channels (id, name, channel_type, description, created_at, session_id, classification)
		 VALUES (?, NULL, ?, '', ?, ?, ?)`,
		id, string(ChannelTypeDM), now, DefaultSessionID,
		string(s.dmDefaultClassification)); err != nil {
		return Channel{}, fmt.Errorf("channels: create dm: %w", err)
	}
	for _, p := range []string{pa, pb} {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO memberships (channel_id, participant_id, respond_policy, joined_at)
			 VALUES (?, ?, 'always', ?)`,
			id, p, now); err != nil {
			return Channel{}, fmt.Errorf("channels: add dm member %s: %w", p, err)
		}
		// RFC 0035 §C: open an interval for each DM participant in the same tx,
		// joined_at = the DM's creation time. The DM channel is created fresh
		// above, so each membership insert is a genuine new row and opens exactly
		// one interval. DM membership is never removed in normal operation, so
		// these stay open for the channel's life.
		if err := openMembershipInterval(ctx, tx, id, p, now); err != nil {
			return Channel{}, fmt.Errorf("channels: open dm interval %s: %w", p, err)
		}
	}
	if err := tx.Commit(); err != nil {
		return Channel{}, err
	}
	s.recordSessionWrite(ctx, DefaultSessionID)

	return Channel{
		ID:        id,
		Type:      ChannelTypeDM,
		CreatedAt: now,
		SessionID: DefaultSessionID,
	}, nil
}
