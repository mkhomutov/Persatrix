// Channel REST handlers — RFC 0011 §C, Phase 1b.
//
// This file ships the create/list/get/publish/history/thread/add-member
// endpoints. The two DELETE endpoints listed in the §C table are
// deferred to PR 4 alongside the response gate that needs to react to
// membership removal.
package server

import (
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/google/uuid"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// channelDefaultListLimit and channelDefaultHistoryLimit mirror the §C
// "Query parameters" table defaults. Bounded above by `channelMaxLimit`
// so a hostile `?limit=999999` cannot allocate gigabytes of slice
// before the store query starts. The cap is generous (the global
// `max_channels` cap is 50 by default) and small enough that one page
// fits comfortably in a single TCP segment.
const (
	// channelDefaultListLimit is intentionally aligned with
	// channels.DefaultMaxChannels (the global named-group cap) so the
	// default page size never exceeds the maximum number of channels
	// the store can hold. PR #245 review (round 3) Nice-to-Have #3.
	channelDefaultListLimit    = channels.DefaultMaxChannels
	channelDefaultHistoryLimit = 50
	channelDefaultThreadLimit  = 100
	channelMaxLimit            = 1000

	// channelMaxMentionsPerPublish bounds the `mentions` array on a
	// REST publish (ISSUE-0011 / PR #245 review SF-3). Mirrors the
	// agent-side `_MAX_MENTIONS_PER_ACTION` (agents/action_executor.py)
	// so a publish that would have been truncated agent-side is rejected
	// loudly when it arrives via the unauthenticated REST surface
	// instead. Defense-in-depth: the PR 4b response gate amplifies
	// per-publish work with `len(mentions)`, and v0.3.0 has no auth on
	// this endpoint until RFC 0009 Phase 4. Auth lift in Phase 4 does
	// not retire this cap — mention spam from a compromised credential
	// would still amplify, so the cap stays.
	channelMaxMentionsPerPublish = 10
)

// channelFallbackWarnOnce guards the once-per-process Warn emitted by
// handlePublishMessage when no router is wired (test-fixture path; see
// PR #245 review round 3 Should-Fix #3). Package-level so handler
// instances created across multiple Server constructors share the
// guard — a single misconfiguration warning per process lifetime is
// sufficient to alert ops without flooding the publish hot path.
var channelFallbackWarnOnce sync.Once

// TestingResetChannelFallbackWarnOnce resets the package-level guard to its
// zero state. It is exported for use in test-setup functions only; production
// code must never call it.
//
// Direct assignment (channelFallbackWarnOnce = sync.Once{}) in test files is
// a data race whenever any test in the package runs with t.Parallel(), because
// the assignment is not protected by a lock while the guard's Do path may be
// executing concurrently. Calling this function from a single-goroutine
// TestXxx setup site (before any goroutines are launched) is safe by Go's
// test execution model. ISSUE-0009 / PR #246 finding M2.
func TestingResetChannelFallbackWarnOnce() {
	channelFallbackWarnOnce = sync.Once{}
}

// handleCreateChannel handles POST /api/v1/channels.
//
// Only group channels are creatable here (RFC 0011 §C). The server
// derives the canonical ID (`group:<name>`) so callers cannot supply a
// drifted `(id, name)` pair — SF-2 of PR #231 review hardens this on
// the store side too, but rejecting at the boundary gives a clearer
// error.
func (s *Server) handleCreateChannel(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	if !requireJSON(w, r) {
		return
	}
	var req createChannelRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Name == "" {
		writeError(w, "BAD_REQUEST", "name is required", http.StatusBadRequest)
		return
	}
	if len(req.Members) == 0 {
		writeError(w, "BAD_REQUEST", "at least one member is required", http.StatusBadRequest)
		return
	}

	canonicalID := "group:" + req.Name
	ch := channels.Channel{
		ID:          canonicalID,
		Name:        req.Name,
		Type:        channels.ChannelTypeGroup,
		Description: req.Description,
		SessionID:   s.channelSessionID, // RFC 0031 Phase 1 — empty falls through to legacy
	}

	// PR #245 review (High): the previous implementation called
	// CreateChannel followed by an N-call AddMember loop with no
	// transaction. A failure mid-loop left an orphan channel that
	// poisoned the client's natural retry with 409 CONFLICT. The store's
	// CreateChannelWithMembers helper makes the bundle atomic so we no
	// longer need handler-side rollback. Member translation lives next to
	// the wire shape (resolveMemberRequests, channel_types.go) because
	// channelMemberRequest is server-local.
	members, err := resolveMemberRequests(req.Members)
	if err != nil {
		s.writeChannelError(w, err)
		return
	}
	if err := s.channelStore.CreateChannelWithMembers(r.Context(), ch, members); err != nil {
		s.writeChannelError(w, err)
		return
	}

	// RFC 0030 — stamp the default governance bundle (floor control, the Tier B
	// salience cap, and the Layer 2 reply budget) so a runtime-created group
	// channel matches a config-declared one; see applyRuntimeGroupGovernance.
	s.applyRuntimeGroupGovernance(canonicalID)

	created, err := s.channelStore.GetChannel(r.Context(), canonicalID)
	if err != nil {
		s.logger.Error("channels: post-create lookup failed",
			zap.String("channel_id", canonicalID), zap.Error(err))
		writeError(w, "INTERNAL", "failed to load created channel", http.StatusInternalServerError)
		return
	}
	resp := channelToResponse(created, nil)
	writeJSON(w, resp, http.StatusCreated)
}

// handleListChannels handles GET /api/v1/channels.
//
// ISSUE-0015: paginates via keyset (`?cursor=<last id>`) and pushes
// LIMIT into SQL so a deployment past the soft cap does not load the
// whole table per request. Fetches `limit + 1` rows so the presence
// of the extra row signals "more pages exist" without a separate
// COUNT query; the response trims to `limit` and surfaces the last
// kept id as `next_cursor`.
func (s *Server) handleListChannels(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	limit, err := parseLimit(r, channelDefaultListLimit)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}
	cursor := r.URL.Query().Get("cursor")
	chs, err := s.channelStore.ListChannels(r.Context(), limit+1, cursor)
	if err != nil {
		s.logger.Error("channels: list failed", zap.Error(err))
		writeError(w, "INTERNAL", "failed to list channels", http.StatusInternalServerError)
		return
	}
	var nextCursor string
	if len(chs) > limit {
		// The probe row indicates "more pages exist". Trim it off so
		// the page contains exactly `limit` rows and surface the last
		// kept id as the next cursor.
		chs = chs[:limit]
		nextCursor = chs[len(chs)-1].ID
	}
	out := make([]channelResponse, 0, len(chs))
	// PR #316 deep-review A-3a: N+1 (1 ListChannels + N GetMembers), bounded
	// by channels.DefaultMaxChannels=50. If that cap ever rises, replace this
	// loop with a batched `ListChannelsWithMembers` store helper before
	// merging the raise — the trade-off only holds while the cap is small.
	for _, c := range chs {
		members, mErr := s.channelStore.GetMembers(r.Context(), c.ID)
		if mErr != nil {
			s.logger.Error("channels: list members fetch failed",
				zap.String("channel_id", c.ID), zap.Error(mErr))
			writeError(w, "INTERNAL", "failed to load channel members", http.StatusInternalServerError)
			return
		}
		out = append(out, channelToResponse(c, members))
	}
	writeJSON(w, listChannelsResponse{Channels: out, NextCursor: nextCursor}, http.StatusOK)
}

// handleGetChannel handles GET /api/v1/channels/{id}.
func (s *Server) handleGetChannel(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")
	ch, err := s.channelStore.GetChannel(r.Context(), id)
	if err != nil {
		s.writeChannelError(w, err)
		return
	}
	members, err := s.channelStore.GetMembers(r.Context(), id)
	if err != nil {
		s.logger.Error("channels: get members failed",
			zap.String("channel_id", id), zap.Error(err))
		writeError(w, "INTERNAL", "failed to load channel members", http.StatusInternalServerError)
		return
	}
	writeJSON(w, channelToResponse(ch, members), http.StatusOK)
}

// handlePublishMessage handles POST /api/v1/channels/{id}/messages.
//
// Routes through [ChannelRouter.Publish] when a router was injected, so
// channel_type cross-validation and fanout fire. Falls back to a direct
// store write when the router is unset (test fixtures only — production
// always wires the router). The fallback path emits a once-per-process
// Warn ([channelFallbackWarnOnce]) so a forgotten WithChannels(store,
// router) wiring is observable without flooding the publish hot path.
func (s *Server) handlePublishMessage(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	if !requireJSON(w, r) {
		return
	}
	id := r.PathValue("id")
	var req publishMessageRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.SenderID == "" {
		writeError(w, "BAD_REQUEST", "sender_id is required", http.StatusBadRequest)
		return
	}
	if req.Content == "" {
		writeError(w, "BAD_REQUEST", "content is required", http.StatusBadRequest)
		return
	}
	// ISSUE-0011 (PR #245 review SF-3): per-element validation lives in
	// the store (`ValidateParticipantID` per mention), but a count cap
	// has to live here — the store accepts whatever it gets. Reject
	// loudly so misconfigured prompts surface with a 400 rather than
	// silently amplifying the response gate's per-recipient work.
	if len(req.Mentions) > channelMaxMentionsPerPublish {
		writeError(w, "BAD_REQUEST",
			fmt.Sprintf("mentions: count %d exceeds cap %d",
				len(req.Mentions), channelMaxMentionsPerPublish),
			http.StatusBadRequest)
		return
	}
	// [RFC 0011 amendment 'Cascade-depth wire propagation']: the wire
	// schema is loose on the upper bound (operators do not know the
	// deployment's cap), but a negative or non-integer cascade_depth
	// is always a publisher bug. Loud-fail at the boundary; the
	// router-side clamp ([0, max_cascade_depth]) is defense-in-depth
	// for programmatic callers that bypass this handler.
	//
	// [RFC 0011 amendment 'Cascade-depth wire propagation']: ../../docs/rfcs/0011-amendment-cascade-depth-wire-propagation.md
	if msg, ok := validateRequestCascadeDepth(req.Metadata); !ok {
		writeError(w, "BAD_REQUEST", msg, http.StatusBadRequest)
		return
	}

	// RFC 0031 Phase 3 PR 4: apply the optional `session_id` override (the
	// CLI's `--session`) — see [Server.resolveSessionOverride].
	ctx, effectiveSession, err := s.resolveSessionOverride(r.Context(), req.SessionID)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}

	// ISSUE-0085 PR 5: apply the optional `epoch_id` override (the CLI's
	// `--epoch`) onto the same dispatch context — see [Server.resolveEpochOverride].
	// The epoch is not stamped on the persisted row (unlike the session), so
	// there is no effective-id return; only the dispatch context is threaded.
	ctx, err = s.resolveEpochOverride(ctx, req.EpochID)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}

	// RFC 0011 display-name-mention-lifting amendment (ML1): resolve the prose
	// `@`-mentions personas actually write ("@Iron Fox") to canonical ids and
	// union them into the structured array BEFORE persist and fanout, so the
	// floor resolution and both gates see the addressees the prose always
	// meant. Runs after the structured >cap 400 (above) and is fail-open — a
	// resolution miss returns the producer's array untouched.
	// See [Server.liftContentMentions].
	req.Mentions = s.liftContentMentions(ctx, id, req.SenderID, req.Content, req.Mentions)

	msg := channels.ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  req.SenderID,
		Content:   req.Content,
		Timestamp: time.Now().UTC(),
		ThreadID:  req.ThreadID,
		Mentions:  req.Mentions,
		Metadata:  req.Metadata,
		SessionID: effectiveSession, // RFC 0031 Phase 1 — empty falls through to legacy
	}

	var pubErr error
	if s.channelRouter != nil {
		// PublishAsync (not Publish) returns at the persistence boundary and
		// runs fanout on a detached goroutine — the synchronous Publish blocked
		// this response on the full agent round (up to 45s/speaker under floor
		// control), stranding the RFC 0048 console composer for 90-135s. Replies
		// surface via the history poll; delivery errors are counted, not returned.
		pubErr = s.channelRouter.PublishAsync(ctx, msg, req.ChannelType)
	} else {
		// PR #245 review (round 3) Should-Fix #3: signpost the
		// router-nil fallback once per process. The fallback path
		// silently bypasses channel_type cross-validation and the
		// channel.messages.delivered metric — both contracts that
		// production callers rely on. Emitting once (sync.Once) keeps
		// the publish hot path log-noise-free while still surfacing
		// the misconfiguration to ops at first traffic.
		channelFallbackWarnOnce.Do(func() {
			s.logger.Warn("channels: publish via router-nil fallback (channel_type validation and delivery metric skipped); wire WithChannels(store, router) in production")
		})
		pubErr = s.channelStore.PublishMessage(r.Context(), msg)
	}
	if pubErr != nil {
		s.writeChannelError(w, pubErr)
		return
	}

	stored, err := s.channelStore.GetMessage(r.Context(), msg.ID)
	if err != nil {
		// The publish committed; lookup failure is internal-only.
		s.logger.Error("channels: post-publish lookup failed",
			zap.String("message_id", msg.ID), zap.Error(err))
		writeJSON(w, messageToResponse(msg), http.StatusCreated)
		return
	}
	writeJSON(w, messageToResponse(stored), http.StatusCreated)
}

// handleGetChannelHistory handles GET /api/v1/channels/{id}/messages.
func (s *Server) handleGetChannelHistory(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	id := r.PathValue("id")
	if _, err := s.channelStore.GetChannel(r.Context(), id); err != nil {
		s.writeChannelError(w, err)
		return
	}
	limit, err := parseLimit(r, channelDefaultHistoryLimit)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}
	before, err := parseBefore(r)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}
	msgs, err := s.loadChannelHistory(r.Context(), r, id, limit, before)
	if err != nil {
		s.logger.Error("channels: history failed",
			zap.String("channel_id", id), zap.Error(err))
		writeError(w, "INTERNAL", "failed to load channel history", http.StatusInternalServerError)
		return
	}
	writeJSON(w, historyResponse{Messages: messagesToResponse(msgs)}, http.StatusOK)
}

// handleGetThread handles GET /api/v1/channels/{id}/messages/{msg_id}/thread.
func (s *Server) handleGetThread(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	chID := r.PathValue("id")
	msgID := r.PathValue("msg_id")
	if _, err := s.channelStore.GetChannel(r.Context(), chID); err != nil {
		s.writeChannelError(w, err)
		return
	}
	if _, err := s.channelStore.GetMessage(r.Context(), msgID); err != nil {
		s.writeChannelError(w, err)
		return
	}
	limit, err := parseLimit(r, channelDefaultThreadLimit)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}
	msgs, err := s.channelStore.GetThread(r.Context(), msgID, limit)
	if err != nil {
		s.logger.Error("channels: thread failed",
			zap.String("channel_id", chID),
			zap.String("message_id", msgID),
			zap.Error(err))
		writeError(w, "INTERNAL", "failed to load thread", http.StatusInternalServerError)
		return
	}
	writeJSON(w, historyResponse{Messages: messagesToResponse(msgs)}, http.StatusOK)
}

// handleAddChannelMember handles POST /api/v1/channels/{id}/members.
func (s *Server) handleAddChannelMember(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	if !requireJSON(w, r) {
		return
	}
	id := r.PathValue("id")
	var req addMemberRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.ID == "" {
		writeError(w, "BAD_REQUEST", "id is required", http.StatusBadRequest)
		return
	}
	if err := s.channelStore.AddMember(r.Context(), id, req.ID, wireRespondPolicy(req.Respond)); err != nil {
		s.writeChannelError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// channelToResponse converts a [channels.Channel] (and an optional
// member slice) to the wire shape.
//
// `ch.SessionID` is intentionally not surfaced. Phase 1 of RFC 0031
// (PR #335) ships no operator-visible session surface — the Phase 3 CLI
// (`persatrix session list / use / archive`) owns that contract. Adding
// `session_id` to this struct would bake an unversioned wire field that
// a future operator-facing API has to either rename or replicate. Leave
// it off until Phase 3 lands.
func channelToResponse(ch channels.Channel, members []channels.Member) channelResponse {
	out := channelResponse{
		ID:          ch.ID,
		Name:        ch.Name,
		Type:        string(ch.Type),
		Description: ch.Description,
		CreatedAt:   ch.CreatedAt,
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
		ID:        m.ID,
		ChannelID: m.ChannelID,
		SenderID:  m.SenderID,
		Content:   m.Content,
		Timestamp: m.Timestamp,
		ThreadID:  m.ThreadID,
		Mentions:  m.Mentions,
		Metadata:  m.Metadata,
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

// writeChannelError lives in channel_errors.go.
// parseLimit + parseBefore live in channel_query_params.go.
// validateRequestCascadeDepth lives in channel_cascade_depth.go.
