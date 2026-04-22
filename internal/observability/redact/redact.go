// Package redact ships the Persatrix log/span redaction hook surface
// (RFC 0018 § F).
//
// This package provides the *interface only*; the default [NoopRedactor] is a
// pass-through.  A real PII / secret scrubber is the responsibility of a
// future security RFC under the RFC 0009 umbrella.
//
// The [Redactor] interface is the single redaction contract shared across
// both observability signals:
//
//   - RFC 0018 — log records (this package, wired into the zap encoder
//     wrapper in [github.com/mkhomutov/persatrix/internal/observability/zapenc]).
//   - RFC 0019 Phase 2 — opt-in tool-payload capture as span attributes
//     (Go side, when the symmetrical wiring lands).
//
// Both call sites pass a map[string]any (the structured event for logs, the
// attribute bag for spans) and expect a map[string]any back.  Implementations
// must not mutate the input map in place — the redactor is invoked on every
// record / attribute bag and the caller assumes the input is unchanged when
// the redactor decides to no-op.
//
// The shape mirrors the Python [agents.observability.redact.Redactor]
// Protocol byte-for-byte so a single redactor implementation in a future
// security RFC can target both runtimes from one design.
package redact

// Redactor is the redaction hook called once per log entry (and per
// span-attribute bag, when wired in RFC 0019 Phase 2).
//
// Implementations must return a (possibly redacted) map and must not raise on
// well-formed input.  The zap encoder wrapper in
// [github.com/mkhomutov/persatrix/internal/observability/zapenc] swallows any
// panic from a buggy Redactor and emits the unredacted record with an
// out-of-band warning, mirroring the Python contract.
type Redactor interface {
	Redact(entry map[string]any) map[string]any
}

// NoopRedactor is the default redactor — it returns the entry unchanged.
//
// Used until a security RFC ships a real implementation.  The encoder wrapper
// still invokes Redact on every entry so future implementations can rely on a
// single hook without revisiting every call site.
type NoopRedactor struct{}

// Redact returns entry unchanged.
func (NoopRedactor) Redact(entry map[string]any) map[string]any {
	return entry
}
