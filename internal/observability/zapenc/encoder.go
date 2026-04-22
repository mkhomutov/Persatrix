// Package zapenc implements the Persatrix-schema zap encoder
// (RFC 0018 § B/C, Phase 2).
//
// The encoder produces one JSON object per zap entry conforming to the
// versioned wire format documented in docs/observability.md
// (schema_version: "1"):
//
//   - schema_version, timestamp, level, service.kind, service.instance,
//     message  (required, in this emission order)
//   - service.role, execution_id, step_id, agent_id, workflow_id,
//     request_id, trace_id, span_id, attributes, source  (optional, in this
//     emission order, present when applicable)
//   - any unknown keys are appended after the documented fields in
//     insertion order
//
// The encoder also:
//
//   - renames legacy field keys (camelCase + "runID") to their schema
//     snake_case names — the rename map is the call-site backstop while
//     the dispatch.go / scheduler.go / etc. audit lands the same renames
//     at the source
//   - reserves slots for trace_id / span_id (populated in PR 3 from
//     trace.SpanContextFromContext)
//   - invokes the configured [redact.Redactor] exactly once per entry, with
//     a panic-safe fallback that emits the unredacted record and logs an
//     out-of-band warning (mirroring the Python chain's contract)
//   - emits source as {file, line, function} from zap's entry.Caller
//     (consumers should construct the logger with zap.AddCaller())
//
// Pretty mode (PERSATRIX_LOG_FORMAT=pretty) is *not* this encoder — pretty is
// a developer affordance wired separately in cmd/orchestrator/main.go using
// zap's NewDevelopmentEncoderConfig.  The schema encoder is the production
// JSON wire format consumed by the future persatrix logs endpoint
// (RFC 0018 Phase 4).
//
// Package name "zapenc" (not "zapcore") avoids collision with upstream
// go.uber.org/zap/zapcore in importer files.  Pinned by RFC 0018
// "Files Touched (Estimated)" per PR #160 review.
package zapenc

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"sort"
	"time"

	"go.uber.org/zap/buffer"
	"go.uber.org/zap/zapcore"

	"github.com/mkhomutov/persatrix/internal/observability/redact"
)

// SchemaVersion is the value emitted under the schema_version key.  Bumping
// this constant is a breaking schema change; see docs/observability.md § 5.
const SchemaVersion = "1"

// PrettyEnvVar is the environment variable that toggles human-readable
// console output in cmd/orchestrator/main.go.  Defined here so the encoder
// package owns the schema-related constants in one place.
const PrettyEnvVar = "PERSATRIX_LOG_FORMAT"

// PrettyEnvValue is the [PrettyEnvVar] value that selects the dev console
// encoder.
const PrettyEnvValue = "pretty"

// fieldOrder is the canonical emission order from RFC 0018 § B (and
// docs/observability.md § 2/§ 3).  Tests assert this order byte-for-byte.
var fieldOrder = []string{
	// Required (RFC § B table 1)
	"schema_version",
	"timestamp",
	"level",
	"service.kind",
	"service.instance",
	"message",
	// Optional (RFC § B table 2)
	"service.role",
	"execution_id",
	"step_id",
	"agent_id",
	"workflow_id",
	"request_id",
	"trace_id",
	"span_id",
	"attributes",
	"source",
}

// fieldOrderIndex is a lookup of fieldOrder used to partition known vs
// unknown keys when ordering output.
var fieldOrderIndex = func() map[string]int {
	m := make(map[string]int, len(fieldOrder))
	for i, k := range fieldOrder {
		m[k] = i
	}
	return m
}()

// legacyRenames maps historical zap field keys to the schema's snake_case
// reserved names.  The rename map is the encoder-side backstop; the matching
// call-site audit lands the same renames at the source so the code is
// self-documenting (RFC 0018 PR 2 plan).
//
// Only the schema's reserved IDs (RFC § B optional fields 8–13) are aliased
// here.  Other camelCase keys (inputTokens, serviceName, etc.) are
// site-local context and currently pass through to the **top-level** of the
// emitted record unchanged — the `attributes` slot in [fieldOrder] is
// reserved for a future PR that nests these site-local keys under it.
var legacyRenames = map[string]string{
	// Run-ID semantics: the orchestrator scheduler historically used "runID"
	// for the workflow run identifier.  The schema names this concept
	// "execution_id" (RFC 0018 § B field 8).
	"runID":       "execution_id",
	"executionID": "execution_id",
	"agentID":     "agent_id",
	"workflowID":  "workflow_id",
	"stepID":      "step_id",
	// HTTP middleware historically used "request_id" already (snake_case);
	// no rename needed.  Listed here for documentation completeness:
	// "requestID":   "request_id",
}

// Options configures a new schema [Encoder].  All fields except Redactor are
// required; Redactor defaults to [redact.NoopRedactor] when nil.
type Options struct {
	// ServiceKind is one of "orchestrator", "agent", "cli" (RFC § B field 4).
	// The orchestrator main always passes "orchestrator".
	ServiceKind string

	// ServiceInstance is the process instance identity (RFC § B field 5).
	// For the orchestrator this is typically the node ID or hostname.
	ServiceInstance string

	// ServiceRole is optional (RFC § B field 7).  Used for agents only;
	// the orchestrator leaves it empty.
	ServiceRole string

	// Redactor is invoked once per entry before serialisation.  Defaults to
	// [redact.NoopRedactor] when nil.
	Redactor redact.Redactor
}

// NewEncoder returns a [zapcore.Encoder] that emits the Persatrix log schema.
//
// The returned encoder wraps zap's built-in JSON encoder for primitive
// field accumulation (AddString, AddInt, etc.) and overrides EncodeEntry to
// post-process the JSON into the schema's canonical key order, inject the
// service.* + schema_version fields, apply the rename map, project zap's
// entry.Caller into the source object, and call the registered Redactor.
//
// Construct the parent logger with zap.AddCaller() for the source field to
// be populated; without it source is omitted (the schema marks it Optional).
func NewEncoder(opts Options) zapcore.Encoder {
	if opts.Redactor == nil {
		opts.Redactor = redact.NoopRedactor{}
	}
	return &schemaEncoder{
		Encoder: zapcore.NewJSONEncoder(jsonEncoderConfig()),
		opts:    opts,
	}
}

// jsonEncoderConfig pins the inner JSON encoder's key names to a small fixed
// set we recognise during post-processing.  The names here are intentionally
// not the schema names — they are the *raw* keys the inner encoder emits,
// which the schemaEncoder then maps onto schema names.
func jsonEncoderConfig() zapcore.EncoderConfig {
	return zapcore.EncoderConfig{
		MessageKey:     "message",
		LevelKey:       "level",
		TimeKey:        "timestamp",
		NameKey:        "logger",
		CallerKey:      "caller",
		FunctionKey:    "function",
		StacktraceKey:  "stacktrace",
		LineEnding:     zapcore.DefaultLineEnding,
		EncodeLevel:    zapcore.CapitalLevelEncoder,
		EncodeTime:     zapcore.RFC3339NanoTimeEncoder,
		EncodeDuration: zapcore.SecondsDurationEncoder,
		EncodeCaller:   zapcore.FullCallerEncoder,
	}
}

// schemaEncoder is the concrete [zapcore.Encoder] returned by NewEncoder.
type schemaEncoder struct {
	zapcore.Encoder // embedded inner JSON encoder; inherits all Add*/OpenNamespace methods
	opts            Options
}

// Clone returns a copy that may accumulate context-bound fields independently
// of the parent.  Both encoder layers must be cloned so With(...) calls on
// child loggers do not leak into the parent's accumulated state.
func (e *schemaEncoder) Clone() zapcore.Encoder {
	return &schemaEncoder{
		Encoder: e.Encoder.Clone(),
		opts:    e.opts,
	}
}

// EncodeEntry produces a single JSON line in the Persatrix log schema.
//
// The implementation parses the inner encoder's output into a map, applies
// the rename map, injects the service.* + schema_version fields, projects
// zap's entry.Caller into a structured source object, invokes the Redactor,
// and re-serialises the result in canonical key order.
//
// Trust note (RFC 0018 PR 2 review, Should-Fix #1): the Redactor is invoked
// **after** schema_version / service.* / source injection, which means a
// buggy or hostile Redactor can mutate or strip these authoritative fields.
// This is deliberate — a future security RFC may need to scrub
// service.instance (PII hostnames) or source.file (path leakage).  Encoder
// invariants therefore depend on Redactor implementations being trusted;
// tests covering a real (non-noop) Redactor must assert that the schema
// fields survive the round-trip.
//
// The parse / re-serialise round-trip is intentional: it lets the encoder
// reuse zap's well-tested primitive serialisers (Time / Duration / Object
// encoding edge cases) instead of reimplementing them, and the per-entry
// cost is modest given the orchestrator's log volume.  If a future profile
// shows this as a hot path we can move to a streaming encoder; for now the
// simplicity-vs-speed trade is correct.
func (e *schemaEncoder) EncodeEntry(entry zapcore.Entry, fields []zapcore.Field) (*buffer.Buffer, error) {
	rawBuf, err := e.Encoder.EncodeEntry(entry, fields)
	if err != nil {
		return nil, err
	}
	defer rawBuf.Free()

	// The inner JSON encoder appends LineEnding ("\n") — strip before parsing
	// so encoding/json sees a single complete object.
	raw := bytes.TrimRight(rawBuf.Bytes(), "\n")
	record := make(map[string]any, len(fields)+8)
	if err := jsonUnmarshal(raw, &record); err != nil {
		// Fallback (RFC 0018 PR 2 review, Must-Fix #1): the inner encoder's
		// JSON failed to round-trip.  We must NOT silently emit the raw
		// (unrenamed, uninjected) bytes — downstream consumers filter on
		// schema_version: "1" and would silently drop these entries,
		// suppressing exactly the diagnostic an operator most needs.  Surface
		// the failure out-of-band on stderrSink (mirrors the redactor-panic
		// pattern) and synthesise a minimal schema-conformant envelope that
		// wraps the raw payload as a base64 attribute so the data is still
		// recoverable from the log stream.
		fmt.Fprintf(stderrSink, "persatrix: encoder fallback, inner JSON did not round-trip (%v); emitting compliant envelope\n", err)
		return e.encodeFallbackEnvelope(entry, raw, err), nil
	}

	// 1. Apply legacy rename map.  Renames only happen when the target key
	//    is not already present — call-site audits that pre-rename to the
	//    schema name take precedence over the encoder's backstop.
	for oldKey, newKey := range legacyRenames {
		if v, ok := record[oldKey]; ok {
			if _, exists := record[newKey]; !exists {
				record[newKey] = v
			}
			delete(record, oldKey)
		}
	}

	// 2. Inject schema_version + service.* group.  These are authoritative
	//    and overwrite any pre-existing keys (per RFC § B "set by the
	//    emitter at the moment a log record is created and never rewritten
	//    by the orchestrator on ingest" — at *creation* the encoder owns
	//    these keys exclusively).
	record["schema_version"] = SchemaVersion
	record["service.kind"] = e.opts.ServiceKind
	record["service.instance"] = e.opts.ServiceInstance
	if e.opts.ServiceRole != "" {
		record["service.role"] = e.opts.ServiceRole
	}

	// 3. Project entry.Caller → source object.  zap's inner encoder emits a
	//    flat "caller" string ("file:line"); the schema wants
	//    {file, line, function}.  Reading entry.Caller directly avoids
	//    re-parsing the formatted string.
	if entry.Caller.Defined {
		record["source"] = map[string]any{
			"file":     entry.Caller.File,
			"line":     entry.Caller.Line,
			"function": entry.Caller.Function,
		}
		// Inner encoder may have emitted "caller" / "function" at the top
		// level; remove so source is the single source of truth for call-
		// site provenance.
		delete(record, "caller")
		delete(record, "function")
	}

	// 4. Apply the redactor with a panic-safe fallback.  A buggy or hostile
	//    Redactor must not take down every logger.Info() call in the
	//    process; mirror the Python chain's contract by emitting the
	//    unredacted record and surfacing the panic out-of-band on stderr.
	record = applyRedactor(e.opts.Redactor, record)

	// 5. Serialise in canonical schema order.
	return serialiseOrdered(record)
}

// encodeFallbackEnvelope synthesises a minimal schema-conformant log line
// when the inner encoder's JSON cannot be round-tripped (Must-Fix #1).  The
// raw payload is base64-encoded under attributes.raw so operators can still
// recover the original bytes without breaking the schema_version filter the
// rest of the pipeline relies on.
//
// We deliberately do not run this envelope through the rename / redactor
// pipeline: this is an emergency code path and we want minimal dependencies
// on the very machinery that just failed.
func (e *schemaEncoder) encodeFallbackEnvelope(entry zapcore.Entry, raw []byte, parseErr error) *buffer.Buffer {
	envelope := map[string]any{
		"schema_version":   SchemaVersion,
		"timestamp":        entry.Time.UTC().Format(time.RFC3339Nano),
		"level":            entry.Level.CapitalString(),
		"service.kind":     e.opts.ServiceKind,
		"service.instance": e.opts.ServiceInstance,
		"message":          "encoder fallback: inner JSON did not round-trip",
		"attributes": map[string]any{
			"raw":         base64.StdEncoding.EncodeToString(raw),
			"parse_error": parseErr.Error(),
		},
	}
	if e.opts.ServiceRole != "" {
		envelope["service.role"] = e.opts.ServiceRole
	}
	if entry.Caller.Defined {
		envelope["source"] = map[string]any{
			"file":     entry.Caller.File,
			"line":     entry.Caller.Line,
			"function": entry.Caller.Function,
		}
	}
	out, err := serialiseOrdered(envelope)
	if err != nil {
		// Truly last-ditch: even the canonical serialiser failed.  Emit a
		// hand-crafted single-line JSON so the log stream remains parseable.
		out = pool.Get()
		out.AppendString(`{"schema_version":"` + SchemaVersion + `","level":"ERROR","message":"encoder fallback: serialise failed"}`)
		out.AppendString(zapcore.DefaultLineEnding)
	}
	return out
}

// applyRedactor invokes r.Redact and recovers from any panic, returning the
// original entry unchanged.  Mirrors the Python _apply_redactor processor.
func applyRedactor(r redact.Redactor, entry map[string]any) (out map[string]any) {
	defer func() {
		if rec := recover(); rec != nil {
			// Out-of-band: the encoder cannot itself emit a structured log
			// here without re-entering the same code path.  A bare stderr
			// line is the deliberate last line of defence; a future
			// security RFC that ships a real Redactor will exercise this
			// path during its own integration tests.
			fmt.Fprintf(stderrSink, "persatrix: redactor panicked, emitting unredacted record: %v\n", rec)
			out = entry
		}
	}()
	return r.Redact(entry)
}

// serialiseOrdered marshals record into a buffer in fieldOrder, with unknown
// keys sorted alphabetically and appended after the known fields.  The
// alphabetical sort gives byte-stable output for diffability — insertion
// order would depend on map iteration which Go intentionally randomises.
func serialiseOrdered(record map[string]any) (*buffer.Buffer, error) {
	out := pool.Get()
	out.AppendByte('{')

	first := true
	emit := func(key string) error {
		v, ok := record[key]
		if !ok {
			return nil
		}
		if !first {
			out.AppendByte(',')
		}
		first = false
		k, err := json.Marshal(key)
		if err != nil {
			return err
		}
		val, err := json.Marshal(v)
		if err != nil {
			return err
		}
		_, _ = out.Write(k)
		out.AppendByte(':')
		_, _ = out.Write(val)
		return nil
	}

	// 5a. Known fields in canonical order.
	for _, k := range fieldOrder {
		if err := emit(k); err != nil {
			out.Free()
			return nil, err
		}
	}

	// 5b. Unknown fields, alphabetised for byte-stability.
	unknown := make([]string, 0, len(record))
	for k := range record {
		if _, known := fieldOrderIndex[k]; !known {
			unknown = append(unknown, k)
		}
	}
	sort.Strings(unknown)
	for _, k := range unknown {
		if err := emit(k); err != nil {
			out.Free()
			return nil, err
		}
	}

	out.AppendByte('}')
	out.AppendString(zapcore.DefaultLineEnding)
	return out, nil
}

// pool reuses [buffer.Buffer] allocations across EncodeEntry calls.
var pool = buffer.NewPool()

// stderrSink is the destination for the encoder's out-of-band warnings
// (redactor panics, JSON-roundtrip fallbacks).  Indirected through a package
// var so the encoder_test can capture and assert the warning without
// hijacking os.Stderr globally.
//
// The sink is only swapped from sequential tests that use t.Cleanup() to
// restore it; we therefore do not protect it with a mutex.  If a future test
// adds t.Parallel() or runtime swapping, reintroduce a sync.RWMutex and
// take it on every read+write — do not re-add an unused lock as a
// placeholder (RFC 0018 PR 2 review, Should-Fix #3).
var stderrSink = newStderr()

// jsonUnmarshal is the JSON parser used by EncodeEntry.  Indirected through
// a package var so the fault-injection test can force the fallback path
// (RFC 0018 PR 2 review, Must-Fix #1 — the path was previously untestable
// because zap's inner JSON encoder always emits valid JSON in practice).
var jsonUnmarshal = json.Unmarshal
