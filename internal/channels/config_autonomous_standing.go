package channels

import "errors"

// config_autonomous_standing.go holds the RFC 0052 §E (v0.3.11 PR 7)
// STANDING/scheduled slice of the autonomous block — the `schedule_interval_seconds`
// / `max_convenings` / `standing_budget_tokens` knobs' sentinels, the override
// accessors the cross-field gate reads, and the shared aggregate-bound predicate.
// Split out of config_autonomous.go (which is at the 500-line review cap) so the
// standing sub-feature's own declarations live with their own siblings, exactly as
// config_autonomous.go itself carved off config.go. The struct FIELDS stay on
// [AutonomousConfig] / [AutonomousOverrides] in config_autonomous.go (a field must
// live with its type); only the free-standing standing declarations live here.
//
// A STANDING channel is an armed channel with a positive schedule interval (the
// RFC 0024 timer that re-convenes it): every fire opens a fresh, SEPARATELY capped
// interaction, so the per-interaction cost cap does NOT bound the recurring total
// ([RFC 0052 §E](../../docs/rfcs/0052-autonomous-agent-channels.md)). A standing
// channel is therefore un-creatable without an AGGREGATE bound — the §E mirror of
// the per-interaction cap-required gate. This slice ships DARK apart from that
// gate: nothing fires the schedule or counts convenings yet (the config-round-trip
// timer seam + the convening counter are PR 7b).

// Standing validation sentinels (RFC 0052 §E). Matched by [errors.Is]; the REST
// layer maps each to 400 Bad Request, symmetric with the one-shot autonomous
// sentinels in config_autonomous.go.
var (
	// ErrInvalidAutonomousSchedule — `autonomous.schedule_interval_seconds` is
	// negative (zero is a one-shot channel; only a positive value arms the RFC 0024
	// standing schedule).
	ErrInvalidAutonomousSchedule = errors.New("channels: invalid autonomous.schedule_interval_seconds")
	// ErrInvalidAutonomousMaxConvenings — `autonomous.max_convenings` is negative
	// (zero is unset).
	ErrInvalidAutonomousMaxConvenings = errors.New("channels: invalid autonomous.max_convenings")
	// ErrInvalidAutonomousStandingBudget — `autonomous.standing_budget_tokens` is
	// negative (zero is unset).
	ErrInvalidAutonomousStandingBudget = errors.New("channels: invalid autonomous.standing_budget_tokens")
	// ErrAutonomousStandingBoundRequired — a STANDING autonomous channel (one whose
	// `autonomous.schedule_interval_seconds` is positive) declares no AGGREGATE
	// bound. RFC 0052 §E: a standing channel is re-convened on a timer, opening a
	// fresh SEPARATELY-capped interaction each fire, so the per-interaction cost cap
	// leaves the recurring total unbounded — the §E mirror of the cap-required gate.
	// It must declare a `max_convenings` count and/or a `standing_budget_tokens`
	// budget. The REST layer maps it to 400, symmetric with [ErrAutonomousCapRequired].
	ErrAutonomousStandingBoundRequired = errors.New("channels: a standing autonomous channel (autonomous.schedule_interval_seconds set) requires an aggregate bound (autonomous.max_convenings and/or autonomous.standing_budget_tokens)")
)

// standingBoundMissing reports whether a STANDING channel (a positive schedule
// interval) declares NO aggregate bound — the shared §E gate both the load path
// ([Config.Validate]) and the apply path ([ChannelConfigOverrides.validateAutonomous])
// key on, so the two enforcement points cannot drift. A one-shot channel (zero
// interval) is bounded by the per-interaction cap alone and is never gated here.
func standingBoundMissing(scheduleIntervalSeconds, maxConvenings int, standingBudgetTokens int64) bool {
	return scheduleIntervalSeconds > 0 && maxConvenings <= 0 && standingBudgetTokens <= 0
}

// effectiveScheduleInterval reports the schedule interval this override resolves
// to — the set value or, if absent, 0 (a one-shot channel). Used by the standing
// aggregate-bound cross-field gate.
func (o *AutonomousOverrides) effectiveScheduleInterval() int {
	if o != nil && o.ScheduleIntervalSeconds != nil {
		return *o.ScheduleIntervalSeconds
	}
	return 0
}

// effectiveMaxConvenings reports the aggregate convening-count bound this override
// resolves to — the set value or, if absent, 0 (unset).
func (o *AutonomousOverrides) effectiveMaxConvenings() int {
	if o != nil && o.MaxConvenings != nil {
		return *o.MaxConvenings
	}
	return 0
}

// effectiveStandingBudgetTokens reports the aggregate standing-window cost budget
// this override resolves to — the set value or, if absent, 0 (unset).
func (o *AutonomousOverrides) effectiveStandingBudgetTokens() int64 {
	if o != nil && o.StandingBudgetTokens != nil {
		return *o.StandingBudgetTokens
	}
	return 0
}
