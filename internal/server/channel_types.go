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
// edit). It is a full REPLACE of the member's editable config:
//   - `respond` is the new disposition and is REQUIRED — unlike add-member's
//     bare-id shorthand it is NOT defaulted, because the disposition is what
//     re-derives the salience bid and is unrecoverable from stored state (the
//     handler rejects an empty value with 400). See [Server.handleUpdateChannelMember].
//   - `threshold` is the new salience bar. Absent/null carries no explicit value,
//     which resolves the same way config load does: a `chair` picks up
//     [channels.DefaultChairThreshold], any other open-floor disposition is left
//     unset (bias-to-silence). A number outside [0, 1], or any threshold on a
//     non-open-floor disposition, is a 400.
//
// The participant id is the path segment, not a body field.
type updateMemberRequest struct {
	Respond   string   `json:"respond"`
	Threshold *float64 `json:"threshold"`
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

// membershipIntervalResponse is one stint in the RFC 0035 Phase 2 membership-
// history payload: the join instant and, for a closed stint, the leave instant.
// `left_at` is omitted while the stint is open, so an operator reads an absent
// `left_at` as "still a member". The interval is half-open `[joined_at, left_at)`
// (RFC 0035 §F): a closed stint's `left_at` is the instant membership ended.
type membershipIntervalResponse struct {
	JoinedAt time.Time  `json:"joined_at"`
	LeftAt   *time.Time `json:"left_at,omitempty"`
}

// membershipHistoryResponse is the envelope for
// GET /api/v1/channels/{id}/members/{participant_id}/history (RFC 0035 Phase 2).
// `intervals` is ordered oldest stint first and is always an array (never null),
// so a participant with no history in the channel reads back `{"intervals": []}`
// at 200 — distinct from the 404 a non-existent channel returns.
type membershipHistoryResponse struct {
	Intervals []membershipIntervalResponse `json:"intervals"`
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
	Revision                               int64                    `json:"revision"`
	FloorControl                           configFieldResponse      `json:"floor_control"`
	SalienceMaxChannelMembers              configFieldResponse      `json:"salience_max_channel_members"`
	MaxRepliesPerParticipantPerInteraction configFieldResponse      `json:"max_replies_per_participant_per_interaction"`
	EndVoteThreshold                       configFieldResponse      `json:"end_vote_threshold"`
	EndVoteWindow                          configFieldResponse      `json:"end_vote_window"`
	EscalationChairID                      configFieldResponse      `json:"escalation_chair_id"`
	InteractionIdleTimeoutSeconds          configFieldResponse      `json:"interaction_idle_timeout_seconds"`
	InteractionBudgetTokens                configFieldResponse      `json:"interaction_budget_tokens"`
	Reasoning                              reasoningConfigResponse  `json:"reasoning"`
	Autonomous                             autonomousConfigResponse `json:"autonomous"`
	// AutonomousRuntime is the LIVE convening-count / aggregate-bound readout
	// (RFC 0052 §E) — runtime counters, not config provenance; see
	// [autonomousRuntimeResponse]. Always present (zero-valued for a
	// non-autonomous or never-convened channel); the clients decide whether to
	// render it.
	AutonomousRuntime autonomousRuntimeResponse `json:"autonomous_runtime"`
}

// reasoningConfigResponse is the RFC 0051 (v0.3.10) `reasoning` block's nested
// view in the config payload — the first NESTED knob on the RFC 0050 surface. Each
// sub-knob carries its own effective value + provenance, so an operator sees at a
// glance which rung is inherited vs explicitly set (e.g. an override of `mode`
// alone reports model/depth/revise as "default").
type reasoningConfigResponse struct {
	Mode   configFieldResponse `json:"mode"`
	Model  configFieldResponse `json:"model"`
	Depth  configFieldResponse `json:"depth"`
	Revise configFieldResponse `json:"revise"`
}

// autonomousConfigResponse is the RFC 0052 (v0.3.11) `autonomous` block's nested
// view in the config payload — the second NESTED knob on the RFC 0050 surface
// (after reasoning). Each sub-knob carries its own effective value + provenance, so
// an operator sees which fields are inherited vs explicitly set. `agenda` is always
// an array (never null). The block is LIVE as of PR 3 — the convene path consults
// it (an armed channel is convenable via POST …/convene); this response layer just
// reports/edits it.
type autonomousConfigResponse struct {
	Enabled                 configFieldResponse `json:"enabled"`
	Topic                   configFieldResponse `json:"topic"`
	Agenda                  configFieldResponse `json:"agenda"`
	Convener                configFieldResponse `json:"convener"`
	Goal                    configFieldResponse `json:"goal"`
	MaxRounds               configFieldResponse `json:"max_rounds"`
	ScheduleIntervalSeconds configFieldResponse `json:"schedule_interval_seconds"`
	MaxConvenings           configFieldResponse `json:"max_convenings"`
	StandingBudgetTokens    configFieldResponse `json:"standing_budget_tokens"`
}

// autonomousRuntimeResponse is the LIVE (non-config) readout of an autonomous
// channel's aggregate convening state — RFC 0052 §E, the convening-count /
// aggregate-bound view the web AutonomousSettings panel + CLI `channel config
// get` render (v0.3.11 PR 7b). Unlike the config block ([autonomousConfigResponse],
// value + provenance), these are RUNTIME counters
// ([channels.ChannelRouter.ConveningCount]): they report what has HAPPENED, not
// what is CONFIGURED, so they carry no `source`. Process-lifetime state — a
// restart resets `convening_count` to zero (convening_counter.go's documented
// scope limit), so the readout is "this process", not "since the channel was
// created".
//
//   - ConveningCount — how many openers this channel has dispatched this process
//     lifetime (0 for a never-convened channel).
//   - ConveningsRemaining — the `max_convenings` allowance left (clamped at zero
//     if a lowered bound sits below the spent count), or nil ⇒ JSON `null` when
//     the channel carries no positive `max_convenings` (unbounded). Computed
//     server-side so the unbounded + clamp rules live in one place.
type autonomousRuntimeResponse struct {
	ConveningCount      int  `json:"convening_count"`
	ConveningsRemaining *int `json:"convenings_remaining"`
}

// recallRequest is the JSON body for POST /api/v1/personas/{participant_id}/recall
// (RFC 0036 PR 3). The scope participant is the PATH segment, never a body field
// — so an LLM-supplied tool argument (PR 4) can never widen or redirect the
// membership scope. Every field below is a non-scope narrowing of an already
// access-checked result set.
//
//   - `query` is the free-text search; empty / pure-punctuation degrades to a
//     recency-ordered listing of the in-scope set (the store decides), so it is
//     not required here.
//   - `channel_id` / `sender` narrow to one channel / one author.
//   - `after` (inclusive) / `before` (exclusive) bound the time window; an absent
//     value decodes to the zero [time.Time], which the store reads as "unset".
//     The zero value — not tag omission — is the "unset" signal: a `time.Time` is
//     a struct, so `omitempty` cannot omit it, and the tag is left off the two
//     time fields rather than carried as a no-op that reads as a wire contract it
//     is not. A non-RFC3339 string is a decode error → 400, like any malformed body.
//   - `limit` is forwarded unmodified; the store clamps it to
//     [channels.MaxRecallLimit], so the bound holds even for a caller that
//     bypasses the persona tool.
//   - `epoch_id` is the optional ISSUE-0085 run-isolation override (§OQ-6 lock),
//     resolved through the same [Server.resolveEpochOverride] plumbing the publish
//     handler uses; absent ⇒ "" ⇒ the store's [channels.DefaultEpochID] ("live").
//     CAVEAT: the channel store is not epoch-partitioned today. The publish path
//     never stamps a non-"live" epoch on a persisted message — the override rides
//     the gRPC dispatch rail, not the row (channel_epoch_override.go) — so
//     `messages.epoch_id` is universally "live" in production. An explicit
//     non-"live" epoch therefore matches nothing published through the real path;
//     the filter is a forward-looking defensive guard, not a live isolation axis.
//     Pinned by TestRecallEndpoint_RealPublishPath_ExplicitEpochUnreachable.
type recallRequest struct {
	Query     string    `json:"query"`
	ChannelID string    `json:"channel_id,omitempty"`
	Sender    string    `json:"sender,omitempty"`
	After     time.Time `json:"after"`
	Before    time.Time `json:"before"`
	Limit     int       `json:"limit,omitempty"`
	EpochID   string    `json:"epoch_id,omitempty"`
}

// recallMessageResponse is one recalled message in the RFC 0036 PR 3 payload.
// Deliberately a narrower shape than [channelMessageResponse]: recall surfaces
// only the verbatim quote and its provenance (origin channel + author), not the
// thread/mentions/metadata plumbing — the persona tool (PR 4) tags each row with
// `channel_id` + `sender` so the model knows it is quoting cross-context
// material. `message_id` / `sender` use the persona-facing field names.
type recallMessageResponse struct {
	MessageID string    `json:"message_id"`
	ChannelID string    `json:"channel_id"`
	Sender    string    `json:"sender"`
	Timestamp time.Time `json:"timestamp"`
	Content   string    `json:"content"`
}

// recallResponse is the envelope for POST /api/v1/personas/{participant_id}/recall.
// `messages` is always an array (never null), newest-relevant first per the
// store's BM25-dominant ranking.
type recallResponse struct {
	Messages []recallMessageResponse `json:"messages"`
}

// channelActivityResponse is the envelope for GET /api/v1/channels/{id}/activity
// (RFC 0048 console presence Tier 1). `Thinking` is the set of participant ids
// the orchestrator has an in-flight turn for — those it dispatched to and is
// awaiting a reply from — and is always an array (never null) so the console
// can treat an idle channel as an empty list without a special case.
type channelActivityResponse struct {
	Thinking []string `json:"thinking"`
}
