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
// (see [Server.emitRecallAudit]), so misuse on the shared surface is at least
// observable. Verbatim cross-channel recall is a sensitive read; it leaves a
// trail.
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

	// ISSUE-0085 PR 5 epoch resolution, byte-identical to the publish handler: an
	// explicit body `epoch_id` rides the request context via resolveEpochOverride,
	// then EpochOverrideFromContext reads it back to bind RecallParams.EpochID. A
	// blank value leaves EpochID "" so the store resolves it to DefaultEpochID
	// ("live") — recall and publish agree on the run-isolation axis (§OQ-6). A
	// wire-illegal epoch is a 400, the same fail-loud posture publish takes.
	ctx, err := s.resolveEpochOverride(r.Context(), req.EpochID)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}

	params := channels.RecallParams{
		ParticipantID: participantID,
		Query:         req.Query,
		EpochID:       channels.EpochOverrideFromContext(ctx),
		ChannelID:     req.ChannelID,
		Sender:        req.Sender,
		After:         req.After,
		Before:        req.Before,
		Limit:         req.Limit, // forwarded unmodified — the store clamps to MaxRecallLimit
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
	s.emitRecallAudit(ctx, participantID, params, len(msgs))

	writeJSON(w, recallMessagesToResponse(msgs), http.StatusOK)
}

// emitRecallAudit emits the RFC 0009 `channel.recall` event for one executed
// recall. It records the calling persona, the query, the resolved epoch, the
// supplied narrowing parameters, and the result COUNT — and deliberately never
// the recalled content: the trail proves a sensitive read happened without
// itself copying the sensitive text into the audit log. Telemetry-class, so the
// high-volume persona-tool path batches rather than fsyncing per call.
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
	}
	// Record only the narrowing parameters that were actually supplied, so the
	// event stays compact and an auditor can read at a glance how the search was
	// scoped beyond membership.
	if p.ChannelID != "" {
		detail["channel_id"] = p.ChannelID
	}
	if p.Sender != "" {
		detail["sender"] = p.Sender
	}
	if p.Limit > 0 {
		detail["limit"] = p.Limit
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
	// it): an executed recall is uniformly a success — failures 500 before the
	// audit — so the field would carry no discriminating signal here.
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
