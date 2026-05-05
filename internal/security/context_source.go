package security

// ContextSource enumerates the provenance of a piece of content as it enters
// the orchestrator-side context pipeline. RFC 0009 §C established the four
// canonical values; OQ #7 added `channel_message` to disambiguate
// agent-to-agent posts on internal channels (RFC 0011) from generic external
// inputs.
//
// Closed set: any value not in [AllContextSources] is rejected by
// [InputSanitizer.Sanitize] so callers cannot smuggle a free-form provenance
// label into the audit trail.
type ContextSource string

// Closed set of provenance labels. The values are stable: operators alert on
// `detail.source == "channel_message"` and similar — renaming silently
// breaks alerts.
const (
	// ContextSourceInternal — content authored by orchestrator code (system
	// prompts, planner-generated scaffolding). Trusted by definition.
	ContextSourceInternal ContextSource = "internal"

	// ContextSourceExternal — content fetched from outside the trust
	// boundary (HTTP responses, file reads against untrusted paths,
	// future bridge inputs). The default high-suspicion bucket.
	ContextSourceExternal ContextSource = "external"

	// ContextSourceAgentOutput — content produced by another agent in the
	// same orchestrator. Treated as untrusted because an upstream LLM may
	// have been prompt-injected even if the orchestrator was not.
	ContextSourceAgentOutput ContextSource = "agent_output"

	// ContextSourceUser — content typed by a human user via REST/CLI/chat.
	// Sanitised but not flagged by default — humans intentionally writing
	// "ignore previous instructions" to test the agent should not show up
	// as an attack signal.
	ContextSourceUser ContextSource = "user"

	// ContextSourceChannelMessage — content posted to an RFC 0011 internal
	// channel. Sanitisation parity with `external`; tagged distinctly so
	// the audit trail tells "agent posted to channel" apart from "scraped
	// webpage" without log-line parsing. RFC 0009 OQ #7 + RFC 0011 §
	// Security Considerations.
	ContextSourceChannelMessage ContextSource = "channel_message"
)

// AllContextSources returns every defined [ContextSource]. Used by the
// closed-set test (`TestContextSource_Closed`) and by callers that need to
// enumerate the surface for documentation.
//
// Ordering is stable and arbitrary — callers must not depend on it.
func AllContextSources() []ContextSource {
	return []ContextSource{
		ContextSourceInternal,
		ContextSourceExternal,
		ContextSourceAgentOutput,
		ContextSourceUser,
		ContextSourceChannelMessage,
	}
}

// IsKnown reports whether s is a member of the closed set.
//
// The check is exact-match — case sensitivity is intentional because the
// values are also written verbatim to the audit log's Detail.source field
// and operators write alerts against them.
func (s ContextSource) IsKnown() bool {
	switch s {
	case ContextSourceInternal,
		ContextSourceExternal,
		ContextSourceAgentOutput,
		ContextSourceUser,
		ContextSourceChannelMessage:
		return true
	}
	return false
}
