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
	// DefaultInteractionBudgetTokens is the fleet-wide RFC 0030 Layer 1
	// per-interaction cost ceiling (§E, v0.3.8) applied to any channel that
	// omits its own `interaction_budget_tokens`. Opt-in: zero or absent means
	// uncapped, so the ceiling is additive and existing channels are
	// unchanged. Resolved per-channel via
	// [ChannelConfig.ResolveInteractionBudgetTokens]. Negative is rejected.
	DefaultInteractionBudgetTokens int64 `yaml:"default_interaction_budget_tokens"`
	// DefaultMaxRepliesPerParticipant is the fleet-wide RFC 0030 Layer 2
	// per-participant reply budget (§F, v0.3.8) applied to any channel that
	// omits its own `max_replies_per_participant_per_interaction`. Opt-in:
	// zero or absent means uncapped, so fair-turn-taking is additive and
	// existing channels are unchanged. Resolved per-channel via
	// [ChannelConfig.ResolveMaxRepliesPerParticipant]. Negative is rejected.
	DefaultMaxRepliesPerParticipant int `yaml:"default_max_replies_per_participant"`
	// Governance holds the fleet-wide RFC 0030 governance knobs that are not
	// per-channel (§OQ-7). Currently just the exempt-principals list that
	// removes human principals from the Layer 2 reply budget.
	Governance GovernanceConfig `yaml:"governance"`
}

// GovernanceConfig is the `governance:` block of `config/channels.yaml` —
// fleet-wide RFC 0030 governance settings (§OQ-7, v0.3.8).
type GovernanceConfig struct {
	// ExemptPrincipals lists the principal classes that are exempt from the
	// Layer 2 per-participant reply budget. The only recognised value is
	// `human`, which maps to the `user` participant type on the wire: a human
	// driving a conversation should never be throttled by the budget meant to
	// keep agents from dominating (GL4 / §OQ-7). An unrecognised entry is
	// ignored (no participant type maps to it), so a typo silently fails open
	// to "not exempt" rather than failing the load.
	ExemptPrincipals []string `yaml:"exempt_principals"`
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

// DefaultEndVoteThreshold (K) and DefaultEndVoteWindow (W) are the RFC 0030
// Layer 4 (§H, v0.3.8) end-of-interaction quorum applied when a declared
// channel omits `end_vote_threshold` / `end_vote_window`. K distinct
// participants emitting `END_INTERACTION_VOTE` within W consecutive turns close
// the interaction. K=2 because one agent saying "done" is not consensus and two
// distinct agents in close succession is; W=3 lets the signal survive an
// intervening one-off comment. Like the floor-turn timeout and the salience cap
// — and unlike the cost ceiling / reply budget, where zero is the meaningful
// "uncapped" value — a zero/absent K or W normalizes to these defaults at load
// (a zero threshold or window is not a meaningful value). The layer is still
// opt-in: with no producer emitting the vote, a populated K/W never fires.
const (
	DefaultEndVoteThreshold = 2
	DefaultEndVoteWindow    = 3
)

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
	// InteractionBudgetTokens is the RFC 0030 Layer 1 (v0.3.8) per-interaction
	// cost ceiling for this channel: once the running token total for an
	// interaction on this channel would cross it, the wallet denies further
	// leases (`INTERACTION_BUDGET_EXHAUSTED`, fail-closed). Opt-in — zero or
	// absent inherits [Config.DefaultInteractionBudgetTokens] (itself zero =
	// uncapped) via [ChannelConfig.ResolveInteractionBudgetTokens]. Negative
	// is rejected. Unlike the salience cap this is NOT normalized to a
	// non-zero default at load: zero is a meaningful value (uncapped), so the
	// channel-vs-fleet precedence is resolved at read time, not load time.
	InteractionBudgetTokens int64 `yaml:"interaction_budget_tokens"`
	// MaxRepliesPerParticipantPerInteraction is the RFC 0030 Layer 2 (v0.3.8)
	// per-participant reply budget for this channel: a participant's (K+1)th
	// publish in one interaction on this channel is rejected pre-persistence
	// (HTTP 429 + `ErrParticipantBudgetExhausted`). Opt-in — zero or absent
	// inherits [Config.DefaultMaxRepliesPerParticipant] (itself zero =
	// uncapped) via [ChannelConfig.ResolveMaxRepliesPerParticipant]. Negative
	// is rejected. Like the cost ceiling and unlike the salience cap, this is
	// NOT normalized to a non-zero default at load: zero is a meaningful value
	// (uncapped), so the channel-vs-fleet precedence is resolved at read time.
	MaxRepliesPerParticipantPerInteraction int `yaml:"max_replies_per_participant_per_interaction"`
	// EndVoteThreshold (K) is the RFC 0030 Layer 4 (§H, v0.3.8) end-of-
	// interaction quorum for this channel: K distinct participants emitting
	// `END_INTERACTION_VOTE` within `EndVoteWindow` consecutive turns close the
	// interaction. Zero/absent normalizes to [DefaultEndVoteThreshold] (K=2) at
	// load — unlike the cost ceiling / reply budget, zero is not a meaningful
	// "uncapped" value here; negative is rejected. Like the floor-turn timeout
	// and salience cap, the normalized value is the resolved value (there is no
	// fleet default to fall back to).
	EndVoteThreshold int `yaml:"end_vote_threshold"`
	// EndVoteWindow (W) is the RFC 0030 Layer 4 (§H, v0.3.8) recency window: an
	// end-vote counts toward the quorum only while it is within W consecutive
	// turns of the current turn (so a stale vote from much earlier no longer
	// counts). Zero/absent normalizes to [DefaultEndVoteWindow] (W=3) at load;
	// negative is rejected.
	EndVoteWindow int `yaml:"end_vote_window"`
}

// ResolveMaxRepliesPerParticipant returns the effective RFC 0030 Layer 2 reply
// budget for this channel: the channel's own
// `max_replies_per_participant_per_interaction` when set (non-zero), otherwise
// the fleet-wide `default_max_replies_per_participant`. A zero result means
// uncapped. This is the single source of truth for the precedence — the
// startup resolver stamps the resolved value onto the router so the publish
// path enforces one number.
func (c ChannelConfig) ResolveMaxRepliesPerParticipant(fleetDefault int) int {
	if c.MaxRepliesPerParticipantPerInteraction > 0 {
		return c.MaxRepliesPerParticipantPerInteraction
	}
	return fleetDefault
}

// ResolveInteractionBudgetTokens returns the effective RFC 0030 Layer 1 cost
// ceiling for this channel: the channel's own `interaction_budget_tokens`
// when set (non-zero), otherwise the fleet-wide
// `default_interaction_budget_tokens`. A zero result means uncapped. This is
// the single source of truth for the precedence — the dispatcher stamps the
// resolved value onto the lease request so the wallet enforces one number.
func (c ChannelConfig) ResolveInteractionBudgetTokens(fleetDefault int64) int64 {
	if c.InteractionBudgetTokens > 0 {
		return c.InteractionBudgetTokens
	}
	return fleetDefault
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
		// Same zero/absent → default sentinel for the RFC 0030 Layer 4 end-vote
		// quorum (K) and recency window (W): a zero threshold or window is not a
		// meaningful "uncapped" value the way a zero budget is, so normalize to
		// the K=2 / W=3 defaults here. Negative is left as-is for Validate to reject.
		if cfg.Channels[i].EndVoteThreshold == 0 {
			cfg.Channels[i].EndVoteThreshold = DefaultEndVoteThreshold
		}
		if cfg.Channels[i].EndVoteWindow == 0 {
			cfg.Channels[i].EndVoteWindow = DefaultEndVoteWindow
		}
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return cfg, nil
}

// Validate — the cross-field loader invariants — lives in config_validate.go
// (split out so this file stays under the 500-line review cap).

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
