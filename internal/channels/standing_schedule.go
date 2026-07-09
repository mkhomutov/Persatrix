package channels

// standing_schedule.go — RFC 0052 §E standing/scheduled discussions, the
// config-round-trip timer PRODUCER (v0.3.11 PR 7c-i).
//
// PR 7a landed the standing config backend + the aggregate-bound gate, and PR 7b
// activated both aggregate ceilings (`max_convenings` count, `standing_budget_tokens`
// spend) as live runtime bounds inside [ChannelRouter.ConveneChannel]. What
// remained of §E is the SCHEDULE itself: `autonomous.schedule_interval_seconds`
// is parsed, validated, and surfaced, but nothing FIRES it — a standing channel
// is convened manually, exactly like a one-shot ([convening_counter.go]/[standing_budget.go]
// both note the timer is "a later slice").
//
// §E resolves the wiring (OQ #4) as a CONFIG ROUND-TRIP, not a runtime
// `RegisterTimer` API: RFC 0024 timers are `agents.yaml`-canonical (the per-agent
// `scheduled_wakes` SQLite table is a derived cache rebuilt from config at boot),
// so a channel's schedule reaches the convener by registering an
// `autonomy.timers` entry in the convener persona's config. This file is the
// PRODUCER of that entry: the pure derivation from a resolved autonomous block to
// the [ConveneTimerSpec] the round-trip must register. It ships DARK — nothing
// consumes [ChannelRouter.StandingConveneTimers] yet, exactly as PR 1's
// router_autonomous.go registry shipped dark before the convene path, and as
// [ChannelRouter.ConveningCount] / [ChannelRouter.StandingSpend] shipped exported
// for a readout a later slice rendered. PR 7c-ii wires the consumer: the
// `agents.yaml` writer and the convener-side `ScheduledWake(callback_kind=convene)`
// handler that calls back into [ChannelRouter.ConveneChannel] (so the fired
// schedule passes the SAME §E aggregate ceilings the manual path does — the
// timer must never bypass the bounds PR 7b built for it).
//
// Two contracts this producer must honour, both pinned by standing_schedule_test.go:
//
//   - The timer id is agent.schema-valid AND reversible. A fired RFC 0024 wake
//     carries only `timer_id` + `callback_kind` (agents/event_loop_types.py
//     `ScheduledWake` — no channel_id), so the convener-side handler must recover
//     the channel to convene from the id alone. An armed channel is group-only
//     ([ChannelRouter.validateAutonomousChannelType]), so its id is the canonical
//     address `group:<name>` with `<name>` matching `channelNamePattern`
//     (lowercase alnum + hyphen) — a subset of the timer-id pattern
//     `^[a-z0-9][a-z0-9_-]*[a-z0-9]$`. Encoding the name behind a fixed
//     [standingConveneTimerPrefix] therefore yields a schema-valid id that
//     [ParseStandingConveneTimerID] reverses exactly.
//
//   - Only an armed STANDING channel yields a timer. A one-shot (interval 0),
//     disarmed, convener-less, or aggregate-unbounded block derives nothing, so
//     the round-trip never registers a schedule the operator did not declare — and
//     never one looser than the §E bounds (an unbounded standing schedule is the
//     runaway those bounds exist to stop; see [deriveConveneTimer]).
//
// DEFERRED to the PR 7c-ii consumer (NOT this producer's concern): the convener
// persona must run at `autonomy.level` semi-autonomous/autonomous for its
// EventLoop scheduler to exist and pick the timer up (agents/server_persona.py
// gates the scheduler on level; a `reactive` convener silently ignores a `timers`
// entry) — the `agents.yaml` writer bumps the level alongside writing the timer.
// The same writer must ALSO carry any existing legacy tick forward: writing a
// `timers` block flips `register_legacy_timer` to false (server_persona.py passes
// `register_legacy_timer=timers is None`), so injecting the convene entry into a
// convener that today ticks on `tick_interval_seconds` with NO `timers` block
// SILENTLY drops its ordinary autonomy tick — the writer must translate that tick
// into an explicit `{kind: "tick"}` timers entry, or a convener loses its
// heartbeat the moment it gains a schedule. Re-arm jitter is omitted (a single
// convener per channel needs no fan-out spread); the entry ships at the schema
// default `jitter_max_seconds: 0.0`.

import (
	"sort"
	"strings"
)

// StandingConveneKind is the RFC 0024 timer `callback_kind` carried on the
// `ScheduledWake` that re-convenes a standing autonomous channel. The
// convener-side handler (PR 7c-ii) branches on it to distinguish a convene wake
// from an ordinary autonomy tick / memory-consolidation / reflection wake, then
// recovers the channel from the wake's `timer_id` via [ParseStandingConveneTimerID].
// A distinct kind — not the `tick` the legacy timer carries — so a convene wake
// is attributable per-timer on dashboards and never folded into the idle path.
//
// The bareword "convene" is reused across unrelated namespaces (the RFC 0009
// forced-turn marker in `response_gate._FORCED_TURN_MARKERS`, the wire flag
// `payload["convene"]`, the convener-opening directive `kind` in
// `persona_runtime/convener.py`); this constant is the `ScheduledWake.callback_kind`
// namespace ALONE — the PR 7c-ii handler branches on THIS value, distinct from
// those, so a future reader must not conflate them.
const StandingConveneKind = "convene"

// standingConveneTimerPrefix is the fixed marker a convene timer id begins with,
// distinguishing it from every other `autonomy.timers` entry (the legacy tick,
// memory consolidation, reflection) and single-sourcing the [standingConveneTimerID]
// encoding and [ParseStandingConveneTimerID] reverse. The trailing `-` keeps the
// encoded id readable (`convene-planning`) and, because a channel name never
// starts with `-` (channelNamePattern), is an unambiguous split point.
const standingConveneTimerPrefix = "convene-"

// ConveneTimerSpec is one RFC 0024 timer entry the config-round-trip seam must
// register in the convener's `agents.yaml` `autonomy.timers` set to re-convene a
// standing channel. It carries the convener whose timer set owns the entry, the
// schema-valid + reversible timer id, the [StandingConveneKind] callback kind,
// and the schedule interval — everything the PR 7c-ii `agents.yaml` writer needs,
// plus the source [ChannelID] for logging/attribution (recoverable from TimerID,
// carried for convenience).
type ConveneTimerSpec struct {
	// ChannelID is the canonical group address this timer re-convenes.
	ChannelID string
	// ConvenerID is the agent id whose `autonomy.timers` set carries this entry —
	// the persona that authors the opening turn (`autonomous.convener`).
	ConvenerID string
	// TimerID is the RFC 0024 `id`: agent.schema-valid and reversibly encoding
	// [ChannelID] (see [standingConveneTimerID] / [ParseStandingConveneTimerID]).
	TimerID string
	// Kind is the RFC 0024 `callback_kind`, always [StandingConveneKind].
	Kind string
	// IntervalSeconds is the RFC 0024 `interval_seconds`: the schedule period.
	// A standing channel's `schedule_interval_seconds` is a positive integer, so
	// this is always >= 1, satisfying the schema's 1.0s busy-loop floor.
	IntervalSeconds int
}

// standingConveneTimerID encodes a group channel id into an RFC 0024 timer id
// that satisfies the agent.schema `autonomy.timers[].id` pattern and is reversed
// by [ParseStandingConveneTimerID]. Returns ok=false for a non-group address (a
// DM/thread id carries a `:` the timer-id pattern forbids and is never armed
// anyway). The name after the `group:` prefix matches `channelNamePattern`
// (lowercase alnum + hyphen), so `standingConveneTimerPrefix + name` is a valid
// timer id by construction.
func standingConveneTimerID(channelID string) (string, bool) {
	name, ok := strings.CutPrefix(channelID, string(ChannelTypeGroup)+":")
	if !ok || name == "" {
		return "", false
	}
	return standingConveneTimerPrefix + name, true
}

// ParseStandingConveneTimerID reverses [standingConveneTimerID]: it recovers the
// canonical group channel id a convene timer id encodes, returning ok=false for a
// timer id that is not a convene timer (the legacy tick, another kind's entry, or
// a bare prefix with no name). Exported because the PR 7c-ii consumer — the
// convener-side wake handler that maps a fired `ScheduledWake.timer_id` back to
// the channel to convene — is the load-bearing caller (a fired wake carries no
// channel_id; the id is the only channel reference).
//
// The recovered name must match `channelNamePattern`, not merely survive the
// prefix strip: the `autonomy.timers[].id` charset admits `_` (see the schema
// pattern), which a group channel name never contains, so an id like
// `convene-foo_bar` is a schema-valid timer id this producer NEVER emits. Rejecting
// it — rather than decoding it to the un-addressable `group:foo_bar` — makes parse a
// strict inverse over the encoder's range and a safe classifier: even if the 7c-ii
// handler recovered the channel off the id prefix instead of the authoritative
// `callback_kind`, an operator-named `convene-*` non-convene timer could not decode
// to a bogus convene target.
func ParseStandingConveneTimerID(timerID string) (string, bool) {
	name, ok := strings.CutPrefix(timerID, standingConveneTimerPrefix)
	if !ok || !channelNamePattern.MatchString(name) {
		return "", false
	}
	return string(ChannelTypeGroup) + ":" + name, true
}

// deriveConveneTimer derives the convener timer spec a channel's resolved
// autonomous block implies, returning ok=false when the channel is not an armed
// STANDING channel — it must be enabled, carry a positive schedule interval, name
// a convener, carry an aggregate bound, and be a group address. A one-shot channel
// (interval 0) is convened manually and gets no timer; a disarmed or convener-less
// block gets none either, so the round-trip only ever registers a schedule the
// operator armed.
//
// The aggregate-bound gate (`max_convenings` / `standing_budget_tokens`) is
// defence-in-depth, NOT redundant belt. RFC 0052 §E requires a standing channel to
// declare an aggregate bound (the config validate gate rejects one without —
// [ErrAutonomousStandingBoundRequired]), and PR 7b enforces those bounds at convene
// time. But [ChannelRouter.SetAutonomous] stamps the registry WITHOUT re-running
// that validate gate, and [ChannelRouter.ConveneChannel] enforces only the bounds
// that exist — so an unbounded standing block reaching the registry by any
// non-validated path (a forced setter, a future caller, config drift) would arm an
// UNBOUNDED recurring schedule, the exact runaway §E exists to prevent. Refusing to
// derive its timer is fail-closed: no timer, no auto-convene, so this producer never
// arms a schedule looser than its own §E bounds. (`<= 0`, not `== 0`: a negative is
// already rejected upstream, but the floor keeps the guard honest on its own.)
func deriveConveneTimer(channelID string, a AutonomousConfig) (ConveneTimerSpec, bool) {
	if !a.Enabled || a.ScheduleIntervalSeconds <= 0 || a.Convener == "" {
		return ConveneTimerSpec{}, false
	}
	if a.MaxConvenings <= 0 && a.StandingBudgetTokens <= 0 {
		return ConveneTimerSpec{}, false
	}
	timerID, ok := standingConveneTimerID(channelID)
	if !ok {
		return ConveneTimerSpec{}, false
	}
	return ConveneTimerSpec{
		ChannelID:       channelID,
		ConvenerID:      a.Convener,
		TimerID:         timerID,
		Kind:            StandingConveneKind,
		IntervalSeconds: a.ScheduleIntervalSeconds,
	}, true
}

// StandingConveneTimers enumerates the convener timer specs implied by every
// armed STANDING channel in the resolved autonomous registry — the full set the
// PR 7c-ii round-trip registers into the conveners' `agents.yaml` timer sets. The
// result is deterministic (timer-id sorted) so a config-round-trip diff is stable
// across boots. DARK: nothing fires these yet; this is the exported producer a
// later slice consumes, the readout-method precedent [ChannelRouter.ConveningCount]
// / [ChannelRouter.StandingSpend] set.
func (r *ChannelRouter) StandingConveneTimers() []ConveneTimerSpec {
	r.autonomousMu.RLock()
	specs := make([]ConveneTimerSpec, 0, len(r.autonomous))
	for channelID, a := range r.autonomous {
		if spec, ok := deriveConveneTimer(channelID, a); ok {
			specs = append(specs, spec)
		}
	}
	r.autonomousMu.RUnlock()
	sort.Slice(specs, func(i, j int) bool { return specs[i].TimerID < specs[j].TimerID })
	return specs
}
