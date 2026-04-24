package main

import (
	"os"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"

	"github.com/mkhomutov/persatrix/internal/observability/redact"
	"github.com/mkhomutov/persatrix/internal/observability/zapenc"
)

// buildLogger constructs the orchestrator's zap logger per RFC 0018 PR 2.
//
// Default (logFormat == "" || "json"): the Persatrix-schema encoder writes
// JSON conforming to docs/observability.md (schema_version: "1") to stderr,
// with a NoopRedactor in place until a security RFC ships a real one.
//
// Pretty (logFormat == "pretty"): zap's development console encoder writes
// human-readable colourised output for local debugging.  Pretty mode is
// intentionally NOT wrapped by the schema encoder — it is a developer
// affordance, not the wire format consumed by persatrix logs.
//
// service.kind is hard-coded to "orchestrator"; service.instance defaults to
// the hostname.  Both are documented in RFC 0018 § B as set by the emitter
// at process start and never rewritten on ingest.
func buildLogger(env, logFormat string) (*zap.Logger, error) {
	if logFormat == zapenc.PrettyEnvValue {
		// Dev console encoder: colour, caller, no JSON.  Matches the
		// previous zap.NewDevelopment() shape so make run output stays
		// recognisable in pretty mode.
		return zap.NewDevelopment()
	}

	// Production schema encoder.  service.instance is, in priority order:
	//   1. PERSATRIX_SERVICE_INSTANCE env var (operator-supplied, stable
	//      across pod restarts, useful where os.Hostname() returns an
	//      ephemeral synthetic name) — RFC 0018 PR 2 review, Should-Fix #2.
	//   2. os.Hostname() (best-effort).
	//   3. literal "orchestrator" (last resort; loses cross-process
	//      provenance when multiple nodes hit this fallback).
	instance := resolveServiceInstance()

	enc := zapenc.NewEncoder(zapenc.Options{
		ServiceKind:     "orchestrator",
		ServiceInstance: instance,
		Redactor:        redact.NoopRedactor{},
	})

	// In production, suppress DEBUG (matches the previous
	// zap.NewProduction() default level); in development, allow DEBUG so
	// JSON-mode local runs still see the verbose output developers expect.
	level := loggerLevel(env)

	core := zapcore.NewCore(enc, zapcore.AddSync(os.Stderr), level)
	return zap.New(core, zap.AddCaller()), nil
}

// resolveServiceInstance returns the service.instance string used by
// every Persatrix-schema log emission per RFC 0018 PR 2 review,
// Should-Fix #2.  Priority order: PERSATRIX_SERVICE_INSTANCE env →
// os.Hostname() → literal "orchestrator".  Extracted so the
// stderr-bound buildLogger and the BufferCore tee in
// attachBufferTee stamp identical service.instance values, which
// preserves the cross-process correlation the merged `persatrix logs
// _` view depends on.
func resolveServiceInstance() string {
	if v := os.Getenv("PERSATRIX_SERVICE_INSTANCE"); v != "" {
		return v
	}
	if host, err := os.Hostname(); err == nil && host != "" {
		return host
	}
	return "orchestrator"
}

// loggerLevel returns the minimum zap level the orchestrator logger
// admits, derived from --env.  Same in-process value is used by both
// the stderr Core (buildLogger) and the BufferCore tee
// (attachBufferTee) so the on-disk view in `persatrix logs` matches
// what the operator sees on stderr — no silent severity skew between
// the two surfaces.
func loggerLevel(env string) zapcore.Level {
	if env == "development" {
		return zap.DebugLevel
	}
	return zap.InfoLevel
}

// attachBufferTee wraps logger so every Write also lands in sink (the
// orchestrator's logbuffer.Buffer).  Returns the same logger when
// sink is nil — keeps callers branch-free.  Without this tee,
// orchestrator-emitted records would only be visible on stderr; the
// `persatrix logs <execution_id>` and merged `_` views would only
// see agent-shipped entries (LogService.StreamLogs).  RFC 0018 § B
// explicitly requires both provenances in the merged stream.
func attachBufferTee(logger *zap.Logger, sink zapenc.BufferSink, env string) *zap.Logger {
	if logger == nil || sink == nil {
		return logger
	}
	tee := zapenc.NewBufferCore(sink, "orchestrator", resolveServiceInstance(), loggerLevel(env))
	return logger.WithOptions(zap.WrapCore(func(c zapcore.Core) zapcore.Core {
		return zapcore.NewTee(c, tee)
	}))
}
