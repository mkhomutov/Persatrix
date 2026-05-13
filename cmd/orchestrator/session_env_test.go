// RFC 0031 Phase 1 PR 2 — PERSATRIX_SESSION_ID boot-time env-var read.
//
// The orchestrator reads the env var at boot and uses it as the per-process
// default that every CreateChannel / PublishMessage write is stamped with.
// Phase 1 ships no per-request override (Phase 3 CLI `--session` lands that).
package main

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

// TestResolveSessionID_UnsetDefaultsToLegacy asserts the documented
// fallback when PERSATRIX_SESSION_ID is unset: the helper returns
// "legacy" and emits an INFO line at boot so the operator can see the
// process landed on the legacy carve-out.
func TestResolveSessionID_UnsetDefaultsToLegacy(t *testing.T) {
	t.Setenv("PERSATRIX_SESSION_ID", "")

	core, recorded := observer.New(zap.InfoLevel)
	logger := zap.New(core)

	got := resolveSessionID(logger)
	assert.Equal(t, "legacy", got)

	logs := recorded.FilterMessageSnippet("PERSATRIX_SESSION_ID").All()
	require.Len(t, logs, 1, "exactly one boot log line about the env var")
	assert.Equal(t, zap.InfoLevel, logs[0].Level)
	assert.Contains(t, logs[0].Message, "unset")
	assert.Contains(t, logs[0].Message, "legacy")
}

// TestResolveSessionID_SetReturnsValueQuietly asserts a well-formed value
// is returned verbatim and no WARN/INFO line is emitted (the happy path
// should be silent — orchestrator boot already logs plenty of state).
func TestResolveSessionID_SetReturnsValueQuietly(t *testing.T) {
	t.Setenv("PERSATRIX_SESSION_ID", "run-a")

	core, recorded := observer.New(zap.InfoLevel)
	logger := zap.New(core)

	got := resolveSessionID(logger)
	assert.Equal(t, "run-a", got)

	// No INFO/WARN about the env var on the happy path.
	logs := recorded.FilterMessageSnippet("PERSATRIX_SESSION_ID").All()
	assert.Empty(t, logs, "well-formed value should not log")
}

// TestResolveSessionID_InvalidCharsWarnsButAccepts asserts that a value
// outside [A-Za-z0-9_-] emits a WARN at boot but is still accepted
// verbatim. Hard validation lives in Phase 3 CLI (`persatrix session new`);
// Phase 1 plumbing treats the value as opaque storage so operators are
// not blocked on a stricter regex landing first.
func TestResolveSessionID_InvalidCharsWarnsButAccepts(t *testing.T) {
	t.Setenv("PERSATRIX_SESSION_ID", "my session")

	core, recorded := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	got := resolveSessionID(logger)
	assert.Equal(t, "my session", got, "value accepted verbatim in Phase 1")

	logs := recorded.FilterMessageSnippet("PERSATRIX_SESSION_ID").All()
	require.Len(t, logs, 1)
	assert.Equal(t, zap.WarnLevel, logs[0].Level)
}
