package channels

import (
	"bytes"
	"errors"
	"fmt"
	"io"
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

// ChannelConfig is a single declared group channel.
type ChannelConfig struct {
	Name        string         `yaml:"name"`
	Description string         `yaml:"description"`
	Members     []MemberConfig `yaml:"members"`
}

// MemberConfig is a `(participant_id, respond_policy)` pair declared in
// config. Loaders accept either the shorthand string form or the explicit
// object form per the schema.
type MemberConfig struct {
	ID            string
	RespondPolicy RespondPolicy
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
			ID      string `yaml:"id"`
			Respond string `yaml:"respond"`
		}
		if err := value.Decode(&raw); err != nil {
			return err
		}
		m.ID = raw.ID
		if raw.Respond == "" {
			m.RespondPolicy = RespondWhenMentioned
		} else {
			m.RespondPolicy = RespondPolicy(raw.Respond)
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
		}
	}
	return nil
}

// CanonicalID returns the canonical address for this declared channel
// (`group:<name>`). The store-side `Channel.ID` PK uses the same value.
func (cc ChannelConfig) CanonicalID() string {
	return "group:" + cc.Name
}
