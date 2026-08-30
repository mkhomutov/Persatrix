// channel_response_builders.go — the store-model → REST wire-shape
// converters for channels and messages. Split from channel_handlers.go
// (verbatim move plus the classification field) when RFC 0037 PR 2's
// classification threading pushed that file past the 500-line review cap
// (the channel_errors.go / channel_query_params.go carve-out pattern).
package server

import (
	"github.com/mkhomutov/persatrix/internal/channels"
)

// channelToResponse converts a [channels.Channel] (and an optional
// member slice) to the wire shape.
//
// `ch.SessionID` is intentionally not surfaced. Phase 1 of RFC 0031
// (PR #335) ships no operator-visible session surface — the Phase 3 CLI
// (`persatrix session list / use / archive`) owns that contract. Adding
// `session_id` to this struct would bake an unversioned wire field that
// a future operator-facing API has to either rename or replicate. Leave
// it off until Phase 3 lands.
//
// `ch.Classification` IS surfaced (RFC 0037 §B, v0.3.12 PR 2) — unlike
// session_id it already has a versioned operator contract (the §A lattice
// enum pinned by `schemas/channel.schema.json`), the catch-up replay reads
// it off the channel list to stamp replayed events, and the RFC 0037
// operator opt-in path (PR 8 docs) reads it back to verify a
// reclassification took.
func channelToResponse(ch channels.Channel, members []channels.Member) channelResponse {
	out := channelResponse{
		ID:             ch.ID,
		Name:           ch.Name,
		Type:           string(ch.Type),
		Description:    ch.Description,
		CreatedAt:      ch.CreatedAt,
		Classification: string(ch.Classification),
	}
	if members != nil {
		out.Members = make([]memberResponse, 0, len(members))
		for _, m := range members {
			out.Members = append(out.Members, memberResponse{
				ID:            m.ParticipantID,
				RespondPolicy: string(m.RespondPolicy),
				JoinedAt:      m.JoinedAt,
				SalienceGated: m.SalienceGated,
				Threshold:     m.Threshold,
			})
		}
	}
	return out
}

func messageToResponse(m channels.ChannelMessage) channelMessageResponse {
	out := channelMessageResponse{
		ID:          m.ID,
		ChannelID:   m.ChannelID,
		SenderID:    m.SenderID,
		Content:     m.Content,
		Timestamp:   m.Timestamp,
		ThreadID:    m.ThreadID,
		Mentions:    m.Mentions,
		Metadata:    m.Metadata,
		PrincipalID: m.PrincipalID,
	}
	if out.Mentions == nil {
		out.Mentions = []string{}
	}
	return out
}

func messagesToResponse(in []channels.ChannelMessage) []channelMessageResponse {
	out := make([]channelMessageResponse, 0, len(in))
	for _, m := range in {
		out = append(out, messageToResponse(m))
	}
	return out
}
