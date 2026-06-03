package server

import "time"

// submitWorkflowRunRequest is the JSON request body for POST /api/v1/workflows/run.
type submitWorkflowRunRequest struct {
	WorkflowID string            `json:"workflow_id"`
	Inputs     map[string]string `json:"inputs"`
}

// submitWorkflowRunResponse is the JSON response for POST /api/v1/workflows/run.
type submitWorkflowRunResponse struct {
	RunID      string `json:"run_id"`
	WorkflowID string `json:"workflow_id"`
	Status     string `json:"status"`
}

// workflowRunResponse is the JSON response for GET /api/v1/workflows/{id}/status
// and each element in the list response.
type workflowRunResponse struct {
	RunID      string         `json:"run_id"`
	WorkflowID string         `json:"workflow_id"`
	Status     string         `json:"status"`
	Error      string         `json:"error,omitempty"` // Non-empty when Status == "failed" (N-23)
	StartedAt  *time.Time     `json:"started_at"`      // *time.Time → null when zero (M-07)
	FinishedAt *time.Time     `json:"finished_at"`     // *time.Time → null when zero (M-07)
	Steps      map[string]any `json:"steps"`
}

// registerAgentRequest is the JSON request body for POST /api/v1/agents/register.
type registerAgentRequest struct {
	ID           string   `json:"id"`
	Name         string   `json:"name"`
	Role         string   `json:"role"` // RFC 0048 amendment §A — optional persona role; "" when unset
	Type         string   `json:"type"` // RFC 0048 amendment §A DTO — agent kind ("task"|"persona"|…); "" when unset
	Address      string   `json:"address"`
	Capabilities []string `json:"capabilities"`
}

// agentResponse is the JSON response for agent endpoints.
// registry.AgentInfo has no json tags and would produce PascalCase JSON if
// serialized directly — these snake_case tags match the workflow DTO convention (F-15).
type agentResponse struct {
	ID           string   `json:"id"`
	Name         string   `json:"name"`
	Role         string   `json:"role"` // RFC 0048 amendment §A — from registry.AgentInfo.Role; "" when unset
	Type         string   `json:"type"` // RFC 0048 amendment §A DTO — from registry.AgentInfo.Type; "" when unset
	Address      string   `json:"address"`
	Capabilities []string `json:"capabilities"`
	Status       string   `json:"status"`
}

// errorResponse is the standard JSON error envelope.
type errorResponse struct {
	Error string `json:"error"`
	Code  string `json:"code"`
}

// chatRequest is the JSON request body for POST /api/v1/agents/{id}/chat.
//
// `chat_session_id` (RFC 0016 chat-conversation token) was renamed from
// `session_id` in v0.3.1 to disambiguate from RFC 0031's operator-
// namespace `session_id`. As of RFC 0031 Phase 3 (PR 4) the `session_id`
// key is back — now carrying the operator session the rename reserved it
// for (see the `SessionID` field below), no longer the RFC 0016 chat token.
// See CHANGELOG `[0.3.1]` Upgrade Notes for the rename and the test
// `TestHandleChat_SessionIDIsOperatorNamespace`.
//
// `session_id` (distinct from `chat_session_id`) is the RFC 0031 Phase 3
// operator-namespace session override the v0.3.1 rename deliberately reserved
// this key for. When present it replaces the orchestrator's boot-default
// session for this conversation — both on the persisted inbound row and as the
// `persatrix-session` header the dispatch path emits to the persona (overriding
// the ISSUE-0082 auto-binding). Absent, the boot default / auto-binding stands.
type chatRequest struct {
	Message         string `json:"message"`
	UserID          string `json:"user_id"`
	ChatSessionID   string `json:"chat_session_id"`
	TimeoutSeconds  int32  `json:"timeout_seconds"`
	ParticipantType string `json:"participant_type"`
	SessionID       string `json:"session_id,omitempty"`
	// EpochID is the optional ISSUE-0085 PR 5 `--epoch` override. When present
	// it replaces the orchestrator's boot-resolved process epoch (PERSATRIX_EPOCH)
	// for this one conversation — emitted as the `persatrix-epoch` header the
	// dispatch path sends to the persona (the run/test-isolation axis). Distinct
	// from `session_id` (room-continuity); orthogonal scope axes. Absent, the
	// boot epoch stands. Not stamped on a persisted row (no channel-store epoch
	// write path; run-isolation is enforced persona-side via the rail).
	EpochID string `json:"epoch_id,omitempty"`
}

// createSessionRequest is the JSON body for POST /api/v1/sessions
// (RFC 0031 Phase 3, §E operator surface). `label` is the human-readable
// name the operator gives the session; it is required (the auto-mint path is
// the only route that creates label-less rows) and must not be the reserved
// `legacy` carve-out (OQ #2a — rejected server-side).
type createSessionRequest struct {
	Label string `json:"label"`
}

// sessionResponse is the JSON shape returned by the session registry
// endpoints. `created_at` is RFC3339 (matching channelResponse); `archived`
// is the one-way archive flag (RFC 0031 §B).
type sessionResponse struct {
	ID        string    `json:"id"`
	Label     string    `json:"label,omitempty"` // empty for auto-minted, not-yet-named rows
	CreatedAt time.Time `json:"created_at"`
	Archived  bool      `json:"archived"`
}

// listSessionsResponse is the envelope for GET /api/v1/sessions.
type listSessionsResponse struct {
	Sessions []sessionResponse `json:"sessions"`
}

// chatResponse is the JSON response for POST /api/v1/agents/{id}/chat.
type chatResponse struct {
	Reply            string `json:"reply"`
	ChatSessionID    string `json:"chat_session_id"`
	AgentID          string `json:"agent_id"`
	Timestamp        int64  `json:"timestamp"`
	AgentDisplayName string `json:"agent_display_name"`
	ReplyStatus      string `json:"reply_status"`
	// ChannelID is the resolved DM channel id (`dm:<agent>:<user>`,
	// GetOrCreateDM) the chat persisted to. Exposed so a client can open the
	// conversation as a channel without a second lookup — the web console's
	// direct-message create flow lands the operator in this channel on the
	// timeline (RFC 0048 channel-creation amendment §B, direct mode).
	ChannelID string `json:"channel_id"`
}
