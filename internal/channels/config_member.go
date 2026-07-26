package channels

// config_member.go holds [MemberConfig] — the per-member declaration unit of
// `config/channels.yaml` — and its dual-form YAML unmarshalling (string
// shorthand vs `{id, respond, threshold}` object, RFC 0011 §A). Split out of
// config.go when RFC 0037 PR 1's classification fields pushed that file past
// the 500-line review cap (the config_validate.go precedent): the member
// shape and its Tier B salience-signal derivation change on the RFC 0030
// membership cadence, not the fleet-knob cadence the rest of config.go moves
// on.

import (
	"fmt"

	"gopkg.in/yaml.v3"
)

// MemberConfig is a `(participant_id, respond_policy)` pair declared in
// config. Loaders accept either the shorthand string form or the explicit
// object form per the schema.
type MemberConfig struct {
	ID            string
	RespondPolicy RespondPolicy
	// Threshold is the per-disposition salience `threshold` for the RFC 0030
	// Tier B relevance bid (v0.3.8). It is a `*float64` tri-state on purpose:
	//   - nil   → unset → bias-to-silence (the conservative default)
	//   - &0..1 → explicit salience bar the bid must clear to reach the turn
	// A `chair` member with no explicit value picks up [DefaultChairThreshold]
	// at load (see [MemberConfig.UnmarshalYAML]).
	//
	// As of PR 2b the threshold round-trips end-to-end: [ReconcileConfig]
	// carries it (and [SalienceGated]) onto the store-side [Member], the
	// `memberships.threshold` column persists it, and the
	// `ChannelMessageEvent.threshold` wire field delivers it to the agent-side
	// bid. The PR-1 persistence/wire gap is closed.
	Threshold *float64
	// SalienceGated marks this member as a salience-bid participant (RFC 0030
	// Tier B, v0.3.8). Derived at load by [ResolveSalienceSignal] from the
	// *declared* disposition (before normalization collapses `participant`/
	// `chair` to the legacy `always` wire value) together with any explicit
	// `threshold`: the participant vocabulary opts in, as does a legacy `always`
	// that carries an explicit threshold. Deriving it before normalization is
	// what lets the bid-ness survive the collapse that would otherwise make a
	// `participant` indistinguishable from a bare legacy `always`. Carried by
	// [ReconcileConfig] onto the store-side [Member.SalienceGated].
	SalienceGated bool
}

// UnmarshalYAML accepts either a string shorthand or an explicit
// `{id, respond}` object form (RFC 0011 §A).
func (m *MemberConfig) UnmarshalYAML(value *yaml.Node) error {
	switch value.Kind {
	case yaml.ScalarNode:
		var id string
		if err := value.Decode(&id); err != nil {
			return err
		}
		m.ID = id
		m.RespondPolicy = RespondWhenMentioned
		return nil
	case yaml.MappingNode:
		var raw struct {
			ID        string   `yaml:"id"`
			Respond   string   `yaml:"respond"`
			Threshold *float64 `yaml:"threshold"`
		}
		if err := value.Decode(&raw); err != nil {
			return err
		}
		m.ID = raw.ID
		if raw.Respond == "" {
			m.RespondPolicy = RespondWhenMentioned
			m.Threshold = raw.Threshold
		} else {
			// Normalize the disposition vocabulary (RFC 0030 relevance
			// amendment) to the canonical legacy triple at the load
			// boundary, so every downstream reader sees only the legacy
			// values. An unknown value passes through unchanged and is
			// rejected by Validate via RespondPolicy.Valid.
			disposition := RespondPolicy(raw.Respond)
			// Derive the Tier B signals from the *declared* disposition before
			// it is normalized: `participant`/`chair` opt into the bid, a chair
			// (or an `always` + explicit threshold) picks up the right bar.
			// This is the non-validating half of [ResolveMemberPolicy] (which
			// the store write paths use): rejection is deferred to Validate so
			// the error carries the channel/member index (RFC 0030 Tier B).
			m.SalienceGated, m.Threshold = ResolveSalienceSignal(disposition, raw.Threshold)
			m.RespondPolicy = disposition.Normalize()
		}
		return nil
	default:
		return fmt.Errorf("channels: members entry must be a string or {id, respond} object (got kind %d)", value.Kind)
	}
}
