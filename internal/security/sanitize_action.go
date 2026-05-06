package security

import "strings"

// SanitizerAction selects what [InputSanitizer.Sanitize] does with content
// that matched at least one detection pattern. RFC 0009 OQ #2 resolved the
// v0.3.0 default as `passthrough` — the orchestrator sees the flag, the
// agent receives the content with `flagged="true"` in the surrounding
// `<external_data>` envelope, and the agent's system-prompt instructions
// are responsible for not following the embedded directive.
//
// Operators running production deployments that ingest from genuinely
// untrusted bridges may opt into `quarantine` instead, in which case the
// content is dropped and a structured `tool_result_quarantined` error is
// returned to the agent.
type SanitizerAction int

const (
	// SanitizerActionPassthrough preserves flagged content. The default.
	// Audit event still fires; the agent sees the content with the
	// `flagged="true"` attribute on its `<external_data>` envelope.
	SanitizerActionPassthrough SanitizerAction = iota

	// SanitizerActionQuarantine drops flagged content. The agent receives
	// a `tool_result_quarantined` error in place of the body. Used by
	// deployments where false-positive cost is lower than the cost of
	// surfacing adversarial bytes to the LLM.
	SanitizerActionQuarantine
)

// String renders the canonical YAML/CLI form. Used by audit Detail and by
// the round-trip test (`TestParseSanitizerAction`).
func (a SanitizerAction) String() string {
	switch a {
	case SanitizerActionQuarantine:
		return "quarantine"
	default:
		return "passthrough"
	}
}

// ParseSanitizerAction is the inverse of [SanitizerAction.String]. Returns
// `(SanitizerActionPassthrough, false)` on any unrecognised input — callers
// at startup should treat the boolean as a config-validation signal and log
// loudly rather than silently accepting the default. Comparison is
// case-insensitive: YAML front-matter typically lowercases, CLI flags
// sometimes upper.
func ParseSanitizerAction(s string) (SanitizerAction, bool) {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "passthrough":
		return SanitizerActionPassthrough, true
	case "quarantine":
		return SanitizerActionQuarantine, true
	default:
		return SanitizerActionPassthrough, false
	}
}

// AllSanitizerActions returns every defined [SanitizerAction] in declaration
// order. Used by `cmd/genpatterns` to emit the Python mirror of the action
// enum so the closed set stays Go↔Python in sync without hand-duplication.
//
// Ordering matches the iota declaration above and is part of the generator
// contract — reordering forces a Python regeneration but does not change
// behaviour.
func AllSanitizerActions() []SanitizerAction {
	return []SanitizerAction{
		SanitizerActionPassthrough,
		SanitizerActionQuarantine,
	}
}
