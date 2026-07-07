package server

import (
	"errors"
	"net/http"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// writeChannelError maps a channels package error to the standard JSON
// envelope and HTTP status. Centralised so every handler reports the
// same code/message for the same store sentinel.
func (s *Server) writeChannelError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, channels.ErrChannelNotFound):
		writeError(w, "NOT_FOUND", "channel not found", http.StatusNotFound)
	case errors.Is(err, channels.ErrMessageNotFound):
		writeError(w, "NOT_FOUND", "message not found", http.StatusNotFound)
	case errors.Is(err, channels.ErrChannelExists):
		writeError(w, "CONFLICT", "channel already exists", http.StatusConflict)
	case errors.Is(err, channels.ErrChannelCapExceeded):
		writeError(w, "CONFLICT", "max_channels cap exceeded", http.StatusConflict)
	case errors.Is(err, channels.ErrConfigRevisionConflict): // RFC 0050 PR 4 optimistic concurrency
		writeError(w, "CONFLICT", err.Error(), http.StatusConflict)
	case errors.Is(err, channels.ErrChannelNotArmed), // RFC 0052 PR 3 — convene against an unarmed channel
		errors.Is(err, channels.ErrChannelAlreadyConvening), // RFC 0052 PR 3 — convene a channel with a live interaction
		errors.Is(err, channels.ErrAutonomousNoAudience),    // RFC 0052 PR 3 — convene a roster with no open-floor responder
		errors.Is(err, channels.ErrAutonomousNoTopic):       // RFC 0052 PR 3 — convene a channel with no topic/agenda/goal
		writeError(w, "CONFLICT", err.Error(), http.StatusConflict)
	case errors.Is(err, channels.ErrInvalidSalienceMaxChannelMembers), // RFC 0050 PR 4 config validation
		errors.Is(err, channels.ErrInvalidInteractionBudgetTokens),
		errors.Is(err, channels.ErrInvalidMaxRepliesPerParticipant),
		errors.Is(err, channels.ErrInvalidEndVoteThreshold),
		errors.Is(err, channels.ErrInvalidEndVoteWindow),
		errors.Is(err, channels.ErrInvalidInteractionIdleTimeout),
		errors.Is(err, channels.ErrInvalidEscalationChair),
		errors.Is(err, channels.ErrInvalidReasoningMode), // RFC 0051 PR 4 reasoning validation
		errors.Is(err, channels.ErrInvalidReasoningModel),
		errors.Is(err, channels.ErrInvalidReasoningDepth),
		errors.Is(err, channels.ErrInvalidReasoningRevise),
		errors.Is(err, channels.ErrAutonomousCapRequired),   // RFC 0052 PR 1 autonomous validation
		errors.Is(err, channels.ErrAutonomousChairRequired), // RFC 0052 PR 4 — armed channel needs a synthesizing chair
		errors.Is(err, channels.ErrInvalidAutonomousConvener),
		errors.Is(err, channels.ErrInvalidAutonomousMaxRounds),
		errors.Is(err, channels.ErrInvalidAutonomousAgenda),
		errors.Is(err, channels.ErrAutonomousNotGroup):
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
	case errors.Is(err, channels.ErrNotMember):
		writeError(w, "FORBIDDEN", "sender is not a member of the channel", http.StatusForbidden)
	case errors.Is(err, channels.ErrMemberNotFound): // RFC 0050 member-config PATCH target missing
		writeError(w, "NOT_FOUND", "member not found", http.StatusNotFound)
	case errors.Is(err, channels.ErrInvalidChannelType),
		errors.Is(err, channels.ErrInvalidParticipantID),
		errors.Is(err, channels.ErrInvalidRespondPolicy),
		errors.Is(err, channels.ErrInvalidThreshold), // RFC 0050 member-config threshold validation
		errors.Is(err, channels.ErrThresholdNotApplicable):
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
	case errors.Is(err, channels.ErrMessageContentTooLarge): // ISSUE-0050
		writeError(w, "PAYLOAD_TOO_LARGE", err.Error(), http.StatusRequestEntityTooLarge)
	case errors.Is(err, channels.ErrParticipantBudgetExhausted): // RFC 0030 Layer 2
		writeError(w, "TOO_MANY_REQUESTS", err.Error(), http.StatusTooManyRequests)
	case errors.Is(err, registry.ErrAgentNotFound), // PR #718 review — the convene dispatch's
		errors.Is(err, channels.ErrAgentNotReady),   // delivery-miss returns: a restarting /
		errors.Is(err, channels.ErrDeliveryRefused): // queue-full convener is a routine,
		// retryable condition (the dispatcher documents all three as
		// best-effort misses), not a store failure — the default arm's 500
		// "channel store error" + Error-level "unexpected error" log misled
		// operators on every convene raced against an agent restart.
		writeError(w, "UNAVAILABLE", err.Error(), http.StatusServiceUnavailable)
	default:
		s.logger.Error("channels: unexpected error", zap.Error(err))
		writeError(w, "INTERNAL", "channel store error", http.StatusInternalServerError)
	}
}
