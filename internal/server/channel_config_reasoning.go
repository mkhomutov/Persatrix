// RFC 0051 Phase 3a (PR 4) — the server-side helpers for the NESTED `reasoning`
// knob on the RFC 0050 config surface. Split out of channel_config_handlers.go
// (near the 500-line cap) because reasoning is the first nested block and so needs
// a sub-key merge the flat knobs do not: a `reasoning` PATCH value is a JSON object
// whose own sub-keys (mode/model/depth/revise) carry the set / null-unset / absent
// tri-state, one level down from the top-level knob merge.
package server

import (
	"context"
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
// unconditionally-frozen flat knobs — it is CONDITIONAL on two counts, both
// delegated to [channels.ReasoningConfig.FreezeOverrides]:
//
//  1. PER-SUB-KNOB / GOVERNANCE-AWARE FREEZE. Only a sub-knob differing from its
//     (governance-dependent) default is frozen; a default sub-knob stays inherit.
//     Post the PR 6 go-live flip the governed default is `bid`, so on a governed
//     channel `mode: bid` stays inherit while an explicit `mode: off` (the kill
//     switch) IS frozen — that is what preserves the kill switch across the flip.
//     A channel non-default ONLY because of a sibling like `model: quality` keeps
//     `mode` inherit and stays responsive to the `bid` default — an operator who
//     touched only `model` never declined the flip. The same `governed` read drives
//     [channels.ReasoningConfig.FreezeOverrides] and the drop below, so they agree.
//
//  2. GOVERNANCE-DRIFT DROP (the reasoning analogue of the escalation chair's
//     [Server.chairIsEnforceableMember]). On an UNgoverned channel a frozen non-off
//     `mode` (drift — the salience-gated member left) is the one sub-knob the apply
//     path cross-validates against live membership
//     ([ChannelRouter.validateReasoningGoverned]); the rung is already inert at
//     dispatch, so freezing it would let that cross-field rule REJECT an unrelated
//     first edit naming a knob the operator never touched. We drop just the inert
//     mode (any committed model/depth/revise still freeze) — the same "tolerate the
//     drift, don't resurrect it as a hard error" posture boot replay and dispatch
//     already take. NOTE this only covers the FIRST edit: once the channel is
//     store-canonical (revision > 0) the merge base is the stored blob, so a
//     persisted non-off mode whose member later leaves will still block a subsequent
//     edit until cleared — the same accepted limitation the chair has.
func (s *Server) reasoningBaseline(ctx context.Context, id string) *channels.ReasoningOverrides {
	governed := s.channelHasSalienceGatedMember(ctx, id)
	froze := s.channelRouter.ReasoningFor(id).FreezeOverrides(governed)
	if froze != nil && froze.Mode != nil && !governed {
		froze.Mode = nil // drifted-ungoverned: drop the inert mode (mirror the chair)
		if froze.IsEmpty() {
			return nil
		}
	}
	return froze
}

// channelHasSalienceGatedMember reports whether the channel currently has at least
// one salience-gated (open-floor participant/chair) member — the membership signal
// the RFC 0051 §G cross-field rule requires for a non-off reasoning mode. It is the
// reasoning analogue of [Server.chairIsEnforceableMember] and shares its posture: a
// store error reading members is treated as "not governed" so the first-edit
// baseline drops the inert mode and the edit proceeds, rather than blocking it on a
// transient fault (the apply path that follows surfaces any real outage anyway). The
// "any gated member?" test is shared with the router-side callers via
// [channels.AnySalienceGated]; only the GetMembers + error posture is local.
func (s *Server) channelHasSalienceGatedMember(ctx context.Context, id string) bool {
	members, err := s.channelStore.GetMembers(ctx, id)
	if err != nil {
		return false
	}
	return channels.AnySalienceGated(members)
}
