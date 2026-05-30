package server

import "time"

// createChannelRequest is the JSON body for POST /api/v1/channels (RFC 0011 §C).
//
// Only group channels are creatable via this endpoint. DMs are opened
// implicitly via the publish path against a `dm:a:b` id; threads anchor
// to a parent message id and are created as a side effect of replying.
type createChannelRequest struct {
	Name        string                 `json:"name"`
	Description string                 `json:"description"`
	Members     []channelMemberRequest `json:"members"`
}

// channelMemberRequest mirrors the YAML `{id, respond}` shape so the REST
// surface and the config loader speak the same vocabulary (RFC 0011 §A).
type channelMemberRequest struct {
	ID      string `json:"id"`
	Respond string `json:"respond"`
}

// addMemberRequest is the JSON body for POST /api/v1/channels/{id}/members.
type addMemberRequest struct {
	ID      string `json:"id"`
	Respond string `json:"respond"`
}

// publishMessageRequest is the JSON body for POST /api/v1/channels/{id}/messages.
//
// `SenderID` is REQUIRED. The orchestrator does not infer sender identity
// in v0.3.0 — auth tokens land in RFC 0009 Phase 4. When the publish
// crosses agent → orchestrator, the agent-side `ActionExecutor` populates
// `sender_id` from its registered ID; human clients (CLI, curl) must
// supply it explicitly.
type publishMessageRequest struct {
	SenderID    string         `json:"sender_id"`
	Content     string         `json:"content"`
	ThreadID    string         `json:"thread_id,omitempty"`
	Mentions    []string       `json:"mentions,omitempty"`
	ChannelType string         `json:"channel_type,omitempty"` // optional cross-check (RFC 0011 §C)
	Metadata    map[string]any `json:"metadata,omitempty"`
	// SessionID is the optional RFC 0031 Phase 3 `--session` override. When
	// present it replaces the orchestrator's boot-default session
	// (`Server.channelSessionID`) for this one publish — both as the
	// persisted row's `session_id` and as the value the dispatch path emits
	// as the `persatrix-session` header (overriding the ISSUE-0082
	// auto-binding). Absent, the boot default / auto-binding stands.
	SessionID string `json:"session_id,omitempty"`
}

// channelResponse is the JSON shape returned by GET/POST /api/v1/channels.
type channelResponse struct {
	ID          string           `json:"id"`
	Name        string           `json:"name,omitempty"` // empty for DM/thread
	Type        string           `json:"channel_type"`
	Description string           `json:"description"`
	CreatedAt   time.Time        `json:"created_at"`
	Members     []memberResponse `json:"members,omitempty"`
}

type memberResponse struct {
	ID            string    `json:"id"`
	RespondPolicy string    `json:"respond"`
	JoinedAt      time.Time `json:"joined_at"`
}

// channelMessageResponse is the JSON shape for individual messages
// returned by the publish, history, and thread endpoints.
type channelMessageResponse struct {
	ID        string         `json:"id"`
	ChannelID string         `json:"channel_id"`
	SenderID  string         `json:"sender_id"`
	Content   string         `json:"content"`
	Timestamp time.Time      `json:"timestamp"`
	ThreadID  string         `json:"thread_id,omitempty"`
	Mentions  []string       `json:"mentions"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

// listChannelsResponse is the envelope for GET /api/v1/channels.
//
// `NextCursor` is opaque to clients: the handler echoes back the last
// returned channel id and the store applies it as `WHERE id > ?` on
// the follow-up request. Empty when the page returns the trailing
// rows so clients know to stop paginating. ISSUE-0015.
type listChannelsResponse struct {
	Channels   []channelResponse `json:"channels"`
	NextCursor string            `json:"next_cursor,omitempty"`
}

// historyResponse is the envelope for GET /api/v1/channels/{id}/messages
// and GET /api/v1/channels/{id}/messages/{msg_id}/thread.
type historyResponse struct {
	Messages []channelMessageResponse `json:"messages"`
}
