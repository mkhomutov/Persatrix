// Channel governance-config PATCH merge — RFC 0050 Phase 1 PR 4.
//
// This file holds the sparse-PATCH → complete-override-set merge behind
// [Server.handlePatchChannelConfig]: the per-knob tri-state fold
// (absent = leave, null = unset→inherit, value = set) and the closed-knob-set
// gate. Split out of channel_config_handlers.go when the ISSUE-0114
// per-channel cascade-depth knob (v0.3.13) pushed that file past the 500-line
// review cap — the HTTP orchestration stays there, the pure merge lives here.
package server

import (
	"bytes"
	"encoding/json"
	"errors"

	"github.com/mkhomutov/persatrix/internal/channels"
)

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
		case "max_cascade_depth":
			// ISSUE-0114 (v0.3.13): the per-channel Layer 0 cascade-depth cap —
			// the productive-discussion length knob. Scalar like its siblings; an
			// above-fleet value is accepted here and warned at the router stamp
			// (the setter's warn-don't-reject posture).
			if isNull {
				out.MaxCascadeDepth = nil
				continue
			}
			v, err := decodeKnob[int](key, rawVal)
			if err != nil {
				return out, err
			}
			out.MaxCascadeDepth = &v
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
		case "reasoning":
			// The first NESTED knob: its value is a JSON object merged sub-key by
			// sub-key (mergeReasoningPatch), not a scalar. A null clears the block.
			merged, err := mergeReasoningPatch(out.Reasoning, rawVal)
			if err != nil {
				return out, err
			}
			out.Reasoning = merged
		case "autonomous":
			// RFC 0052 (v0.3.11): the second NESTED knob — a JSON object merged
			// sub-key by sub-key (mergeAutonomousPatch). A null clears the block.
			merged, err := mergeAutonomousPatch(out.Autonomous, rawVal)
			if err != nil {
				return out, err
			}
			out.Autonomous = merged
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
