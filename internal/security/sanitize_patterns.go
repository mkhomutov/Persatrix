package security

import "regexp"

// Pattern is one detection rule used by [InputSanitizer]. The Name is the
// stable operator-facing identifier (alerts are written against it); the
// Regex is compiled once at package init.
type Pattern struct {
	// Name is the stable identifier surfaced in [SanitizedInput.Flags] and
	// in the audit event's Detail.flags array. Names are written into
	// alerting rules — renaming without a deprecation breaks alerts
	// silently.
	Name string

	// Regex is compiled at package init via [regexp.MustCompile]. The
	// constructor panics if any pattern fails to compile, which is the
	// correct behaviour for a closed-set table that ships in source.
	Regex *regexp.Regexp

	// Description is operator-facing prose. Surfaced by the genpatterns
	// generator in the comment block above the corresponding Python
	// pattern entry so the two languages stay in sync without re-reading
	// the RFC.
	Description string
}

// DefaultPatterns is the canonical detection table. The Go side is the
// authority — a generator reads this slice and emits the Python mirror
// (agents/security_patterns.py). The pattern-parity test (Python) asserts
// the two sides stay byte-identical.
//
// Pattern families per RFC 0009 §C:
//
//   - instruction_override: phrases that try to make the LLM disregard or
//     replace prior instructions.
//   - role_injection: phrases that try to redefine the agent's identity.
//   - exfiltration: phrases that try to bait the agent into pushing data
//     to an attacker-controlled URL.
//
// All patterns are case-insensitive (`(?i)` prefix). Bounded `.{0,N}` in the
// exfiltration patterns prevents catastrophic backtracking on long inputs.
//
// Cross-language regex constraints (Go RE2 ↔ Python `re`):
//
//   - No backreferences (\1, \2, ...): unsupported by RE2; the generator
//     would compile this Go side and produce a Python pattern that no
//     longer means the same thing.
//   - No lookaround ((?=...), (?!...), (?<=...), (?<!...)): unsupported
//     by RE2.
//   - No POSIX character classes ([[:alpha:]]): RE2-only; Python `re`
//     would not parse them.
//   - Named groups (?P<name>...) are fine — both engines support them.
//
// PR #253 deep-review F7 — `cmd/genpatterns` cannot detect a Python-only
// construct because the Go side already refused to compile it; this
// constraint comment is the design-review backstop.
var DefaultPatterns = []Pattern{
	{
		Name:        "instruction_override",
		Regex:       regexp.MustCompile(`(?i)\bignore\s+(?:all\s+)?previous\s+instructions?\b`),
		Description: "Phrase asking the model to disregard prior instructions wholesale.",
	},
	{
		Name:        "instruction_override",
		Regex:       regexp.MustCompile(`(?i)\bdisregard\s+(?:everything|all|the)\b`),
		Description: "Variant phrasing of instruction override.",
	},
	{
		Name:        "instruction_override",
		Regex:       regexp.MustCompile(`(?i)\bnew\s+instructions\s*:`),
		Description: "Attempt to inject a fresh instruction block via a labelled prefix.",
	},
	{
		Name:        "instruction_override",
		Regex:       regexp.MustCompile(`(?i)\bsystem\s+prompt\s*:`),
		Description: "Attempt to spoof a fresh system-prompt block.",
	},
	{
		Name:        "role_injection",
		Regex:       regexp.MustCompile(`(?i)\byou\s+are\s+now\b`),
		Description: "Attempt to redefine the agent's role mid-context.",
	},
	{
		Name:        "role_injection",
		Regex:       regexp.MustCompile(`(?i)\bact\s+as\b`),
		Description: "Attempt to coerce a role swap.",
	},
	{
		Name:        "role_injection",
		Regex:       regexp.MustCompile(`(?i)\bpretend\s+to\s+be\b`),
		Description: "Attempt to coerce a role swap via roleplay framing.",
	},
	{
		Name:        "role_injection",
		Regex:       regexp.MustCompile(`(?i)\bforget\s+you\s+are\b`),
		Description: "Attempt to clear the agent's identity preamble.",
	},
	{
		Name:        "exfiltration",
		Regex:       regexp.MustCompile(`(?i)\bsend\b.{0,50}\bto\b\s+\S+\.(?:com|net|org|io|test|example)`),
		Description: "Bait the agent into sending data to an external host.",
	},
	{
		Name:        "exfiltration",
		Regex:       regexp.MustCompile(`(?i)\boutput\b.{0,50}\bhttps?://`),
		Description: "Bait the agent into echoing data to an external URL.",
	},
	{
		Name:        "exfiltration",
		Regex:       regexp.MustCompile(`(?i)\bPOST\b.{0,50}\bhttps?://`),
		Description: "Bait the agent into POSTing data to an external URL.",
	},
}
