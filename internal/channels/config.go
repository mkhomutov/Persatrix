package channels

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"math"
	"os"

	"gopkg.in/yaml.v3"
)

// Config is the parsed shape of `config/channels.yaml` (RFC 0011 §A).
//
// Validation against `schemas/channel.schema.json` runs separately via
// `make validate` — the loader's job is to enforce the loud-failure rules
// from §B (config-vs-store coexistence) and produce a deterministic
// in-memory shape ready for either startup seeding or REST-routed
// reconciliation in PR 2.
type Config struct {
	MaxChannels int             `yaml:"max_channels"`
	Channels    []ChannelConfig `yaml:"channels"`
	// MaxCascadeDepth overrides the orchestrator-side cascade-depth cap
	// from [RFC 0011 amendment 'Cascade-depth wire propagation'].
	// Optional — zero or absent uses the router's
	// [defaults.DefaultMaxCascadeDepth] default. Must stay aligned with
	// the Python `EventDispatcher.max_cascade_depth` value
	// ([agents/dispatch.py:43]); the two are one conceptual cap with
	// two enforcement points (primary + defense-in-depth).
	//
	// [RFC 0011 amendment 'Cascade-depth wire propagation']: ../../docs/rfcs/0011-amendment-cascade-depth-wire-propagation.md
	MaxCascadeDepth int `yaml:"max_cascade_depth"`
}

// DefaultFloorTurnTimeoutSeconds is the per-speaker turn timeout for floor
// control when a declared channel omits `floor_turn_timeout_seconds` (RFC
// 0030 amendment decision D2). It is distinct from the 5s
// `channelFanoutPerRecipientTimeout` — a floor turn waits for the speaker to
// compose and publish a full reply, which is LLM-latency-bound, so 45s is
// the generous-but-bounded default. PR 1 only parses/normalizes the knob;
// PR 2's serialized loop consumes it.
const DefaultFloorTurnTimeoutSeconds = 45

// DefaultSalienceMaxChannelMembers is the channel-size cap (RFC 0030 amendment
// OQ #4 / TB6) applied when a declared channel omits
// `salience_max_channel_members`. Above the cap the agent-side seam skips the
// salience bid entirely and falls back to `addressed`-only, so a cheap bid × N
// members stays bounded on a large channel. Kept in lock-step with the Python
// `DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS` (agents/salience_bid.py) — the value
// rides the wire, but a zero/absent field falls back to the Python default, so
// the two must agree. Zero/absent normalizes to this value at load.
const DefaultSalienceMaxChannelMembers = 20

// DefaultChairThreshold is the low salience `threshold` applied to a `chair`
// member when its config omits an explicit value (RFC 0030 Tier B, v0.3.8).
// A low bar means the chair clears the cheap relevance bid readily and keeps
// an open-floor discussion moving — the facilitator behaviour. On the wire a
// chair is just a `participant`, so this low default is its whole identity
// (see [RespondChair]).
//
// Deliberately low-but-nonzero — both extremes are wrong for a facilitator,
// and the two are distinct values under the nil-vs-&0.0 tri-state (see
// [MemberConfig.Threshold]):
//   - nil (the plain-`participant` default) reads as unset → bias-to-silence:
//     the conservative "stay quiet on ambiguous open-floor traffic" stance,
//     the opposite of a facilitator.
//   - &0.0 is a real, explicit bar of zero that EVERY salience score clears,
//     so the chair would pile onto literally every message and defeat the
//     point of having a bid at all.
//
// 0.15 sits just above zero: the chair speaks readily but still respects a
// minimal salience floor. The exact value is calibration-post-soak per RFC
// 0030 amendment OQ #3. PR 1 only parses/normalizes the knob; the Tier B bid
// (a later PR) is the first reader.
const DefaultChairThreshold = 0.15

// ChannelConfig is a single declared group channel.
type ChannelConfig struct {
	Name        string         `yaml:"name"`
	Description string         `yaml:"description"`
	Members     []MemberConfig `yaml:"members"`
	// FloorControl opts this channel into RFC 0030 Layer 2.5 speaker
	// serialization: candidate responders take the floor one at a time,
	// each reading the prior speaker's reply, instead of replying
	// concurrently and mutually-blind.
	//
	// It is a `*bool` tri-state on purpose. Every channel declared in
	// `config/channels.yaml` is a group channel, and PR 3 makes the
	// resolved default ON for group channels — but operators must still be
	// able to opt a specific channel back OUT. A plain `bool` cannot
	// express that: its zero value (`false`) is indistinguishable from an
	// explicit `floor_control: false`, so a group default of ON would make
	// opt-out impossible. With the pointer:
	//   - nil   → operator said nothing → resolver applies the group default (ON)
	//   - &true → explicit opt-in
	//   - &false→ explicit opt-out (honoured over the group default)
	// Read through [ChannelConfig.FloorControlEnabled], never directly.
	FloorControl *bool `yaml:"floor_control"`
	// FloorTurnTimeoutSeconds caps how long the floor loop waits for a
	// single speaker's reply before advancing to the next responder
	// (amendment D2). Zero/absent normalizes to
	// [DefaultFloorTurnTimeoutSeconds] at load; negative is rejected.
	FloorTurnTimeoutSeconds int `yaml:"floor_turn_timeout_seconds"`
	// SalienceMaxChannelMembers is the RFC 0030 Tier B (v0.3.8) channel-size cap
	// (TB6 / amendment OQ #4): above this many candidate responders the
	// agent-side seam skips the salience bid and falls back to `addressed`-
	// only, keeping a cheap bid × N members bounded on a large channel.
	// Zero/absent normalizes to [DefaultSalienceMaxChannelMembers] at load;
	// negative is rejected. The resolved value rides the
	// `ChannelMessageEvent.salience_max_channel_members` wire field; the router
	// stamps it onto the dispatch envelope at fanout time.
	SalienceMaxChannelMembers int `yaml:"salience_max_channel_members"`
}

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
	// Tier B, v0.3.8). Set at load from the *declared* disposition (before
	// normalization collapses `participant`/`chair` to the legacy `always`
	// wire value), so the bid-ness survives the normalization that would
	// otherwise make a `participant` indistinguishable from a legacy `always`.
	// Carried by [ReconcileConfig] onto the store-side [Member.SalienceGated].
	// See [ResolveSalienceSignal] for the derivation rule.
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
			// ResolveSalienceSignal is the single choke point the store write
			// paths share, so the mapping lives in one place (RFC 0030 Tier B).
			m.SalienceGated, m.Threshold = ResolveSalienceSignal(disposition, raw.Threshold)
			m.RespondPolicy = disposition.Normalize()
		}
		return nil
	default:
		return fmt.Errorf("channels: members entry must be a string or {id, respond} object (got kind %d)", value.Kind)
	}
}

// LoadConfig reads and validates `config/channels.yaml`. Returns a non-nil
// [Config] on success. An empty / commented-out file produces a [Config]
// with `MaxChannels = DefaultMaxChannels` and no channels — startup
// continues with no declared channels rather than failing.
func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("channels: read %s: %w", path, err)
	}
	cfg := &Config{}
	dec := yaml.NewDecoder(bytes.NewReader(data))
	dec.KnownFields(true) // surface legacy `type` / `history_visible` etc. as parse errors
	if err := dec.Decode(cfg); err != nil {
		if !errors.Is(err, io.EOF) {
			// io.EOF means the file is empty / fully commented out — treat
			// as the empty config rather than a parse error so the
			// freshly-rewritten `config/channels.yaml` template loads.
			return nil, fmt.Errorf("channels: parse %s: %w", path, err)
		}
	}
	if cfg.MaxChannels <= 0 {
		cfg.MaxChannels = DefaultMaxChannels
	}
	// Normalize the per-channel floor-turn timeout: zero/absent means "use
	// the default" (mirroring the max_cascade_depth sentinel). Negative
	// values are left as-is so Validate rejects them loudly rather than
	// silently falling back. PR 1 keeps the knob inert; the normalization
	// just guarantees PR 2's loop reads a populated value.
	for i := range cfg.Channels {
		if cfg.Channels[i].FloorTurnTimeoutSeconds == 0 {
			cfg.Channels[i].FloorTurnTimeoutSeconds = DefaultFloorTurnTimeoutSeconds
		}
		// Same zero/absent → default sentinel for the RFC 0030 Tier B
		// channel-size cap, so the dispatcher always has a populated value to
		// stamp on the wire. Negative is left as-is for Validate to reject.
		if cfg.Channels[i].SalienceMaxChannelMembers == 0 {
			cfg.Channels[i].SalienceMaxChannelMembers = DefaultSalienceMaxChannelMembers
		}
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return cfg, nil
}

// Validate enforces the cross-field invariants the JSON Schema cannot
// express alone:
//   - participant ids satisfy `validateParticipantID` (no `:`, no whitespace)
//   - declared `members` entries are unique within a channel
//   - declared channel names are unique across the file
//   - the declared channel count does not exceed `MaxChannels`
//   - every membership respond policy is in the canonical set
//   - a per-disposition `threshold`, when present, is a finite value in
//     `[0, 1]` and rides only on an open-floor disposition (the salience
//     bid never runs on a non-open-floor member, so a threshold there is a
//     silent no-op — RFC 0030 Tier B)
//   - `max_cascade_depth` is non-negative (zero is the loader-default
//     sentinel; negative is rejected — PR #319 deep review finding 5.2)
func (c *Config) Validate() error {
	// MaxCascadeDepth: reject negative early so an operator typo surfaces
	// as a loader error rather than as a silent fall-back to the default.
	// The JSON schema's `minimum: 0` catches this at `make validate` time;
	// this Go-side check is the belt-and-suspenders for operators who
	// skipped that step. Zero is intentionally accepted — it is the
	// loader-default sentinel honored by [ChannelRouter.SetMaxCascadeDepth].
	if c.MaxCascadeDepth < 0 {
		return fmt.Errorf("%w: %d (must be >= 0)",
			ErrInvalidMaxCascadeDepth, c.MaxCascadeDepth)
	}
	if len(c.Channels) > c.MaxChannels {
		return fmt.Errorf("%w: declared=%d cap=%d",
			ErrChannelCapExceeded, len(c.Channels), c.MaxChannels)
	}
	seenName := make(map[string]bool, len(c.Channels))
	for i, ch := range c.Channels {
		if ch.Name == "" {
			return fmt.Errorf("channels[%d]: name is required", i)
		}
		// Mirror `schemas/channel.schema.json` → `definitions.channel.name.pattern`
		// so a value that passes the loader also passes `make validate`. See
		// channelNamePattern (channels.go) and PR #231 review Should-Fix #6.
		if !channelNamePattern.MatchString(ch.Name) {
			return fmt.Errorf("channels[%d]: name %q does not match %s",
				i, ch.Name, channelNamePattern.String())
		}
		if seenName[ch.Name] {
			return fmt.Errorf("channels[%d]: duplicate name %q", i, ch.Name)
		}
		seenName[ch.Name] = true

		// Reject a negative floor-turn timeout (the schema's `minimum: 1`
		// catches it at `make validate`; this is the belt-and-suspenders
		// for operators who skipped that step). Zero never reaches here —
		// LoadConfig normalizes it to the default before Validate runs.
		if ch.FloorTurnTimeoutSeconds < 0 {
			return fmt.Errorf("channels[%d=%s]: %w: %d (must be >= 1)",
				i, ch.Name, ErrInvalidFloorTurnTimeout, ch.FloorTurnTimeoutSeconds)
		}

		// Reject a negative Tier B channel-size cap (the schema's `minimum: 1`
		// catches it at `make validate`; this is the belt-and-suspenders for
		// operators who skipped that step). Zero never reaches here — LoadConfig
		// normalizes it to the default before Validate runs.
		if ch.SalienceMaxChannelMembers < 0 {
			return fmt.Errorf("channels[%d=%s]: %w: %d (must be >= 1)",
				i, ch.Name, ErrInvalidSalienceMaxChannelMembers, ch.SalienceMaxChannelMembers)
		}

		if len(ch.Members) == 0 {
			return fmt.Errorf("channels[%d=%s]: at least one member required", i, ch.Name)
		}
		seenMember := make(map[string]bool, len(ch.Members))
		for j, m := range ch.Members {
			if err := validateParticipantID(m.ID); err != nil {
				return fmt.Errorf("channels[%d=%s].members[%d]: %w", i, ch.Name, j, err)
			}
			if seenMember[m.ID] {
				return fmt.Errorf("channels[%d=%s].members[%d]: duplicate id %q",
					i, ch.Name, j, m.ID)
			}
			seenMember[m.ID] = true
			if !m.RespondPolicy.Valid() {
				return fmt.Errorf("channels[%d=%s].members[%d=%s]: %w: %q",
					i, ch.Name, j, m.ID, ErrInvalidRespondPolicy, m.RespondPolicy)
			}
			// Validate the salience threshold (RFC 0030 Tier B). Absent
			// (nil) is the unset bias-to-silence default and is never an
			// error.
			if m.Threshold != nil {
				// Reject a non-finite or out-of-range bound. The schema's
				// `minimum: 0`/`maximum: 1` catches an out-of-range value at
				// `make validate`; this is the belt-and-suspenders for an
				// operator who skipped that step. The explicit `IsNaN` guard
				// closes a gap neither bound catches: `.nan` slips past a bare
				// `< 0 || > 1` comparison (every comparison against NaN is
				// false), and would otherwise reach the bid as a salience bar
				// that no score can ever clear. (`±Inf` is already caught by
				// the range bound.)
				if math.IsNaN(*m.Threshold) || *m.Threshold < 0.0 || *m.Threshold > 1.0 {
					return fmt.Errorf("channels[%d=%s].members[%d=%s]: %w: %v (must be a finite value in [0, 1])",
						i, ch.Name, j, m.ID, ErrInvalidThreshold, *m.Threshold)
				}
				// Reject a threshold on a disposition that runs no open-floor
				// bid. After normalization, only open-floor speakers
				// (participant/chair/legacy always) carry RespondAlways; a
				// threshold on when_mentioned/never is a silent no-op. This is
				// a cross-field invariant the JSON schema cannot express, so
				// it has no `make validate` mirror — the loader is the sole
				// enforcement point.
				if m.RespondPolicy != RespondAlways {
					return fmt.Errorf("channels[%d=%s].members[%d=%s]: %w: %q carries threshold %v but only an open-floor disposition (participant/chair/always) runs the salience bid",
						i, ch.Name, j, m.ID, ErrThresholdNotApplicable, m.RespondPolicy, *m.Threshold)
				}
			}
		}
	}
	return nil
}

// CanonicalID returns the canonical address for this declared channel
// (`group:<name>`). The store-side `Channel.ID` PK uses the same value.
func (cc ChannelConfig) CanonicalID() string {
	return "group:" + cc.Name
}

// FloorControlEnabled resolves the RFC 0030 Layer 2.5 floor-control flag for
// this declared channel (PR 3 — the behaviour flip). Every channel declared
// in `config/channels.yaml` is a group channel, so the resolved default is
// ON: serializing concurrent responders into a deterministic, mutually-aware
// speaker round is the desired behaviour for multi-persona group channels.
// An explicit `floor_control: false` opts a channel back out and is honoured
// over the default (see the [ChannelConfig.FloorControl] tri-state doc).
//
// Floor control is a no-op below two candidate responders ([ChannelRouter.fanout]),
// so a "DM-shaped" group (single responder) resolving ON costs nothing — the
// concurrent path runs unchanged. The orchestrator feeds this value into
// [ChannelRouter.SetFloorControl] at startup ([cmd/orchestrator/channels.go]).
func (cc ChannelConfig) FloorControlEnabled() bool {
	if cc.FloorControl == nil {
		return true // group default ON (PR 3)
	}
	return *cc.FloorControl
}
