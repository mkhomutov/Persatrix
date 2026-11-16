package channels

import (
	"context"
	"database/sql"
	"fmt"
	"sort"

	"go.uber.org/zap"
)

// legacy_participant_ids.go — members an earlier orchestrator stored under
// the looser participant id rule (ISSUE-0209).
//
// Such a member keeps loading: its channels, its DMs and their histories are
// listed and read as before. What stops is every new write that carries the
// id, and what never worked is a persona answering it. Nothing in a running
// deployment says so until someone posts, so the store says it once when it
// opens.

// maxLegacyIDsNamed bounds how many ids one warning names, so a store with
// very many of them still logs one line a log pipeline will take whole. The
// warning carries the full count beside the names.
const maxLegacyIDsNamed = 500

// legacyMemberIDsWarning is the line a store logs, once, when it opens with
// members whose id the participant id rule refuses.
const legacyMemberIDsWarning = "channels: stored members have an id the participant id rule refuses; " +
	"they stay listed and readable, but they cannot post, be mentioned or be added again, " +
	"and no persona answers them. To rename one, remove the member and add it under an id that fits the rule"

// LegacyMember is one participant whose stored id the rule refuses, and how
// many channels it is a member of.
type LegacyMember struct {
	ParticipantID string
	Channels      int
}

// legacyMembers lists the participants with at least one membership whose id
// does not fit the participant id rule, ordered by id.
func legacyMembers(ctx context.Context, db *sql.DB) ([]LegacyMember, error) {
	rows, err := db.QueryContext(ctx,
		`SELECT participant_id, COUNT(*) FROM memberships GROUP BY participant_id`)
	if err != nil {
		return nil, fmt.Errorf("channels: list member ids: %w", err)
	}
	defer rows.Close() //nolint:errcheck
	var out []LegacyMember
	for rows.Next() {
		var m LegacyMember
		if err := rows.Scan(&m.ParticipantID, &m.Channels); err != nil {
			return nil, fmt.Errorf("channels: list member ids: %w", err)
		}
		if participantIDPattern.MatchString(m.ParticipantID) {
			continue
		}
		out = append(out, m)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("channels: list member ids: %w", err)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ParticipantID < out[j].ParticipantID })
	return out, nil
}

// warnLegacyMembers logs [legacyMemberIDsWarning] when the store holds
// members whose id the rule refuses, naming each with its number of
// channels. It logs nothing for a store with none, and a failed read is
// itself only a warning: the store opens either way.
func warnLegacyMembers(ctx context.Context, db *sql.DB, logger *zap.Logger) {
	members, err := legacyMembers(ctx, db)
	if err != nil {
		logger.Warn("channels: could not check stored member ids against the participant id rule", zap.Error(err))
		return
	}
	if len(members) == 0 {
		return
	}
	named := members
	if len(named) > maxLegacyIDsNamed {
		named = named[:maxLegacyIDsNamed]
	}
	names := make([]string, len(named))
	for i, m := range named {
		names[i] = fmt.Sprintf("%s (%d)", m.ParticipantID, m.Channels)
	}
	logger.Warn(legacyMemberIDsWarning,
		zap.Int("count", len(members)),
		zap.Strings("members", names),
		zap.Int("not_named", len(members)-len(named)),
		zap.String("rule", participantIDPattern.String()),
	)
}
