package server

// channel_mention_lift.go — RFC 0011 display-name-mention-lifting amendment,
// the publish-seam wiring (ML1/ML5). Split out of channel_handlers.go (which
// sits at the 500-line review cap) so the publish handler stays under it,
// mirroring how channel_governance.go carved out the runtime-governance glue.
//
// The pure resolver lives in internal/channels ([channels.LiftDisplayNameMentions]);
// this is the call site that gives it what it needs — the channel's members
// joined with the agent registry's display names — and folds its result back
// into the publish payload before persist and fanout, so the prose `@`-mentions
// personas actually write ("@Iron Fox") become the canonical ids the wire
// already carries ("iron-fox"). No proto/wire/schema change: only what the
// existing `mentions` array *contains*, produced one seam earlier (ML1).

import (
	"context"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// liftContentMentions unions the in-text `@`-mentions resolved from `content`
// into `structured` (the producer's explicit `mentions` array), returning the
// combined list to persist and fan out. Structured entries keep their order and
// come first (the producer's explicit intent outranks prose, ML1); lifted ids
// follow in content order, de-duplicated against the union; the total is capped
// at [channelMaxMentionsPerPublish] with any overflow dropped and logged (ML5).
//
// Fail-open throughout (the amendment's degraded branch is exactly today's
// behaviour — ISSUE-0096 open): a missing router/store member lookup or a
// registry miss skips the lift and returns `structured` untouched, so a
// resolution hiccup can never block a publish. The registry supplies display
// names only — even with no registry an in-text *id* ("@iron-fox") still lifts,
// since candidate ids come from the membership rows.
func (s *Server) liftContentMentions(ctx context.Context, channelID, senderID, content string, structured []string) []string {
	if s.channelStore == nil || content == "" {
		return structured
	}
	members, err := s.channelStore.GetMembers(ctx, channelID)
	if err != nil || len(members) == 0 {
		// Channel not found / lookup failed: the publish itself will surface
		// the error downstream. Lifting is best-effort — never the thing that
		// fails a publish.
		return structured
	}

	// id → display name, from the registry directory (the same source the
	// persona roster join reads). A registry miss leaves the name empty, which
	// the resolver treats as "id-only" for that member.
	names := map[string]string{}
	if s.registry != nil {
		if agents, lErr := s.registry.List(ctx); lErr == nil {
			for _, a := range agents {
				names[a.ID] = a.Name
			}
		}
	}
	candidates := make([]channels.MentionCandidate, len(members))
	for i, m := range members {
		candidates[i] = channels.MentionCandidate{
			ID:          m.ParticipantID,
			DisplayName: names[m.ParticipantID],
		}
	}

	lifted := channels.LiftDisplayNameMentions(content, candidates, senderID)

	// ML5: a folded display name shared by two members made an in-text mention
	// unresolvable — surface the config smell loudly, but only because it was
	// actually named here (the resolver reports content-triggered collisions,
	// so a standing roster collision never spams a quiet channel).
	if len(lifted.AmbiguousNames) > 0 {
		s.logger.Warn("channels: display-name mention is ambiguous; lifted nobody (two members share the name)",
			zap.String("channel_id", channelID),
			zap.Strings("ambiguous_names", lifted.AmbiguousNames))
	}
	if len(lifted.IDs) == 0 {
		return structured
	}

	// Union: structured prefix verbatim, then lifted ids not already present,
	// capped at the publish limit. The structured array already passed the
	// hard >cap 400 in the handler, so it fits; lifted overflow is dropped
	// (logged), never a 400 — the prose is a best-effort enrichment.
	seen := make(map[string]struct{}, len(structured)+len(lifted.IDs))
	for _, id := range structured {
		seen[id] = struct{}{}
	}
	out := append([]string(nil), structured...)
	var added, dropped []string
	for _, id := range lifted.IDs {
		if _, dup := seen[id]; dup {
			continue
		}
		seen[id] = struct{}{}
		if len(out) >= channelMaxMentionsPerPublish {
			dropped = append(dropped, id)
			continue
		}
		out = append(out, id)
		added = append(added, id)
	}
	if len(added) > 0 {
		s.logger.Debug("channels: lifted display-name mentions from content",
			zap.String("channel_id", channelID),
			zap.Strings("lifted", added))
	}
	if len(dropped) > 0 {
		s.logger.Warn("channels: lifted mentions dropped at the per-publish cap",
			zap.String("channel_id", channelID),
			zap.Int("cap", channelMaxMentionsPerPublish),
			zap.Strings("dropped", dropped))
	}
	return out
}
