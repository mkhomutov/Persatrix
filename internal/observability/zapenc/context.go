// trace context binding for the schema encoder (RFC 0018 Phase 3).
//
// zap's [zapcore.Entry] does not carry a context.Context, so the schema
// encoder cannot read trace_id / span_id directly during EncodeEntry.  The
// otelzap-style middleware approach used here binds the IDs as zap fields on
// the logger when a span is active, and the encoder's existing
// schema_version-aware fieldOrder pipeline emits them in the canonical slot
// (already reserved at fieldOrder positions 12-13).
//
// Call sites that have a context.Context with an OTEL span in scope wrap
// their logger with [LoggerWithContext] before emitting; call sites without
// a context omit the IDs entirely (the schema's "Optional" contract from
// RFC 0018 § B).  This keeps the encoder simple and avoids a Core wrapper
// that would have to reach back into goroutine-local storage to recover the
// active context.

package zapenc

import (
	"context"

	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"
)

// LoggerWithContext returns a derived logger that emits trace_id / span_id
// fields when ctx carries a valid OTEL SpanContext.  When no span is active
// (or the context is nil) the original logger is returned unchanged so the
// schema's "Optional" contract is preserved (absent fields are omitted, not
// emitted as empty strings).
//
// Use at any call site that has a context.Context in scope and wants the
// emitted log records to carry the IDs of the currently-active span.  The
// returned logger is bound — subsequent .With(...) calls compose normally.
//
// Performance note: this creates a new zap.Logger per call when a span is
// active.  In hot paths (per-step dispatch, per-tick) wrap once at the top
// of the function and reuse the returned logger.
func LoggerWithContext(ctx context.Context, logger *zap.Logger) *zap.Logger {
	if ctx == nil || logger == nil {
		return logger
	}
	sc := trace.SpanContextFromContext(ctx)
	if !sc.IsValid() {
		return logger
	}
	return logger.With(
		zap.String("trace_id", sc.TraceID().String()),
		zap.String("span_id", sc.SpanID().String()),
	)
}
