package main

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

// ISSUE-0006: envIntDefault silently falls back on invalid/zero env values,
// so a typo in SECURITY_RATE_LIMIT_CALLS (e.g. "0", "abc") boots with the
// default rate limit and no log line says so. The fix below pins the
// post-fix behaviour: when the env var is non-empty AND unparseable / <=0,
// emit one WARN naming the var and the rejected value, then fall back to
// the default. Empty (unset) stays silent — operators who never set the
// var are not misconfigured.

const envIntDefaultWarnMsg = "security.rate_limit.env_invalid scope=startup"

func TestEnvIntDefault_UnsetReturnsDefaultNoWarn(t *testing.T) {
	t.Setenv(rateLimitCallsEnvVar, "")

	core, recorded := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	got := envIntDefault(logger, rateLimitCallsEnvVar, defaultRateLimitCalls)
	assert.Equal(t, defaultRateLimitCalls, got, "unset env returns default")
	assert.Empty(t, recorded.FilterMessage(envIntDefaultWarnMsg).All(),
		"unset env must not WARN (operator did not try to override)")
}

func TestEnvIntDefault_ValidValueAccepted(t *testing.T) {
	t.Setenv(rateLimitCallsEnvVar, "120")

	core, recorded := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	got := envIntDefault(logger, rateLimitCallsEnvVar, defaultRateLimitCalls)
	assert.Equal(t, 120, got, "valid value parsed and returned")
	assert.Empty(t, recorded.FilterMessage(envIntDefaultWarnMsg).All(),
		"valid value must not WARN")
}

func TestEnvIntDefault_UnparseableWarnsAndFallsBack(t *testing.T) {
	t.Setenv(rateLimitCallsEnvVar, "abc")

	core, recorded := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	got := envIntDefault(logger, rateLimitCallsEnvVar, defaultRateLimitCalls)
	assert.Equal(t, defaultRateLimitCalls, got, "unparseable falls back to default")

	entries := recorded.FilterMessage(envIntDefaultWarnMsg).All()
	require.Len(t, entries, 1,
		"unparseable env must produce exactly one WARN so the misconfiguration is visible")
	assert.Equal(t, zap.WarnLevel, entries[0].Level)

	fields := entries[0].ContextMap()
	assert.Equal(t, rateLimitCallsEnvVar, fields["source"], "WARN must name the env var")
	assert.Equal(t, "abc", fields["value"], "WARN must echo the rejected value")
	assert.Equal(t, int64(defaultRateLimitCalls), fields["fallback"],
		"WARN must report the fallback the operator actually got")
}

func TestEnvIntDefault_ZeroOrNegativeWarnsAndFallsBack(t *testing.T) {
	cases := []struct {
		name  string
		value string
	}{
		{"zero", "0"},
		{"negative", "-5"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(rateLimitCallsEnvVar, tc.value)

			core, recorded := observer.New(zap.WarnLevel)
			logger := zap.New(core)

			got := envIntDefault(logger, rateLimitCallsEnvVar, defaultRateLimitCalls)
			assert.Equal(t, defaultRateLimitCalls, got,
				"non-positive value falls back to default (disable knob is SECURITY_RATE_LIMIT_ENABLED, not 0)")

			entries := recorded.FilterMessage(envIntDefaultWarnMsg).All()
			require.Len(t, entries, 1,
				"non-positive env must produce exactly one WARN")
			fields := entries[0].ContextMap()
			assert.Equal(t, rateLimitCallsEnvVar, fields["source"])
			assert.Equal(t, tc.value, fields["value"])
		})
	}
}

func TestEnvIntDefault_NilLoggerSafe(t *testing.T) {
	// envIntDefault must remain nil-logger-safe so callers in test fixtures
	// or boot paths without a configured logger don't panic on misconfig.
	t.Setenv(rateLimitCallsEnvVar, "abc")
	got := envIntDefault(nil, rateLimitCallsEnvVar, defaultRateLimitCalls)
	assert.Equal(t, defaultRateLimitCalls, got)
}
