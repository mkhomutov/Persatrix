// Package zapenc — orchestrator self-ingest core.
//
// BufferCore is a zapcore.Core that mirrors the orchestrator's own zap
// entries into the in-process logbuffer.Buffer that backs the
// `persatrix logs` REST + SSE surface.  Without this tee, agent-shipped
// entries (LogService.StreamLogs) are the only entries visible to
// `persatrix logs`; orchestrator-emitted lines (scheduler / executor /
// state / cost) are written to stderr only.
//
// RFC 0018 § B explicitly states the merged `persatrix logs` stream
// "lets a reader distinguish per-record provenance" between
// service.kind=orchestrator and service.kind=agent records — which
// requires the orchestrator's own records to land in the buffer.
//
// The Core is intended to be combined with the stderr Core via
// zapcore.NewTee: the human-readable / JSON wire encoding stays on
// stderr, while a parallel structured copy is admitted to the buffer.
// Entries that fail Buffer.Append's deny-by-default validation
// (missing or invalid execution_id, level below the buffer's drop
// threshold, rate-limited, etc.) are silently dropped — the buffer's
// own counters surface the reason to RFC 0019 metrics.
package zapenc

import (
	"time"

	"go.uber.org/zap/zapcore"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// BufferSink is the narrow surface BufferCore needs from
// logbuffer.Buffer.  Declared here so tests can substitute a fake
// without spinning up a real on-disk buffer.
type BufferSink interface {
	Append(logbuffer.Entry) logbuffer.DropReason
}

// bufferCore implements zapcore.Core by translating each Write into a
// logbuffer.Entry and delegating admission to the sink.
type bufferCore struct {
	zapcore.LevelEnabler
	sink            BufferSink
	serviceKind     string
	serviceInstance string
	serviceRole     string
	context         []zapcore.Field
}

// NewBufferCore constructs a Core that admits entries to sink.
// serviceKind/serviceInstance are stamped on every emitted entry to
// match the schema encoder's invariants (RFC 0018 § B).  level
// gates which severities are enqueued — typically the same level the
// stderr Core uses, so the on-disk view matches the operator's
// console.
func NewBufferCore(sink BufferSink, serviceKind, serviceInstance string, level zapcore.LevelEnabler) zapcore.Core {
	if sink == nil {
		panic("zapenc.NewBufferCore: sink is nil")
	}
	if level == nil {
		level = zapcore.InfoLevel
	}
	return &bufferCore{
		LevelEnabler:    level,
		sink:            sink,
		serviceKind:     serviceKind,
		serviceInstance: serviceInstance,
	}
}

// With clones the core and accumulates the supplied context fields so
// they are merged into every subsequent Write — mirrors the contract
// used by zap's built-in cores.
func (c *bufferCore) With(fields []zapcore.Field) zapcore.Core {
	clone := *c
	if len(fields) > 0 {
		merged := make([]zapcore.Field, 0, len(c.context)+len(fields))
		merged = append(merged, c.context...)
		merged = append(merged, fields...)
		clone.context = merged
	}
	return &clone
}

// Check is the standard zapcore enablement hook — adds this core to
// the CheckedEntry when the entry passes the level filter.
func (c *bufferCore) Check(ent zapcore.Entry, ce *zapcore.CheckedEntry) *zapcore.CheckedEntry {
	if c.Enabled(ent.Level) {
		return ce.AddCore(ent, c)
	}
	return ce
}

// Write builds a logbuffer.Entry from ent + context + fields and
// hands it to the sink.  Errors from Append are intentionally not
// propagated: a logging path must never fail the caller, and the
// buffer's own DropReason counters carry the diagnostic.
func (c *bufferCore) Write(ent zapcore.Entry, fields []zapcore.Field) error {
	enc := zapcore.NewMapObjectEncoder()
	for _, f := range c.context {
		f.AddTo(enc)
	}
	for _, f := range fields {
		f.AddTo(enc)
	}

	entry := logbuffer.Entry{
		SchemaVersion:   "1",
		Timestamp:       ent.Time.UTC(),
		Level:           levelToString(ent.Level),
		ServiceKind:     c.serviceKind,
		ServiceInstance: c.serviceInstance,
		ServiceRole:     c.serviceRole,
		Message:         ent.Message,
	}
	if entry.Timestamp.IsZero() {
		entry.Timestamp = time.Now().UTC()
	}
	if ent.Caller.Defined {
		entry.Source = &logbuffer.Source{
			File:     ent.Caller.File,
			Line:     uint32(ent.Caller.Line), //nolint:gosec // line numbers fit in uint32
			Function: ent.Caller.Function,
		}
	}

	// Apply the same legacy-rename normalisation as the schema encoder
	// so a call-site that still emits "runID" / "executionID" / etc.
	// lands in the right reserved slot.  The map lives in encoder.go.
	for legacy, schema := range legacyRenames {
		v, ok := enc.Fields[legacy]
		if !ok {
			continue
		}
		if _, alreadySet := enc.Fields[schema]; !alreadySet {
			enc.Fields[schema] = v
		}
		delete(enc.Fields, legacy)
	}

	entry.ExecutionID = popString(enc.Fields, "execution_id")
	entry.StepID = popString(enc.Fields, "step_id")
	entry.AgentID = popString(enc.Fields, "agent_id")
	entry.RequestID = popString(enc.Fields, "request_id")
	entry.TraceID = popString(enc.Fields, "trace_id")
	entry.SpanID = popString(enc.Fields, "span_id")

	// Surface workflow_id under attributes["workflow"] so the REST
	// `--workflow` filter (filterEntries in logs_handler.go) matches.
	if wf := popString(enc.Fields, "workflow_id"); wf != "" {
		if enc.Fields == nil {
			enc.Fields = map[string]any{}
		}
		if _, present := enc.Fields["workflow"]; !present {
			enc.Fields["workflow"] = wf
		}
	}

	if len(enc.Fields) > 0 {
		entry.Attributes = enc.Fields
	}

	c.sink.Append(entry)
	return nil
}

// Sync is a no-op — the buffer flushes to disk on its own cadence.
func (c *bufferCore) Sync() error { return nil }

// popString reads and removes a string-valued key.  Non-string values
// are left in place (so they still flow into Attributes) and return "".
func popString(m map[string]any, key string) string {
	v, ok := m[key]
	if !ok {
		return ""
	}
	s, ok := v.(string)
	if !ok {
		return ""
	}
	delete(m, key)
	return s
}

// levelToString maps zap levels to the uppercase strings the buffer's
// drop-level filter and the REST `--level` filter expect.
func levelToString(l zapcore.Level) string {
	switch l {
	case zapcore.DebugLevel:
		return "DEBUG"
	case zapcore.InfoLevel:
		return "INFO"
	case zapcore.WarnLevel:
		return "WARN"
	case zapcore.ErrorLevel:
		return "ERROR"
	case zapcore.DPanicLevel, zapcore.PanicLevel, zapcore.FatalLevel:
		return "ERROR"
	default:
		return l.CapitalString()
	}
}
