package channels

// config_autonomous_defaults.go — the RFC 0052 autonomous block defaults
// (OQ #5: shipped conservative, tuned from the v0.3.11 live soak per
// ISSUE-0109) + the agenda-length cap. Split out of config_autonomous.go when
// the ISSUE-0109 calibration rationale pushed that file past the 500-line
// review cap — the same sibling precedent as config_autonomous_standing.go.

const (
	// DefaultAutonomousEnabled is the package default: autonomy is OPT-IN, so an
	// absent block (and every existing channel) is disabled and byte-for-byte
	// unchanged.
	DefaultAutonomousEnabled = false
	// DefaultAutonomousMaxRounds is the hard round bound an absent/zero
	// `max_rounds` fills to — a second independent terminator alongside the cost
	// cap ([RFC 0052 §D](../../docs/rfcs/0052-autonomous-agent-channels.md)).
	//
	// UNIT (deep review): the tally advances once per fanout cycle at the fanout
	// tail (bounded_close.go). Under floor control — the group default and the
	// expected autonomous posture, since autonomous convening is an open-floor
	// group concept — one cycle is one FLOOR ROUND (every responder speaking once
	// inside the serialized round), so the bound reads as floor rounds. With floor
	// control explicitly OFF, or on a degenerate <2-responder round, a cycle is a
	// single message, so the same number bounds far fewer conversational rounds.
	// Keep floor control on (the default) for the round reading to hold.
	//
	// The two paths also differ by one at the boundary, a consequence of the
	// no-reopen ordering (bounded_close.go / fanout.go): the FLOOR path counts and
	// closes AFTER the round runs, so the `max_rounds`-th round's discussion
	// happens and then the interaction closes; the CONCURRENT path counts and
	// closes BEFORE the dispatch, so the `max_rounds`-th message is NOT dispatched
	// live — it reaches members only as the close-notification artifact. So on a
	// concurrent (e.g. two-persona) roster `max_rounds` bounds `max_rounds - 1`
	// live exchanges. Immaterial at the default; at the tiny-bound extreme the
	// §D artifact guarantee takes over — the close never fires before the
	// interaction's first live dispatch (maybeBoundedClose's round-1 guard,
	// PR #716 review), so `max_rounds = 1` means one live exchange on either
	// path, never a zero-delivery close.
	//
	// VALUE (ISSUE-0109, v0.3.11 soak): 8, down from the shipped 12. On a
	// PRODUCTIVE roster this bound is structurally unreachable above the
	// cascade-depth cap — the ISSUE-0110 continuation re-fans the round's last
	// reply, so every continued round advances the round tally AND the reply's
	// cascade depth together, and the depth cap ([defaults.DefaultMaxCascadeDepth],
	// 5) crosses first whenever `max_rounds` exceeds it (all 5 productive soak
	// arcs closed on the depth bound; `max_rounds` never fired at 6/8/12). The
	// round bound earns its keep on STALL-driven arcs, where convener cadence
	// turns are fresh stimuli (depth resets per re-invite/advance) and rounds
	// accumulate without depth: 8 keeps that net comfortably above every observed
	// live arc (the stalliest closed well under it, and MT-AUTONOMOUS-001 ran
	// `max_rounds: 8` end to end) while tightening the worst-case spend a
	// stall-looping roster can reach before the structural close.
	DefaultAutonomousMaxRounds = 8
)

// MaxAutonomousAgendaItems caps the agenda length. It bounds the per-agenda-item
// escalation ration the convener gets in PR 6 (≤ one advance + one re-invite per
// item → total convener turns stay linear in agenda length), so a pathological
// agenda cannot become an unbounded turn budget. Generous; only a typo-scale list
// trips it.
const MaxAutonomousAgendaItems = 64
