package server

import (
	"time"

	"github.com/mkhomutov/persatrix/internal/channels"
)

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

// updateMemberRequest is the JSON body for
// PATCH /api/v1/channels/{id}/members/{participant_id} (RFC 0050 member-config
// edit). It is a REPLACE of the member's editable config: `respond` is the new
// disposition (empty → when_mentioned, the add-member default) and `threshold`
// the new salience bar — a tri-state pointer where absent/null unsets it
// (bias-to-silence). The participant id is the path segment, not a body field.
type updateMemberRequest struct {
	Respond   string   `json:"respond"`
	Threshold *float64 `json:"threshold,omitempty"`
}

// wireRespondPolicy applies the RFC 0011 §A default to a wire-supplied
// `respond` string: empty means `when_mentioned`, matching the config
// loader's bare-ID shorthand. The single defaulting rule for the REST
// surface (create-channel and add-member translations); the store keeps
// its own copy inside CreateChannelWithMembers for its non-REST caller,
// the config reconcile path.
func wireRespondPolicy(respond string) channels.RespondPolicy {
	if respond == "" {
		return channels.RespondWhenMentioned
	}
	return channels.RespondPolicy(respond)
}

// resolveMemberRequests translates the wire-shape member list into store
// members, judging each member's full request validity — identity
// ([channels.ValidateParticipantID]) and declared disposition
// ([channels.ResolveMemberPolicy]) — here at the wire boundary, in the
// same id-then-policy order the store's AddMember uses. An invalid value
// surfaces the same sentinel the store would have returned
// ([channels.ErrInvalidParticipantID] / [channels.ErrInvalidRespondPolicy])
// — before the channel row is created instead of mid-transaction, so a
// malformed body 400s ahead of any state conflict (pinned by the
// TestChannels_CreateChannel_Invalid*WinsOverConflict pair).
func resolveMemberRequests(reqs []channelMemberRequest) ([]channels.Member, error) {
	members := make([]channels.Member, 0, len(reqs))
	for _, m := range reqs {
		if err := channels.ValidateParticipantID(m.ID); err != nil {
			return nil, err
		}
		mp, err := channels.ResolveMemberPolicy(wireRespondPolicy(m.Respond))
		if err != nil {
			return nil, err
		}
		members = append(members, channels.Member{
			ParticipantID: m.ID,
			RespondPolicy: mp.Policy,
			SalienceGated: mp.SalienceGated,
			Threshold:     mp.Threshold,
		})
	}
	return members, nil
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
	// EpochID is the optional ISSUE-0085 PR 5 `--epoch` override. When present
	// it replaces the orchestrator's boot-resolved process epoch
	// (PERSATRIX_EPOCH) for this one publish — emitted as the `persatrix-epoch`
	// header the dispatch path sends to the persona (the run/test-isolation
	// axis), orthogonal to `session_id` (room-continuity). Unlike SessionID it
	// is NOT stamped on the persisted row (the channel-store `epoch_id` column
	// keeps its "live" default); absent, the boot epoch stands.
	EpochID string `json:"epoch_id,omitempty"`
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
	// SalienceGated / Threshold surface the RFC 0030 Tier B signal (v0.3.8).
	// The store normalizes the disposition vocabulary to the legacy triple
	// before persisting (chair/participant → always, observer → never), so
	// `respond` alone cannot distinguish a salience-gated participant from a
	// legacy always-replier. These two fields are the only thing that survives
	// the store boundary (see [channels.Member.SalienceGated]); without them an
	// operator cannot read back the disposition they set. Threshold is a
	// pointer/omitempty tri-state: absent → unset (bias-to-silence).
	SalienceGated bool     `json:"salience_gated"`
	Threshold     *float64 `json:"threshold,omitempty"`
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

// configFieldResponse is one governance knob's resolved view in the RFC 0050
// `GET/PATCH …/config` payload: the effective `value` plus its `source`
// provenance. `source` is "channel" when an explicit per-channel override is
// persisted for the knob, or "default" when the channel inherits the resolved
// fleet/group default. (The governance knobs in this set are all channel-scoped,
// so there is no third "member" level here — member-scoped settings, e.g. a
// member's salience threshold, ride the member surface, not this one.) `value`
// is the typed effective value (bool / int / string). Every knob — including
// interaction_budget_tokens, made router-held in the RFC 0050 amendment
// (interaction-budget enforcement) — resolves its inherited effective value
// through a router getter, so `value` is never `null` for an inherited knob.
type configFieldResponse struct {
	Value  any    `json:"value"`
	Source string `json:"source"`
}

// channelConfigResponse is the JSON shape returned by GET and PATCH
// /api/v1/channels/{id}/config (RFC 0050 Phase 1 PR 4): the channel's current
// optimistic-concurrency `revision` plus each governed knob's effective value +
// provenance. The revision is the value a follow-up PATCH echoes back in the
// `If-Match` header; a knob's `source` lets an operator see at a glance which
// values are inherited vs explicitly set.
type channelConfigResponse struct {
	Revision                               int64               `json:"revision"`
	FloorControl                           configFieldResponse `json:"floor_control"`
	SalienceMaxChannelMembers              configFieldResponse `json:"salience_max_channel_members"`
	MaxRepliesPerParticipantPerInteraction configFieldResponse `json:"max_replies_per_participant_per_interaction"`
	EndVoteThreshold                       configFieldResponse `json:"end_vote_threshold"`
	EndVoteWindow                          configFieldResponse `json:"end_vote_window"`
	EscalationChairID                      configFieldResponse `json:"escalation_chair_id"`
	InteractionIdleTimeoutSeconds          configFieldResponse `json:"interaction_idle_timeout_seconds"`
	InteractionBudgetTokens                configFieldResponse `json:"interaction_budget_tokens"`
}

// channelActivityResponse is the envelope for GET /api/v1/channels/{id}/activity
// (RFC 0048 console presence Tier 1). `Thinking` is the set of participant ids
// the orchestrator has an in-flight turn for — those it dispatched to and is
// awaiting a reply from — and is always an array (never null) so the console
// can treat an idle channel as an empty list without a special case.
type channelActivityResponse struct {
	Thinking []string `json:"thinking"`
}
