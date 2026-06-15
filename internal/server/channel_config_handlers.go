// Channel governance-config REST handlers — RFC 0050 Phase 1 PR 4.
//
// This file exposes the store-canonical apply path
// ([channels.ChannelRouter.ApplyChannelConfig]) over HTTP as
// GET/PATCH /api/v1/channels/{id}/config, gated behind the operator-authored
// `config_edit_enabled` toggle (config/ui.yaml). The toggle gates BOTH the read
// and the write so the whole surface ships dark and so the CLI (PR 5) and a web
// client are gated by the one server-side check — see [Server.configEditEnabled].
//
// PATCH carries a sparse `{knob: value}` body where an explicit `null` unsets a
// knob back to inherit; the merge against the current stored overrides lives
// here (the apply path takes the COMPLETE desired override set, not a delta).
// Optimistic concurrency rides the `If-Match` header: the caller states the
// revision it last read, and a stale value is a 409 — the same primitive the
// store's compare-and-set enforces.
package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// configSourceChannel / configSourceDefault are the two provenance labels in a
// [configFieldResponse]. The governance knobs here are channel-scoped, so the
// provenance is binary: an explicit per-channel override ("channel") or the
// inherited fleet/group default ("default").
const (
	configSourceChannel = "channel"
	configSourceDefault = "default"
)

// configEditEnabled reports whether the operator turned on the RFC 0050
// per-channel config-edit surface (`config_edit_enabled` on channel_timeline).
// It reads only the operator toggle; subsystem availability (the router being
// wired) is a separate 503 check in the handlers, mirroring how the UI surface
// splits enabled (authored) from available (runtime-derived). A nil uiConfig
// falls back to the defaults, where the toggle ships OFF.
func (s *Server) configEditEnabled() bool {
	cfg := s.uiConfig
	if cfg == nil {
		cfg = DefaultUIConfig()
	}
	return cfg.Panels["channel_timeline"].ConfigEditEnabled
}

// handleGetChannelConfig handles GET /api/v1/channels/{id}/config — the
// channel's current revision plus each governed knob's effective value and
// provenance. Gated behind the config-edit toggle (the read is part of the same
// dark-by-default surface as the write, so the CLI `config get` and web alike
// see a 403 until an operator opts in).
func (s *Server) handleGetChannelConfig(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil || s.channelRouter == nil {
		writeError(w, "UNAVAILABLE", "channel config surface not configured", http.StatusServiceUnavailable)
		return
	}
	if !s.configEditEnabled() {
		writeConfigEditDisabled(w)
		return
	}
	id := r.PathValue("id")
	resp, err := s.buildChannelConfigResponse(r.Context(), id)
	if err != nil {
		s.writeChannelError(w, err)
		return
	}
	writeJSON(w, resp, http.StatusOK)
}

// handlePatchChannelConfig handles PATCH /api/v1/channels/{id}/config: a sparse
// `{knob: value}` body (explicit `null` unsets a knob back to inherit) applied
// through the store-canonical apply path under an `If-Match` revision guard.
//
// Order matters and mirrors the other channel writers: availability (503) →
// toggle (403) → body well-formedness (400) → If-Match present+parseable
// (428/400) → load current overrides (404 if the channel is gone) → merge →
// apply (validation 400 / conflict 409). A successful apply re-reads and returns
// the new effective config, so the caller gets the bumped revision to use as the
// next If-Match without a second round-trip.
func (s *Server) handlePatchChannelConfig(w http.ResponseWriter, r *http.Request) {
	if s.channelStore == nil || s.channelRouter == nil {
		writeError(w, "UNAVAILABLE", "channel config surface not configured", http.StatusServiceUnavailable)
		return
	}
	if !s.configEditEnabled() {
		writeConfigEditDisabled(w)
		return
	}
	if !requireJSON(w, r) {
		return
	}
	id := r.PathValue("id")

	// Decode the sparse patch as a key→raw map so an absent key (leave alone), an
	// explicit null (unset→inherit), and a value (set) stay distinguishable — the
	// tri-state a typed struct with pointer fields would collapse (absent and null
	// both decode to nil).
	var raw map[string]json.RawMessage
	if !decodeJSON(w, r, &raw) {
		return
	}

	expectedRevision, ok := parseIfMatch(w, r)
	if !ok {
		return
	}

	// Merge the patch over the channel's current stored overrides — the apply
	// path replaces the override blob wholesale, so the REST layer is responsible
	// for turning a sparse edit into the complete desired set. A 404 here (channel
	// gone) precedes any write.
	current, revision, err := s.channelStore.GetChannelConfig(r.Context(), id)
	if err != nil {
		s.writeChannelError(w, err)
		return
	}

	// ISSUE-0103: pick the merge BASE. For an already-edited channel (revision
	// > 0) the store is canonical, so the base is its stored overrides. But a
	// YAML-seeded channel sits at revision 0 with an EMPTY store blob — its
	// governance lives only on the router (seeded from config/channels.yaml at
	// boot). Merging a sparse first edit onto that empty blob would hand the
	// wholesale-replace apply path a set carrying only the edited knob, so every
	// OTHER non-default knob — most visibly the escalation chair — silently
	// reverts to the package default. So on the FIRST edit we seed the base from
	// the channel's resolved governance, making the channel store-canonical with a
	// faithful snapshot — the same kind of freeze the YAML adopt path makes via
	// ChannelConfig.toConfigOverrides (minus the not-yet-live interaction_budget
	// knob, which resolvedConfigBaseline deliberately omits — see its doc). The
	// sparse patch then layers over that baseline and nothing un-edited is dropped.
	// Skipped for an empty patch so a no-op PATCH stays a no-op rather than
	// freezing the channel.
	base := current
	if revision == 0 && len(raw) > 0 {
		base = s.resolvedConfigBaseline(r.Context(), id)
	}

	merged, err := mergeConfigPatch(base, raw)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}

	if err := s.channelRouter.ApplyChannelConfig(r.Context(), id, merged, expectedRevision, ""); err != nil {
		s.writeChannelError(w, err)
		return
	}

	resp, err := s.buildChannelConfigResponse(r.Context(), id)
	if err != nil {
		s.writeChannelError(w, err)
		return
	}
	writeJSON(w, resp, http.StatusOK)
}

// writeConfigEditDisabled is the shared 403 for the toggle-off gate, kept
// identical across the read and write handlers so an operator gets one clear
// diagnosis pointing at the knob to flip.
func writeConfigEditDisabled(w http.ResponseWriter) {
	writeError(w, "FORBIDDEN",
		"channel config editing is disabled (set panels.channel_timeline.config_edit_enabled: true in config/ui.yaml)",
		http.StatusForbidden)
}

// parseIfMatch reads the optimistic-concurrency revision off the `If-Match`
// header. The header is REQUIRED: a config write without a stated expected
// revision is a lost-update hazard, so an absent header is 428 Precondition
// Required rather than a silent unconditional write. The value is the bare
// integer revision (a quoted ETag-style `"3"` is tolerated); anything else is a
// 400. Returns the parsed revision and false (response already written) on any
// failure.
func parseIfMatch(w http.ResponseWriter, r *http.Request) (int64, bool) {
	raw := strings.TrimSpace(r.Header.Get("If-Match"))
	if raw == "" {
		writeError(w, "PRECONDITION_REQUIRED",
			"If-Match header with the current config revision is required", http.StatusPreconditionRequired)
		return 0, false
	}
	raw = strings.Trim(raw, `"`)
	rev, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		writeError(w, "BAD_REQUEST",
			"If-Match must be the integer config revision", http.StatusBadRequest)
		return 0, false
	}
	return rev, true
}

// mergeConfigPatch folds a sparse `{knob: rawJSON}` patch onto a base override
// set (the caller's choice — the channel's stored overrides normally, or its
// resolved governance baseline on a revision-0 first edit; see ISSUE-0103 in
// [Server.handlePatchChannelConfig]) and returns the COMPLETE desired override
// set for the apply path. Per knob: a JSON null clears the override (unset→inherit), any other
// value sets it, and a key absent from the patch leaves the current value
// untouched. An unrecognised key (default case) or a value of the wrong JSON
// type ([decodeKnob]) is rejected here so a typo'd knob 400s rather than
// silently doing nothing — this is where the closed-knob-set (the wire analogue
// of additionalProperties:false) is enforced. Note the decode into
// `map[string]json.RawMessage` upstream cannot do it: DisallowUnknownFields is a
// no-op on a map, so the gate is this loop, which runs AFTER the channel load
// and If-Match check (so a typo'd knob on a missing channel surfaces the 404
// first).
func mergeConfigPatch(current channels.ChannelConfigOverrides, patch map[string]json.RawMessage) (channels.ChannelConfigOverrides, error) {
	out := current // value copy; pointer fields are shared but only reassigned below
	for key, rawVal := range patch {
		isNull := isJSONNull(rawVal)
		switch key {
		case "floor_control":
			if isNull {
				out.FloorControl = nil
				continue
			}
			v, err := decodeKnob[bool](key, rawVal)
			if err != nil {
				return out, err
			}
			out.FloorControl = &v
		case "salience_max_channel_members":
			if isNull {
				out.SalienceMaxChannelMembers = nil
				continue
			}
			v, err := decodeKnob[int](key, rawVal)
			if err != nil {
				return out, err
			}
			out.SalienceMaxChannelMembers = &v
		case "interaction_budget_tokens":
			if isNull {
				out.InteractionBudgetTokens = nil
				continue
			}
			v, err := decodeKnob[int64](key, rawVal)
			if err != nil {
				return out, err
			}
			out.InteractionBudgetTokens = &v
		case "max_replies_per_participant_per_interaction":
			if isNull {
				out.MaxRepliesPerParticipantPerInteraction = nil
				continue
			}
			v, err := decodeKnob[int](key, rawVal)
			if err != nil {
				return out, err
			}
			out.MaxRepliesPerParticipantPerInteraction = &v
		case "end_vote_threshold":
			if isNull {
				out.EndVoteThreshold = nil
				continue
			}
			v, err := decodeKnob[int](key, rawVal)
			if err != nil {
				return out, err
			}
			out.EndVoteThreshold = &v
		case "end_vote_window":
			if isNull {
				out.EndVoteWindow = nil
				continue
			}
			v, err := decodeKnob[int](key, rawVal)
			if err != nil {
				return out, err
			}
			out.EndVoteWindow = &v
		case "escalation_chair_id":
			if isNull {
				out.EscalationChairID = nil
				continue
			}
			v, err := decodeKnob[string](key, rawVal)
			if err != nil {
				return out, err
			}
			out.EscalationChairID = &v
		case "interaction_idle_timeout_seconds":
			if isNull {
				out.InteractionIdleTimeoutSeconds = nil
				continue
			}
			v, err := decodeKnob[int](key, rawVal)
			if err != nil {
				return out, err
			}
			out.InteractionIdleTimeoutSeconds = &v
		default:
			return out, errors.New("unknown config knob: " + key)
		}
	}
	return out, nil
}

// isJSONNull reports whether a raw JSON value is the literal `null` (ignoring
// surrounding whitespace) — the unset-this-knob sentinel in a PATCH body.
func isJSONNull(raw json.RawMessage) bool {
	return string(bytes.TrimSpace(raw)) == "null"
}

// decodeKnob strictly unmarshals a single knob's raw JSON into its Go type,
// turning a wrong-typed value (e.g. a string where an int is expected, or a
// fractional number for an integer knob) into a 400-worthy error that names the
// knob. encoding/json already enforces this — json.Unmarshal refuses to coerce a
// fractional literal like 1.5 into an int — so the wrapper adds no strictness of
// its own; it exists only to attach the knob name to the diagnosis.
func decodeKnob[T any](key string, raw json.RawMessage) (T, error) {
	var v T
	if err := json.Unmarshal(raw, &v); err != nil {
		return v, errors.New(key + ": " + err.Error())
	}
	return v, nil
}

// buildChannelConfigResponse assembles the effective-config view: it reads the
// channel's stored overrides + revision (the provenance source of truth — a knob
// present in the blob is "channel", absent is "default") and the router's
// resolved getters (the effective value an inherited knob falls back to). A
// missing channel surfaces [channels.ErrChannelNotFound] for the caller to map
// to 404.
//
// Interaction budget is the one knob whose inherited effective value is NOT
// resolved here: it is not router-held (RFC 0050 Phase 1 Open item 4), so when
// it is inherited the response reports value null. When it IS overridden the
// stored value is echoed, so an operator still sees what they set.
//
// Keep the router-held getters below in sync with [Server.resolvedConfigBaseline]:
// the two are a matched pair (this method REPORTS each knob's provenance; the
// baseline FREEZES the same set on a first edit). A getter added here but not
// there would resolve effectively yet silently drop out of the first-edit
// baseline — the ISSUE-0103 footgun. TestChannelConfig_FirstEditFreezesDefaultsAsChannel
// pins the invariant.
func (s *Server) buildChannelConfigResponse(ctx context.Context, id string) (channelConfigResponse, error) {
	overrides, revision, err := s.channelStore.GetChannelConfig(ctx, id)
	if err != nil {
		return channelConfigResponse{}, err
	}

	floorEnabled, _, _ := s.channelRouter.FloorControlFor(id)
	salienceMax, _ := s.channelRouter.SalienceMaxChannelMembersFor(id)
	replyBudget := s.channelRouter.ReplyBudgetFor(id)
	k, wWindow := s.channelRouter.EndVoteParamsFor(id)
	chair, _ := s.channelRouter.EscalationChairFor(id)
	idleSeconds, _ := s.channelRouter.InteractionIdleTimeoutFor(id)

	resp := channelConfigResponse{
		Revision:                               revision,
		FloorControl:                           configField(floorEnabled, overrides.FloorControl != nil),
		SalienceMaxChannelMembers:              configField(salienceMax, overrides.SalienceMaxChannelMembers != nil),
		MaxRepliesPerParticipantPerInteraction: configField(replyBudget, overrides.MaxRepliesPerParticipantPerInteraction != nil),
		EndVoteThreshold:                       configField(k, overrides.EndVoteThreshold != nil),
		EndVoteWindow:                          configField(wWindow, overrides.EndVoteWindow != nil),
		EscalationChairID:                      configField(chair, overrides.EscalationChairID != nil),
		InteractionIdleTimeoutSeconds:          configField(idleSeconds, overrides.InteractionIdleTimeoutSeconds != nil),
	}

	// Interaction budget: echo the override when set, otherwise leave the effective
	// value null (deferred resolution — see the method doc).
	if overrides.InteractionBudgetTokens != nil {
		resp.InteractionBudgetTokens = configField(*overrides.InteractionBudgetTokens, true)
	} else {
		resp.InteractionBudgetTokens = configFieldResponse{Value: nil, Source: configSourceDefault}
	}
	return resp, nil
}

// resolvedConfigBaseline snapshots the channel's currently-resolved governance
// into a COMPLETE override set — the merge base ISSUE-0103 layers a first edit
// over so a sparse PATCH on a revision-0 (YAML-seeded) channel does not reset its
// un-edited knobs to the package default. It reads the same six router-held
// getters as [Server.buildChannelConfigResponse], so the snapshot is exactly the
// channel's effective governance at the moment it becomes store-canonical.
//
// The escalation chair is the one knob captured conditionally: an empty chair
// stays nil (no escalation — the opt-in default), mirroring
// [channels.ChannelConfig.toConfigOverrides] so a chair-less channel is not
// frozen with an explicit empty string that would read back as "explicitly
// cleared". It is ALSO dropped when it is no longer enforceable — see
// [Server.chairIsEnforceableMember]: a router-held chair that has drifted out of
// the channel's membership (or become an observer) is inert at dispatch, and
// seeding it back into the baseline would make [ChannelRouter.ApplyChannelConfig]
// re-run the cross-field chair-membership rule and REJECT an otherwise-unrelated
// first edit with a 400 about a chair the operator never touched. Boot replay
// and dispatch both deliberately tolerate that drift; the baseline must not
// resurrect it as a hard error. So a drifted chair is omitted (left unset on the
// edited channel — the same outcome dispatch already produces), while a valid
// chair still survives the first edit (ISSUE-0103).
//
// Interaction budget is intentionally absent: it is not router-held (RFC 0050
// Open item 4), is not behaviourally affected by the store edit (it is resolved
// on demand from YAML on the wallet path), and continues to inherit until it is
// wired — so omitting it from the snapshot loses nothing live.
func (s *Server) resolvedConfigBaseline(ctx context.Context, id string) channels.ChannelConfigOverrides {
	floorEnabled, _, _ := s.channelRouter.FloorControlFor(id)
	salienceMax, _ := s.channelRouter.SalienceMaxChannelMembersFor(id)
	replyBudget := s.channelRouter.ReplyBudgetFor(id)
	k, wWindow := s.channelRouter.EndVoteParamsFor(id)
	chair, _ := s.channelRouter.EscalationChairFor(id)
	idleSeconds, _ := s.channelRouter.InteractionIdleTimeoutFor(id)

	base := channels.ChannelConfigOverrides{
		FloorControl:                           &floorEnabled,
		SalienceMaxChannelMembers:              &salienceMax,
		MaxRepliesPerParticipantPerInteraction: &replyBudget,
		EndVoteThreshold:                       &k,
		EndVoteWindow:                          &wWindow,
		InteractionIdleTimeoutSeconds:          &idleSeconds,
	}
	if chair != "" && s.chairIsEnforceableMember(ctx, id, chair) {
		base.EscalationChairID = &chair
	}
	return base
}

// chairIsEnforceableMember reports whether `chairID` would survive
// [ChannelRouter.validateEscalationChair]'s membership rules — it is a declared
// member of the channel and is not an observer (respond: never). It mirrors the
// member/observer checks there (the floor-control check is excluded: the
// baseline sets floor control to its own resolved value, so a revision-0 chaired
// channel is floor-on by construction). Used only to decide whether a
// router-held chair should be FROZEN into the first-edit baseline; an
// unenforceable chair is dropped so a drifted chair cannot block an unrelated
// edit. A store error reading members is treated as "not enforceable" — better
// to drop the chair and let the edit proceed than to block it on a transient
// fault (the apply path that follows would surface a real outage anyway).
func (s *Server) chairIsEnforceableMember(ctx context.Context, id, chairID string) bool {
	members, err := s.channelStore.GetMembers(ctx, id)
	if err != nil {
		return false
	}
	for i := range members {
		if members[i].ParticipantID == chairID {
			return members[i].RespondPolicy.Normalize() != channels.RespondNever
		}
	}
	return false
}

// configField builds a knob's response cell: the effective value plus its
// provenance label derived from whether an explicit override is present.
func configField(value any, overridden bool) configFieldResponse {
	source := configSourceDefault
	if overridden {
		source = configSourceChannel
	}
	return configFieldResponse{Value: value, Source: source}
}
