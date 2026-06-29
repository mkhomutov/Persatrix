// RFC 0052 Phase 1a (PR 1) — the server-side helpers for the NESTED `autonomous`
// knob on the RFC 0050 config surface. Split out of channel_config_handlers.go
// (near the 500-line cap), mirroring channel_config_reasoning.go: the autonomous
// block is a JSON object whose own sub-keys carry the set / null-unset / absent
// tri-state, one level down from the top-level knob merge.
package server

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// mergeAutonomousPatch folds an `autonomous` PATCH value onto the base autonomous
// override. The whole value is tri-state like a top-level knob — a JSON null clears
// the entire block (unset → inherit), an object merges its sub-keys, and (handled
// by the caller) an absent `autonomous` key leaves the block untouched. Within the
// object each sub-key is itself tri-state: null clears that sub-knob, a value sets
// it, an absent sub-key leaves it. An unrecognised sub-key or a wrong-typed value
// is rejected (the closed-sub-knob-set gate, the nested analogue of
// mergeConfigPatch's default case). A block that ends all-cleared collapses back to
// nil so it reads identically to never-set (no literal `{}` in the blob).
func mergeAutonomousPatch(base *channels.AutonomousOverrides, raw json.RawMessage) (*channels.AutonomousOverrides, error) {
	if isJSONNull(raw) {
		return nil, nil
	}
	var sub map[string]json.RawMessage
	if err := json.Unmarshal(raw, &sub); err != nil {
		return nil, errors.New("autonomous: " + err.Error())
	}

	out := channels.AutonomousOverrides{}
	if base != nil {
		out = *base
	}
	for key, rawVal := range sub {
		isNull := isJSONNull(rawVal)
		switch key {
		case "enabled":
			if isNull {
				out.Enabled = nil
				continue
			}
			v, err := decodeKnob[bool]("autonomous.enabled", rawVal)
			if err != nil {
				return nil, err
			}
			out.Enabled = &v
		case "topic":
			if isNull {
				out.Topic = nil
				continue
			}
			v, err := decodeKnob[string]("autonomous.topic", rawVal)
			if err != nil {
				return nil, err
			}
			out.Topic = &v
		case "agenda":
			if isNull {
				out.Agenda = nil
				continue
			}
			v, err := decodeKnob[[]string]("autonomous.agenda", rawVal)
			if err != nil {
				return nil, err
			}
			out.Agenda = &v
		case "convener":
			if isNull {
				out.Convener = nil
				continue
			}
			v, err := decodeKnob[string]("autonomous.convener", rawVal)
			if err != nil {
				return nil, err
			}
			out.Convener = &v
		case "goal":
			if isNull {
				out.Goal = nil
				continue
			}
			v, err := decodeKnob[string]("autonomous.goal", rawVal)
			if err != nil {
				return nil, err
			}
			out.Goal = &v
		case "max_rounds":
			if isNull {
				out.MaxRounds = nil
				continue
			}
			v, err := decodeKnob[int]("autonomous.max_rounds", rawVal)
			if err != nil {
				return nil, err
			}
			out.MaxRounds = &v
		default:
			return nil, errors.New("unknown autonomous knob: " + key)
		}
	}
	if out.IsEmpty() {
		return nil, nil
	}
	return &out, nil
}

// autonomousResponse builds the nested `autonomous` view for the config GET/PATCH
// payload: each sub-knob's effective value (read off the router-resolved rung) +
// its provenance (whether that specific sub-knob is explicitly overridden). The
// autonomous analogue of [reasoningResponse].
func autonomousResponse(a channels.AutonomousConfig, ov *channels.AutonomousOverrides) autonomousConfigResponse {
	return autonomousConfigResponse{
		Enabled:   configField(a.Enabled, ov != nil && ov.Enabled != nil),
		Topic:     configField(a.Topic, ov != nil && ov.Topic != nil),
		Agenda:    configField(agendaValue(a.Agenda), ov != nil && ov.Agenda != nil),
		Convener:  configField(a.Convener, ov != nil && ov.Convener != nil),
		Goal:      configField(a.Goal, ov != nil && ov.Goal != nil),
		MaxRounds: configField(a.MaxRounds, ov != nil && ov.MaxRounds != nil),
	}
}

// agendaValue normalizes a nil agenda to an empty slice so the JSON cell reads
// `[]` rather than `null` — an operator reads "no agenda items", not "unset".
func agendaValue(agenda []string) []string {
	if agenda == nil {
		return []string{}
	}
	return agenda
}

// autonomousBaseline is the autonomous leg of the ISSUE-0103 first-edit freeze
// ([Server.resolvedConfigBaseline]). CONDITIONAL like the escalation chair and the
// reasoning rung (not unconditional like the flat knobs), delegated to
// [channels.AutonomousConfig.FreezeOverrides]: only a non-default (armed or
// otherwise customized) rung is frozen; a disabled default stays inherit.
//
// It carries the same governance-drift drop as the chair / reasoning: if the
// frozen rung is ARMED but its convener has drifted out of the channel's
// membership, freezing it would let the convener-membership cross-field rule
// ([ChannelRouter.validateAutonomousConvener]) REJECT an unrelated first edit
// naming a knob the operator never touched. The armed rung is already un-convenable
// at dispatch (a non-member convener cannot author the opening turn), so the
// baseline drops the whole block — the "tolerate the drift, don't resurrect it as a
// hard error" posture boot replay and the other conditional knobs already take.
func (s *Server) autonomousBaseline(ctx context.Context, id string) *channels.AutonomousOverrides {
	froze := s.channelRouter.AutonomousFor(id).FreezeOverrides()
	if froze == nil {
		return nil
	}
	if froze.Enabled != nil && *froze.Enabled && froze.Convener != nil &&
		!s.channelHasMember(ctx, id, *froze.Convener) {
		return nil // drifted-convener: drop the inert armed block (mirror the chair)
	}
	return froze
}

// channelHasMember reports whether `participantID` is a declared member of the
// channel — the membership signal the OQ #1 convener rule needs. A store error
// reading members is treated as "not a member" so a drifted/unreadable convener is
// dropped from the first-edit baseline and the edit proceeds, rather than blocking
// it on a transient fault (the apply path that follows surfaces any real outage).
func (s *Server) channelHasMember(ctx context.Context, id, participantID string) bool {
	members, err := s.channelStore.GetMembers(ctx, id)
	if err != nil {
		return false
	}
	for i := range members {
		if members[i].ParticipantID == participantID {
			return true
		}
	}
	return false
}
