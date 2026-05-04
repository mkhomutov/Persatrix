// Package main: rate limiter + circuit breaker bootstrap (RFC 0009 PR 2).
//
// Extracted from main.go so the orchestrator entry point stays under the
// 500-line code-review limit enforced by scripts/checks/file_size.py.
package main

import (
	"context"
	"os"
	"strconv"
	"time"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/security"
)

const (
	// rateLimitEnabledEnvVar opts the per-agent REST rate limiter in or
	// out at startup. Default is "on" — operators must explicitly disable
	// (and accept the WARN log + audit event documenting the choice).
	rateLimitEnabledEnvVar = "SECURITY_RATE_LIMIT_ENABLED"
	// rateLimitCallsEnvVar overrides the per-agent calls-per-window cap.
	rateLimitCallsEnvVar = "SECURITY_RATE_LIMIT_CALLS"
	// rateLimitWindowEnvVar overrides the rolling-window length in seconds.
	rateLimitWindowEnvVar = "SECURITY_RATE_LIMIT_WINDOW_SECONDS"
	// rateLimitMaxAgentsEnvVar bounds the per-agent ring map (DoS mitigation
	// against self-reported X-Agent-ID flooding; see RFC 0009 §B + PR plan).
	rateLimitMaxAgentsEnvVar = "SECURITY_RATE_LIMIT_MAX_AGENTS"

	defaultRateLimitCalls            = 60
	defaultRateLimitWindowSeconds    = 60
	defaultRateLimitMaxTrackedAgents = 1000
)

// initRateLimiter constructs the per-agent REST/gRPC rate limiter and
// circuit breaker from environment variables. Returns (nil, nil, nil)
// when the operator has explicitly opted out via
// `SECURITY_RATE_LIMIT_ENABLED=false`; in that case the caller logs a
// WARN and emits a `rate_limit.disabled` audit event so the choice is
// recorded in the tamper-evident chain (RFC 0009 §H).
//
// `auditor` is forwarded to both subsystems so denials, evictions, and
// quarantine transitions land in the orchestrator audit log; nil is
// safe (security-class events become no-ops).
func initRateLimiter(logger *zap.Logger, auditor security.AuditLogger) (*security.RateLimiter, *security.CircuitBreaker, error) {
	enabled := envBoolDefault(rateLimitEnabledEnvVar, true)
	calls := envIntDefault(rateLimitCallsEnvVar, defaultRateLimitCalls)
	window := envIntDefault(rateLimitWindowEnvVar, defaultRateLimitWindowSeconds)
	maxAgents := envIntDefault(rateLimitMaxAgentsEnvVar, defaultRateLimitMaxTrackedAgents)

	if !enabled {
		logger.Warn("security.rate_limit.disabled scope=startup",
			zap.String("source", rateLimitEnabledEnvVar),
		)
		emitRateLimitDisabled(logger, auditor)
		return nil, nil, nil
	}

	rl, err := security.NewRateLimiter(security.RateLimitConfig{
		CallsPerWindow:   calls,
		WindowSeconds:    window,
		MaxTrackedAgents: maxAgents,
		Enabled:          true,
		Logger:           logger,
		Auditor:          auditor,
	})
	if err != nil {
		return nil, nil, err
	}

	// RFC 0009 §H — quarantine thresholds. Capability + tool-denied
	// violations are the strongest signal an agent is misbehaving (a
	// short rolling window catches bursts); rate-limit + input-flagged
	// violations get a wider window (steady-state misbehaviour).
	cb, err := security.NewCircuitBreaker(security.CircuitBreakerConfig{
		Thresholds: map[security.ViolationType]security.ThresholdRule{
			security.ViolationCapability: {Count: 3, Window: 5 * time.Minute},
			security.ViolationToolDenied: {Count: 3, Window: 5 * time.Minute},
			security.ViolationRateLimit:  {Count: 5, Window: 10 * time.Minute},
			security.ViolationInputFlag:  {Count: 5, Window: 10 * time.Minute},
		},
		Logger:  logger,
		Auditor: auditor,
	})
	if err != nil {
		return nil, nil, err
	}
	logger.Info("rate limiter initialized",
		zap.Int("calls_per_window", calls),
		zap.Int("window_seconds", window),
		zap.Int("max_tracked_agents", maxAgents),
	)
	return rl, cb, nil
}

func emitRateLimitDisabled(logger *zap.Logger, auditor security.AuditLogger) {
	if auditor == nil {
		return
	}
	if err := auditor.Emit(context.Background(), security.AuditEvent{
		EventType: security.AuditRateLimitDisabled,
		Action:    "startup",
		Resource:  "rate_limiter",
		Outcome:   "disabled",
		Detail: map[string]any{
			"source": rateLimitEnabledEnvVar,
		},
	}); err != nil {
		logger.Warn("audit emit failed for rate_limit.disabled", zap.Error(err))
	}
}

func envBoolDefault(key string, def bool) bool {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	// strconv.ParseBool already accepts mixed-case forms ("True",
	// "FALSE", etc.); the previous strings.ToLower call was redundant.
	// (PR #244 review L-03.)
	b, err := strconv.ParseBool(v)
	if err != nil {
		return def
	}
	return b
}

// unquarantineToken reads SECURITY_UNQUARANTINE_TOKEN from the
// environment. Returns "" when unset.
//
// PR #244 round-2 review M-05: when the env var is unset the endpoint
// is reachable without authentication. That is a deliberate stop-gap
// (full identity lands in RFC 0009 Phase 4) but it inverts the project's
// deny-by-default posture, so the operator's choice to leave the
// endpoint open must be explicit and auditable rather than inferred
// from configuration silence. Two signals are emitted in the unset
// case:
//
//   - WARN log `security.unquarantine.unauthenticated scope=startup`
//     so the choice surfaces in stdout / log aggregation immediately.
//   - Security-class audit event [security.AuditUnquarantineEndpointOpen]
//     so the choice is recorded in the tamper-evident chain alongside
//     `rate_limit.disabled`. Per-event fsync \u2014 a crash before the next
//     request must not erase the record.
//
// A non-empty value is logged at Info so operators can verify the
// stop-gap is active.
//
// Extracted from main.go to keep that file under the 500-line limit
// (PR #244 review H-02).
func unquarantineToken(logger *zap.Logger, auditor security.AuditLogger) string {
	const envVar = "SECURITY_UNQUARANTINE_TOKEN"
	tok := os.Getenv(envVar)
	if tok == "" {
		logger.Warn("security.unquarantine.unauthenticated scope=startup",
			zap.String("source", envVar),
			zap.String("posture", "endpoint reachable without bearer token \u2014 set "+envVar+" to enable shared-secret gate"),
		)
		emitUnquarantineEndpointOpen(logger, auditor)
		return ""
	}
	logger.Info("unquarantine endpoint protected by shared secret",
		zap.String("source", envVar))
	return tok
}

func emitUnquarantineEndpointOpen(logger *zap.Logger, auditor security.AuditLogger) {
	if auditor == nil {
		return
	}
	if err := auditor.Emit(context.Background(), security.AuditEvent{
		EventType: security.AuditUnquarantineEndpointOpen,
		Action:    "startup",
		Resource:  "unquarantine_endpoint",
		Outcome:   "open",
		Detail: map[string]any{
			"source": "SECURITY_UNQUARANTINE_TOKEN",
		},
	}); err != nil {
		logger.Warn("audit emit failed for unquarantine.endpoint.open", zap.Error(err))
	}
}

func envIntDefault(key string, def int) int {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return def
	}
	return n
}
