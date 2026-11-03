package server

// Every publish is bound to its caller (RFC 0009 Phases 3–4 plan PR 8, design
// point G-7).

import (
	"net/http"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/security"
)

// publishSenderRefused reports whether, under auth.mode: enabled, a publish
// names a sender other than its caller, and if so answers 403 and writes an
// authz.denied audit line. A publish may name the agent its X-Agent-Token
// proves, or the signed-in account's own participant, typed `user` or not
// typed at all. So only an agent speaks as that agent, no account passes for
// an agent, and no account can post a manager's directive under the manager's
// name. A caller that proves neither may name nobody. Under disabled nothing
// is proved, so any sender stands, as before.
func (s *Server) publishSenderRefused(w http.ResponseWriter, r *http.Request, sender string,
	metadata map[string]any,
) bool {
	if !s.authEnforced() {
		return false
	}
	ident := identityFrom(r.Context())
	switch {
	case ident.AgentID != "":
		if sender == ident.AgentID {
			return false
		}
	case ident.Authenticated:
		typed := channels.ReadParticipantType(metadata)
		if sender == ident.ParticipantID && (typed == "" || typed == channels.ParticipantTypeUser) {
			return false
		}
	}
	detail := map[string]any{"reason": "sender_not_caller", "source": s.requestSource(r)}
	// The sender is the caller's to fill, so only one the store would take
	// reaches the line.
	if channels.ValidateParticipantID(sender) == nil {
		detail["sender_id"] = sender
	}
	if ident.Authenticated {
		detail["participant_id"] = ident.ParticipantID
		detail["username"] = ident.Username
	}
	s.emitAudit(r.Context(), security.AuditEvent{
		EventType: security.AuditAuthzDenied,
		AgentID:   ident.AgentID,
		Action:    "publish",
		Resource:  auditBounded(r.URL.Path),
		Outcome:   "refused",
		Detail:    detail,
	})
	writeError(w, "FORBIDDEN", "a publish may name only its caller as sender", http.StatusForbidden)
	return true
}
