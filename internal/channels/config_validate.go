package channels

import (
	"fmt"
	"math"
)

// config_validate.go holds [Config.Validate] — the cross-field loader
// invariants the JSON Schema cannot express. Split out of config.go so that
// file stays under the 500-line review cap (same precedent as reply_budget.go /
// router_metrics.go); the validation rules accrete one belt-and-suspenders
// check per governance knob as the RFC 0030 layers land, so they change on
// their own cadence from the struct/loader definitions.

// Validate enforces the cross-field invariants the JSON Schema cannot
// express alone:
//   - participant ids satisfy `ValidateParticipantID` (no `:`, no whitespace)
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
	// RFC 0030 Layer 1: the fleet-wide default cost ceiling. Zero is the
	// uncapped opt-in default; negative is a typo the loader rejects loudly
	// (the schema's `minimum: 0` catches it at `make validate`).
	if c.DefaultInteractionBudgetTokens < 0 {
		return fmt.Errorf("%w: default_interaction_budget_tokens=%d (must be >= 0)",
			ErrInvalidInteractionBudgetTokens, c.DefaultInteractionBudgetTokens)
	}
	// Interaction-id producer (IP3): the fleet-wide idle window. Explicit
	// zero is valid (idle rotation off); negative is a typo the loader
	// rejects loudly (the schema's `minimum: 0` catches it at `make validate`).
	if c.DefaultInteractionIdleTimeoutSeconds != nil && *c.DefaultInteractionIdleTimeoutSeconds < 0 {
		return fmt.Errorf("%w: default_interaction_idle_timeout_seconds=%d (must be >= 0)",
			ErrInvalidInteractionIdleTimeout, *c.DefaultInteractionIdleTimeoutSeconds)
	}
	// RFC 0030 Layer 2: the fleet-wide default reply budget. Zero is the
	// uncapped opt-in default; negative is a typo the loader rejects loudly
	// (the schema's `minimum: 0` catches it at `make validate`).
	if c.DefaultMaxRepliesPerParticipant < 0 {
		return fmt.Errorf("%w: default_max_replies_per_participant=%d (must be >= 0)",
			ErrInvalidMaxRepliesPerParticipant, c.DefaultMaxRepliesPerParticipant)
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

		// Reject a negative per-channel RFC 0030 Layer 1 cost ceiling. Zero
		// is valid (uncapped → inherits the fleet default); only negative is
		// an error. The schema's `minimum: 0` catches it at `make validate`;
		// this is the belt-and-suspenders for operators who skipped it.
		if ch.InteractionBudgetTokens < 0 {
			return fmt.Errorf("channels[%d=%s]: %w: %d (must be >= 0)",
				i, ch.Name, ErrInvalidInteractionBudgetTokens, ch.InteractionBudgetTokens)
		}

		// Reject a negative per-channel interaction idle window (IP3).
		// Explicit zero is valid (idle rotation off for this channel); only
		// negative is an error. The schema's `minimum: 0` catches it at
		// `make validate`; this is the belt-and-suspenders for skippers.
		if ch.InteractionIdleTimeoutSeconds != nil && *ch.InteractionIdleTimeoutSeconds < 0 {
			return fmt.Errorf("channels[%d=%s]: %w: %d (must be >= 0)",
				i, ch.Name, ErrInvalidInteractionIdleTimeout, *ch.InteractionIdleTimeoutSeconds)
		}

		// Reject a negative per-channel RFC 0030 Layer 2 reply budget. Zero is
		// valid (uncapped → inherits the fleet default); only negative is an
		// error. The schema's `minimum: 0` catches it at `make validate`; this
		// is the belt-and-suspenders for operators who skipped it.
		if ch.MaxRepliesPerParticipantPerInteraction < 0 {
			return fmt.Errorf("channels[%d=%s]: %w: %d (must be >= 0)",
				i, ch.Name, ErrInvalidMaxRepliesPerParticipant, ch.MaxRepliesPerParticipantPerInteraction)
		}

		// Reject a negative RFC 0030 Layer 4 end-vote quorum / window. Zero
		// never reaches here — LoadConfig normalizes it to the K=2 / W=3 default
		// before Validate runs; only a negative is an error. The schema's
		// `minimum: 1` catches it at `make validate`; this is the belt-and-
		// suspenders for operators who skipped that step.
		if ch.EndVoteThreshold < 0 {
			return fmt.Errorf("channels[%d=%s]: %w: %d (must be >= 1)",
				i, ch.Name, ErrInvalidEndVoteThreshold, ch.EndVoteThreshold)
		}
		if ch.EndVoteWindow < 0 {
			return fmt.Errorf("channels[%d=%s]: %w: %d (must be >= 1)",
				i, ch.Name, ErrInvalidEndVoteWindow, ch.EndVoteWindow)
		}

		if len(ch.Members) == 0 {
			return fmt.Errorf("channels[%d=%s]: at least one member required", i, ch.Name)
		}
		seenMember := make(map[string]bool, len(ch.Members))
		for j, m := range ch.Members {
			if err := ValidateParticipantID(m.ID); err != nil {
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
