// sqlite_classification.go — sqliteStore classification write path (RFC 0037
// §B, v0.3.12 PR 2). Own file rather than sqlite.go / sqlite_create.go: both
// sit near the 500-line review cap, and the classification write is the seed
// of the §Security audited-reclassification surface, so it grows here (the
// sqlite_classification_migration.go precedent from PR 1). Shares the
// `sqliteStore` receiver defined in sqlite.go.
package channels

import (
	"context"
	"fmt"

	"go.uber.org/zap"
)

// SetChannelClassification implements [ChannelStore.SetChannelClassification].
//
// Strict on the level (no [NormalizeForStamp] rewrite): the two callers — the
// reconcile adoption step and the future audited reclassification — both hold
// an explicit, already-validated level; a silent rewrite here would mask a
// caller bug as a quiet `internal` stamp. §A rule (a) covers creation
// defaults, not updates.
func (s *sqliteStore) SetChannelClassification(ctx context.Context, channelID string, level Classification) error {
	if !level.Valid() {
		return fmt.Errorf("%w: %q", ErrInvalidClassification, level)
	}
	res, err := s.db.ExecContext(ctx,
		`UPDATE channels SET classification = ? WHERE id = ?`,
		string(level), channelID)
	if err != nil {
		return fmt.Errorf("channels: set classification: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("channels: set classification rowsaffected: %w", err)
	}
	if n == 0 {
		return fmt.Errorf("%w: %s", ErrChannelNotFound, channelID)
	}
	// Info-level like the create paths' lifecycle logs: classification
	// changes are rare, operator-driven, and §Security-relevant — the log
	// line is the audit substrate until the RFC 0009 event lands (Phase 3).
	s.logger.Info("channels: channel classification set",
		zap.String("channel_id", channelID),
		zap.String("classification", string(level)))
	return nil
}
