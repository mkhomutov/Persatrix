package server

// RFC 0036 PR 3 — the audited REST surface over verbatim message recall.
//
// PR 2 built the load-bearing access-control query at the store level
// ([channels.ChannelStore.RecallMessages]): a membership-scoped, epoch-filtered
// FTS5/LIKE search whose `membership_intervals` EXISTS join *is* the recall
// authorization decision. This file exposes it over REST as
// `POST /api/v1/personas/{participant_id}/recall`, binding the scope participant
// from the request PATH (never a body field) and emitting the RFC 0009
// `channel.recall` audit event server-side.
//
// Carved into its own file (not channel_handlers.go) per the RFC 0036 PR plan:
// channel_handlers.go sits at the repo's file-size review cap, so new endpoints
// route around it — the same split RFC 0035 (membership_history) and RFC 0050
// (config) used.

import (
	"context"
	"net/http"
	"strings"
	"time"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/security"
)

// handleRecallMessages handles POST /api/v1/personas/{participant_id}/recall.
//
// The scope participant is the PATH segment, bound into RecallParams.ParticipantID
// — never a body field. The store's `membership_intervals` EXISTS join is the
// access-control decision (RFC 0036 §C); this handler only binds trusted request
// context (the path id + the resolved epoch) into it, so a join → leave → rejoin
// recalls both stints and neither the pre-join period nor the removal gap, and a
// crafted query body can never widen or redirect the scope.
//
// POST, not GET (RFC 0036 §"REST shape"): recall carries a free-text body plus
// structured narrowing parameters and is audited — semantically a command, not a
// cacheable fetch.
//
// Auth posture (OQ #1): this adds NO bespoke auth. It matches the surrounding
// (currently unauthenticated, single-tenant) channel REST surface and inherits
// RFC 0009's identity/auth model when that lands; it MUST NOT ship more
// permissively than its neighbours. Until then every executed call is audited
// (see [Server.emitRecallAudit]) — but note the audited actor is the
// self-asserted PATH participant, not an authenticated identity: with no auth a
// caller can recall as any participant, and the trail then attributes the read to
// the claimed id, not the true caller. So the audit makes a recall observable as
// an EVENT, but cannot yet ATTRIBUTE it; real attribution arrives with RFC 0009.
// Verbatim cross-channel recall is a sensitive read; it leaves a trail.
func (s *Server) handleRecallMessages(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil {
		writeError(w, "UNAVAILABLE", "channel store not configured", http.StatusServiceUnavailable)
		return
	}
	if !requireJSON(w, r) {
		return
	}
	participantID := r.PathValue("participant_id")
	var req recallRequest
	if !decodeJSON(w, r, &req) {
		return
	}

	// ISSUE-0085 PR 5 epoch resolution, sharing the publish handler's plumbing: an
	// explicit body `epoch_id` rides the request context via resolveEpochOverride,
	// then EpochOverrideFromContext reads it back to bind RecallParams.EpochID. A
	// blank value leaves EpochID "" so the store resolves it to DefaultEpochID
	// ("live"). A wire-illegal epoch is a 400, the same fail-loud posture publish
	// takes.
	//
	// NB: recall and publish share this RESOLUTION but not its EFFECT. Publish's
	// resolved epoch rides the gRPC dispatch rail and is deliberately NOT persisted
	// (channel_epoch_override.go) — every stored message keeps the "live" column
	// default — whereas recall's resolved epoch binds the store's `messages.epoch_id`
	// filter. So an explicit non-"live" epoch matches nothing the real publish path
	// ever wrote; the §OQ-6 filter is a forward-looking guard, not a live isolation
	// boundary today. Pinned by TestRecallEndpoint_RealPublishPath_ExplicitEpochUnreachable.
	ctx, err := s.resolveEpochOverride(r.Context(), req.EpochID)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}

	params := channels.RecallParams{
		ParticipantID: participantID,
		Query:         req.Query,
		EpochID:       channels.EpochOverrideFromContext(ctx),
		// ISSUE-0107: the body `channel_id` narrower is canonicalized to the store's
		// prefixed id form (a bare `mt-recall-001` → `group:mt-recall-001`), so a
		// persona/tool that narrows by the human-facing channel name it sees in
		// context matches the same rows as the canonical id. `sender` is NOT
		// namespaced, so it is bound raw.
		ChannelID: canonicalNarrowChannelID(req.ChannelID),
		Sender:    req.Sender,
		After:     req.After,
		Before:    req.Before,
		Limit:     req.Limit, // forwarded unmodified — the store clamps to MaxRecallLimit
	}

	msgs, err := s.channelStore.RecallMessages(ctx, params)
	if err != nil {
		s.logger.Error("channels: recall failed",
			zap.String("participant_id", participantID), zap.Error(err))
		writeError(w, "INTERNAL", "failed to recall messages", http.StatusInternalServerError)
		return
	}

	// Audit AFTER the scoped query the server actually executed, recording the
	// result count, never the content (RFC 0036 §Security — Audit). Emitted here
	// rather than in the Phase 2 tool so a bypassed or misbehaving tool client
	// cannot suppress the trail.
	//
	// Executed reads only (PR #677 review finding #1): a request that fails before
	// this point — 400 (malformed body / wire-illegal epoch), 503 (store unset), or
	// 500 (store error, already logged at Error just above) — emits NO event. A
	// deliberate boundary, distinct from the empty-Outcome rationale in
	// [Server.emitRecallAudit]: a failed call read nothing, and auditing
	// attacker-controlled malformed input would let an unauthenticated caller
	// inflate the trail at will. Revisit once RFC 0009 gives an attempt an
	// attributable identity. Pinned by TestRecallEndpoint_FailedAttemptNotAudited.
	s.emitRecallAudit(ctx, participantID, params, len(msgs))

	writeJSON(w, recallMessagesToResponse(msgs), http.StatusOK)
}

// canonicalNarrowChannelID maps the recall body's optional `channel_id` narrowing
// param to the store-canonical id form before it is bound into RecallParams.
//
// The channel store matches `messages.channel_id` against the canonical, prefixed
// id (`group:<name>` / `dm:<…>` / `thread:<…>`) — the form the channel REST path
// handlers mint from the request (channel_handlers.go: `canonicalID := "group:" + req.Name`).
// Recall, however, takes `channel_id` from the request BODY, so a persona — or the
// recall tool — naturally narrows by the bare, human-facing channel name it sees
// in context (`mt-recall-001`), which matched nothing (ISSUE-0107). Prepend the
// `group:` prefix to a bare name so a bare and a canonical id narrow identically;
// leave an already-prefixed id (any known channel type) and the empty/un-narrowed
// case untouched.
//
// The prefix set (`group:`/`dm:`/`thread:`) mirrors channels.channelTypeFromID
// (router.go) and the ChannelType constants (channels.go); that helper is
// unexported, so the list is duplicated here rather than reused. A new channel
// type prefix must be added in this guard too — otherwise a bare id of that type
// would be silently treated as a group and narrow to nothing.
//
// Narrowing-only — this can never widen scope past the RFC 0035 membership EXISTS
// filter; at worst a mis-canonicalized id selects the wrong channel and returns a
// subset (the pre-fix behaviour was the empty set).
func canonicalNarrowChannelID(id string) string {
	if id == "" {
		return "" // un-narrowed — span every accessible channel
	}
	if strings.HasPrefix(id, "group:") || strings.HasPrefix(id, "dm:") || strings.HasPrefix(id, "thread:") {
		return id // already canonical
	}
	return "group:" + id // bare group name → canonical id
}

// emitRecallAudit emits the RFC 0009 `channel.recall` event for one executed
// recall. It records the calling persona, the query, the resolved epoch, the
// effective limit, the supplied narrowing parameters, and the result COUNT — and
// deliberately never the recalled content: the trail proves a sensitive read
// happened without itself copying the sensitive text into the audit log.
// Telemetry-class, so the high-volume persona-tool path batches rather than
// fsyncing per call.
//
// The `query` IS recorded verbatim — a conscious choice (PR #677 review finding
// #3). It is caller-supplied free text that could itself carry a sensitive
// phrase, but it is exactly what an auditor needs to read how the search was
// scoped, and it is the verbatim sibling of `memory.read`, which logs its query
// the same way. Only the recalled CONTENT — the rows the query returned — is
// withheld.
//
// AgentID == Resource == the persona: recall acts on behalf of, and is scoped
// to, the calling participant, so the participant is both the actor and the
// stable forensic anchor (the agent_id form the audit subsystem prefers). The
// channel(s) touched live in Detail when narrowed; an un-narrowed recall spans
// every accessible channel, so there is no single resource to name.
func (s *Server) emitRecallAudit(ctx context.Context, participantID string, p channels.RecallParams, resultCount int) {
	// The resolved epoch (what the store actually filtered on) — mirror the
	// store's empty-to-DefaultEpochID resolution so the audit names the world
	// that was searched, not the unresolved request value.
	epoch := p.EpochID
	if epoch == "" {
		epoch = channels.DefaultEpochID
	}
	detail := map[string]any{
		"query":        p.Query,
		"epoch_id":     epoch,
		"result_count": resultCount,
		// limit is ALWAYS applied (every recall has an effective cap), so unlike the
		// optional narrowers below it is always recorded — and as the EFFECTIVE value
		// the store applied, via the same [channels.RecallParams.EffectiveLimit] the
		// store LIMITs on. So an auditor reading result_count == limit knows the set
		// was truncated; recording the raw request would mis-read a clamped result as
		// un-truncated (PR #677 review finding #2).
		"limit": p.EffectiveLimit(),
	}
	// Record only the narrowing parameters that were actually supplied, so the
	// event stays compact and an auditor can read at a glance how the search was
	// scoped beyond membership.
	//
	// `channel_id` here is the CANONICALIZED value: emitRecallAudit runs on the
	// post-canonicalNarrowChannelID params, so a bare request narrow
	// (`mt-recall-001`) is audited as the id the store actually filtered on
	// (`group:mt-recall-001`) — the audit names the searched channel, not the raw
	// request form (ISSUE-0107). Pinned by TestRecallEndpoint_BareChannelNarrowMatchesCanonical.
	if p.ChannelID != "" {
		detail["channel_id"] = p.ChannelID
	}
	if p.Sender != "" {
		detail["sender"] = p.Sender
	}
	// Time bounds as RFC3339 strings, not raw time.Time — clean, human-readable
	// audit JSON and no struct for the redactor's reflective walk to descend into.
	if !p.After.IsZero() {
		detail["after"] = p.After.UTC().Format(time.RFC3339)
	}
	if !p.Before.IsZero() {
		detail["before"] = p.Before.UTC().Format(time.RFC3339)
	}
	// Outcome is left empty, matching the existing server emit sites (none populate
	// it): an executed recall is uniformly a success — every failure path returns
	// before this emit (see the call site) — so the field would carry no
	// discriminating signal here.
	s.emitAudit(ctx, security.AuditEvent{
		EventType: security.AuditChannelRecall,
		AgentID:   participantID,
		Action:    "recall",
		Resource:  participantID,
		Detail:    detail,
	})
}

// recallMessagesToResponse maps the store's [channels.ChannelMessage] slice to
// the narrower recall wire shape (verbatim quote + provenance only). Always
// returns a non-nil `Messages` slice so the payload is `{"messages": []}` (never
// null) for an empty result.
func recallMessagesToResponse(in []channels.ChannelMessage) recallResponse {
	out := recallResponse{Messages: make([]recallMessageResponse, 0, len(in))}
	for _, m := range in {
		out.Messages = append(out.Messages, recallMessageResponse{
			MessageID: m.ID,
			ChannelID: m.ChannelID,
			Sender:    m.SenderID,
			Timestamp: m.Timestamp,
			Content:   m.Content,
		})
	}
	return out
}
