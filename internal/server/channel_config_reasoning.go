// RFC 0051 Phase 3a (PR 4) — the server-side helpers for the NESTED `reasoning`
// knob on the RFC 0050 config surface. Split out of channel_config_handlers.go
// (near the 500-line cap) because reasoning is the first nested block and so needs
// a sub-key merge the flat knobs do not: a `reasoning` PATCH value is a JSON object
// whose own sub-keys (mode/model/depth/revise) carry the set / null-unset / absent
// tri-state, one level down from the top-level knob merge.
package server

import (
	"encoding/json"
	"errors"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// mergeReasoningPatch folds a `reasoning` PATCH value onto the base reasoning
// override. The whole value is tri-state like a top-level knob — a JSON null
// clears the entire block (unset → inherit), an object merges its sub-keys, and
// (handled by the caller) an absent `reasoning` key leaves the block untouched.
// Within the object each sub-key is itself tri-state: null clears that sub-knob,
// a value sets it, an absent sub-key leaves it. An unrecognised sub-key or a
// wrong-typed value is rejected (the closed-sub-knob-set gate, the nested analogue
// of mergeConfigPatch's default case). A block that ends all-cleared collapses
// back to nil so it reads identically to never-set (no literal `{}` in the blob).
func mergeReasoningPatch(base *channels.ReasoningOverrides, raw json.RawMessage) (*channels.ReasoningOverrides, error) {
	if isJSONNull(raw) {
		return nil, nil
	}
	var sub map[string]json.RawMessage
	if err := json.Unmarshal(raw, &sub); err != nil {
		return nil, errors.New("reasoning: " + err.Error())
	}

	out := channels.ReasoningOverrides{}
	if base != nil {
		out = *base
	}
	for key, rawVal := range sub {
		isNull := isJSONNull(rawVal)
		switch key {
		case "mode":
			if isNull {
				out.Mode = nil
				continue
			}
			v, err := decodeKnob[string]("reasoning.mode", rawVal)
			if err != nil {
				return nil, err
			}
			out.Mode = &v
		case "model":
			if isNull {
				out.Model = nil
				continue
			}
			v, err := decodeKnob[string]("reasoning.model", rawVal)
			if err != nil {
				return nil, err
			}
			out.Model = &v
		case "depth":
			if isNull {
				out.Depth = nil
				continue
			}
			v, err := decodeKnob[string]("reasoning.depth", rawVal)
			if err != nil {
				return nil, err
			}
			out.Depth = &v
		case "revise":
			if isNull {
				out.Revise = nil
				continue
			}
			v, err := decodeKnob[int]("reasoning.revise", rawVal)
			if err != nil {
				return nil, err
			}
			out.Revise = &v
		default:
			return nil, errors.New("unknown reasoning knob: " + key)
		}
	}
	if out.IsEmpty() {
		return nil, nil
	}
	return &out, nil
}

// reasoningResponse builds the nested `reasoning` view for the config GET/PATCH
// payload: each sub-knob's effective value (read off the router-resolved rung) +
// its provenance (whether that specific sub-knob is explicitly overridden). It is
// the reasoning analogue of the per-knob [configField] calls in
// buildChannelConfigResponse.
func reasoningResponse(rc channels.ReasoningConfig, ov *channels.ReasoningOverrides) reasoningConfigResponse {
	return reasoningConfigResponse{
		Mode:   configField(rc.Mode, ov != nil && ov.Mode != nil),
		Model:  configField(rc.Model, ov != nil && ov.Model != nil),
		Depth:  configField(rc.Depth, ov != nil && ov.Depth != nil),
		Revise: configField(rc.Revise, ov != nil && ov.Revise != nil),
	}
}

// reasoningBaseline is the reasoning leg of the ISSUE-0103 first-edit freeze
// ([Server.resolvedConfigBaseline]). Like the escalation chair — and UNLIKE the
// unconditionally-frozen flat knobs — it is CONDITIONAL: it freezes the resolved
// rung into the baseline only when that rung is NON-default (a YAML-declared rung
// worth preserving across an unrelated sparse first edit), and returns nil for the
// default rung so the channel keeps inheriting it.
//
// The conditional is not just tidiness. RFC 0051 PR 6 flips the governed-channel
// DEFAULT off→bid; an explicit "off" override is preserved across that flip
// (intentional kill switch), so freezing a default-off channel to an explicit
// "off" here — merely because an operator edited some unrelated knob first — would
// silently opt it out of the flip it never meant to decline. Leaving the default
// rung as inherit (nil) keeps such a channel responsive to the future default.
func reasoningBaseline(rc channels.ReasoningConfig) *channels.ReasoningOverrides {
	if rc == channels.DefaultReasoningConfig() {
		return nil // default rung — inherit, not freeze (stays responsive to the PR 6 flip)
	}
	mode, model, depth, revise := rc.Mode, rc.Model, rc.Depth, rc.Revise
	return &channels.ReasoningOverrides{
		Mode:   &mode,
		Model:  &model,
		Depth:  &depth,
		Revise: &revise,
	}
}
