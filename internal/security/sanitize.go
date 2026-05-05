package security

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"time"

	"go.uber.org/zap"
)

// InputSanitizer detects prompt-injection patterns in inbound content
// (RFC 0009 §C). It is a defense-in-depth layer — the structural separation
// provided by the agent-side `<external_data>` envelope and the persona's
// system-prompt instructions are the primary mitigation.
//
// Behaviour by configured action:
//
//   - SanitizerActionPassthrough (default for v0.3.0): flagged content is
//     returned with Flagged=true and Content unchanged. The caller surfaces
//     the flagged status to the agent via the surrounding envelope.
//   - SanitizerActionQuarantine: flagged content is dropped (Content="") so
//     the caller can return a structured `tool_result_quarantined` error
//     instead of forwarding the body.
//
// On every flag, an `input.flagged` audit event fires through the injected
// [AuditLogger]. Audit emission is best-effort: a sink that returns an error
// is logged-and-continued rather than turning sanitisation into a synchronous
// dependency on the audit pipeline.
//
// Concurrency: safe for concurrent use. The pattern slice is read-only after
// construction; the auditor is stateless from the sanitizer's perspective
// (the file-backed implementation has its own mutex).
type InputSanitizer struct {
	patterns []Pattern
	auditor  AuditLogger
	action   SanitizerAction
	now      func() time.Time
	// logger receives a WARN line whenever auditor.Emit returns an
	// error. Optional — when nil, audit-sink failures are silently
	// dropped (the original v0.3.0 behaviour). PR #253 deep-review F3
	// added the option so on-call sees prompt-injection signals
	// vanishing rather than losing audit-chain entries without trace.
	logger *zap.Logger
}

// SanitizerOption configures an [InputSanitizer]. The zero-arg constructor
// (`NewInputSanitizer()`) returns a passthrough sanitizer with no auditor —
// safe for unit tests and for code that wants the detection signal but not
// the audit-emission side effect.
type SanitizerOption func(*InputSanitizer)

// WithSanitizerAction overrides the default ([SanitizerActionPassthrough]).
func WithSanitizerAction(a SanitizerAction) SanitizerOption {
	return func(s *InputSanitizer) {
		s.action = a
	}
}

// WithSanitizerAuditor wires an [AuditLogger] to receive `input.flagged`
// events. Pass nil to disable audit emission entirely (the default when the
// option is not supplied).
func WithSanitizerAuditor(a AuditLogger) SanitizerOption {
	return func(s *InputSanitizer) {
		s.auditor = a
	}
}

// WithSanitizerPatterns overrides [DefaultPatterns]. Used by tests that pin
// a minimal pattern set to keep assertions focused.
func WithSanitizerPatterns(p []Pattern) SanitizerOption {
	return func(s *InputSanitizer) {
		s.patterns = p
	}
}

// WithSanitizerClock injects a clock for deterministic audit timestamps.
// Defaults to [time.Now].
func WithSanitizerClock(now func() time.Time) SanitizerOption {
	return func(s *InputSanitizer) {
		if now != nil {
			s.now = now
		}
	}
}

// WithSanitizerLogger injects a zap logger that receives a WARN line
// whenever the configured auditor's Emit returns an error. The
// sanitizer's emit policy is best-effort by design (a sink failure must
// not turn detection into a synchronous dependency on the audit
// pipeline) — the logger surfaces the drop so on-call can correlate
// missing audit-chain entries with sink health rather than silently
// losing prompt-injection signals.
//
// Pass nil (or omit the option) to keep the original silent-drop
// behaviour. PR #253 deep-review F3.
func WithSanitizerLogger(l *zap.Logger) SanitizerOption {
	return func(s *InputSanitizer) {
		s.logger = l
	}
}

// NewInputSanitizer returns a sanitizer preloaded with [DefaultPatterns],
// passthrough action, no auditor, and `time.Now` as its clock.
func NewInputSanitizer(opts ...SanitizerOption) *InputSanitizer {
	s := &InputSanitizer{
		patterns: DefaultPatterns,
		action:   SanitizerActionPassthrough,
		now:      time.Now,
	}
	for _, opt := range opts {
		opt(s)
	}
	return s
}

// SanitizedInput is the result of [InputSanitizer.Sanitize].
//
// Content carries the body the caller should forward to downstream code.
// Under passthrough that is the original input verbatim; under quarantine it
// is the empty string when Flagged is true.
//
// Source carries the input's provenance unchanged so the caller can
// propagate it into the agent-side `<external_data>` envelope without a
// second lookup.
//
// Flagged signals at least one pattern matched. The Flags slice carries the
// stable Pattern.Name values for every match, deduplicated and sorted so
// downstream tests can rely on stable ordering.
type SanitizedInput struct {
	Content string
	Source  ContextSource
	Flagged bool
	Flags   []string
}

// ErrUnknownContextSource is returned when [Sanitize] is called with a
// ContextSource value not in the closed set defined by [AllContextSources].
// Caller bugs surface as build/test failures rather than silent unmarked
// content.
var ErrUnknownContextSource = errors.New("security: unknown ContextSource")

// Sanitize runs the configured pattern set over input and returns a
// [SanitizedInput]. The ctx is forwarded to the audit sink so cancellation
// from the caller propagates to a stalled fsync (the audit logger detaches
// cancellation internally for post-emit durability — see
// `Server.emitAudit`).
//
// Returns [ErrUnknownContextSource] if source is not a member of
// [AllContextSources]. Other errors are surfaced from the audit sink only;
// the sanitisation itself is in-memory and infallible.
func (s *InputSanitizer) Sanitize(ctx context.Context, input string, source ContextSource) (SanitizedInput, error) {
	if !source.IsKnown() {
		return SanitizedInput{}, fmt.Errorf("%w: %q", ErrUnknownContextSource, source)
	}

	flags := s.matchAll(input)

	out := SanitizedInput{
		Content: input,
		Source:  source,
		Flagged: len(flags) > 0,
		Flags:   flags,
	}
	if !out.Flagged {
		return out, nil
	}

	if s.action == SanitizerActionQuarantine {
		out.Content = ""
	}

	if s.auditor != nil {
		ev := AuditEvent{
			Timestamp: s.now().UTC(),
			EventType: AuditInputFlagged,
			Action:    "sanitize",
			Resource:  "input",
			Outcome:   "flagged",
			Detail: map[string]any{
				"source": string(source),
				"flags":  append([]string(nil), flags...),
				"action": s.action.String(),
			},
		}
		// Best-effort emit: a sink failure must not turn sanitisation into
		// a hard dependency on the audit pipeline. We don't propagate the
		// error back to the LLM-facing path. Where a logger has been
		// injected (WithSanitizerLogger), the failure surfaces at WARN so
		// on-call can correlate missing audit-chain entries with sink
		// health — without it, prompt-injection signals could vanish
		// without trace if the chain breaks (disk full, fsync stall,
		// append handle invalidated by external rename).
		if emitErr := s.auditor.Emit(ctx, ev); emitErr != nil && s.logger != nil {
			s.logger.Warn(
				"sanitize: audit emit failed",
				zap.Error(emitErr),
				zap.String("event_type", string(AuditInputFlagged)),
				zap.String("source", string(source)),
			)
		}
	}

	return out, nil
}

// matchAll returns the deduplicated, sorted list of pattern names that
// matched input. Sorting makes test assertions stable; deduplication keeps
// the output compact when the same pattern family fires more than once
// (e.g. two `\bsend\b.{0,50}\bto\b` matches in a longer payload).
func (s *InputSanitizer) matchAll(input string) []string {
	if input == "" {
		return nil
	}
	seen := make(map[string]struct{}, len(s.patterns))
	for _, p := range s.patterns {
		if _, ok := seen[p.Name]; ok {
			continue
		}
		if p.Regex.MatchString(input) {
			seen[p.Name] = struct{}{}
		}
	}
	if len(seen) == 0 {
		return nil
	}
	out := make([]string, 0, len(seen))
	for name := range seen {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}
