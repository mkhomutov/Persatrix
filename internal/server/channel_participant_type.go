package server

// channel_participant_type.go — ISSUE-0119: resolve a channel publish
// sender's peer type ("user" | "agent") server-side, so the RFC 0011
// participant-type wire field is populated for EVERY publisher rather than
// only for the REST chat handler.
//
// Why this exists. Person identity is stored on the relationship row keyed
// `(other_participant_id, other_participant_type)`, and the agent-side read
// resolves that type from the wire, defaulting to "agent" when absent
// (`agents/sender_type.py`). Until this file, only `chat_handler.go` stamped
// the key: a human publishing into a group channel arrived typeless, the
// persona queried the agent-typed row, and the cross-room identity it had
// learned in the DM (RFC 0031 F-7) was invisible — it greeted a stranger in
// the group and then accumulated a SECOND, permanently split person record
// there. ISSUE-0068 closed exactly this for the chat surface and left the
// channel path on the broken default.
//
// Why the registry is the discriminator. Humans are never registered — the
// chat handler already relies on this ("they are not in the agent registry",
// ISSUE-0034). Agents publish through the same REST endpoint as humans
// (`agents/channel_publisher.py`), so one lookup at the shared door types
// both sides correctly, and the two production publish entry points
// (chat + channel) are then both stamped at the source. A future bridge
// posting on behalf of an external agent is the case the registry cannot
// see; that is what the explicit caller claim stays available for.

import (
	"context"
	"errors"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// resolveSenderParticipantType resolves the peer type for a channel publish
// sender from the agent registry: a registered id is an "agent", an
// unregistered one is a "user".
//
// Returns "" — meaning "unresolved, stamp nothing" — in the two cases where
// a guess would be worse than the pre-ISSUE-0119 silence:
//
//   - no registry is wired (minimal deployments and unit fixtures), and
//   - the registry read fails for a reason other than a clean miss.
//
// The second is the load-bearing one: a backend failure must not be read as
// "not registered", because that would type a genuine agent peer as a human
// and write its interactions onto a user-typed relationship row — the same
// class of split record this change exists to prevent, merely inverted. An
// unresolved publish rides the wire exactly as it did before, so the failure
// mode is the old behaviour rather than a new corruption. It is logged at
// Warn because a registry that cannot answer is an operator-visible fault.
func (s *Server) resolveSenderParticipantType(ctx context.Context, senderID string) string {
	if s.registry == nil {
		return ""
	}
	if _, err := s.registry.Get(ctx, senderID); err != nil {
		if errors.Is(err, registry.ErrAgentNotFound) {
			return channels.ParticipantTypeUser
		}
		s.logger.Warn("channels: participant-type resolution failed; publishing untyped (peer will resolve as agent)",
			zap.String("sender_id", senderID),
			zap.Error(err),
		)
		return ""
	}
	return channels.ParticipantTypeAgent
}
