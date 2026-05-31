// ISSUE-0085 PR 4 — PERSATRIX_EPOCH boot-time env-var read.
//
// The orchestrator reads the env var once at boot and uses it as the
// per-process epoch emitted on the `persatrix-epoch` gRPC header on every
// outbound dispatch. Unlike the session id (per-room, resolved per request),
// the epoch is a single process-global value: `live` in production, a
// per-job id in CI.
package main

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// TestResolveEpochID_UnsetDefaultsToLive asserts the documented fallback
// when PERSATRIX_EPOCH is unset: the helper returns the canonical
// [channels.DefaultEpochID] anchor ("live") and emits an INFO line at boot
// so the operator can see the process landed on the single-world default
// epoch.
//
// The expected value is asserted against [channels.DefaultEpochID] — the one
// exported Go copy of the cross-language sentinel, pinned to "live" by
// channels.TestDefaultEpochID_CrossLanguageLockStepLiteral and named by the
// Python leaf test (test_epoch_id_leaf_module.py) as *the* Go anchor — not a
// bare local literal. This mirrors resolveSessionID sourcing its default from
// [channels.DefaultSessionID] (PR #335 review L2: the default "lives in one
// place"), so the orchestrator boot default can never drift from the value
// the channel-store migration and the Python filter agree on.
func TestResolveEpochID_UnsetDefaultsToLive(t *testing.T) {
	t.Setenv("PERSATRIX_EPOCH", "")

	core, recorded := observer.New(zap.InfoLevel)
	logger := zap.New(core)

	got := resolveEpochID(logger)
	assert.Equal(t, channels.DefaultEpochID, got)

	logs := recorded.FilterMessageSnippet("PERSATRIX_EPOCH").All()
	require.Len(t, logs, 1, "exactly one boot log line about the env var")
	assert.Equal(t, zap.InfoLevel, logs[0].Level)
	assert.Contains(t, logs[0].Message, "unset")
	assert.Contains(t, logs[0].Message, "live")
}

// TestResolveEpochID_SetReturnsValueQuietly asserts a well-formed value is
// returned verbatim and no WARN/INFO line is emitted (the happy path should
// be silent — orchestrator boot already logs plenty of state).
func TestResolveEpochID_SetReturnsValueQuietly(t *testing.T) {
	t.Setenv("PERSATRIX_EPOCH", "ci-job-1234")

	core, recorded := observer.New(zap.InfoLevel)
	logger := zap.New(core)

	got := resolveEpochID(logger)
	assert.Equal(t, "ci-job-1234", got)

	logs := recorded.FilterMessageSnippet("PERSATRIX_EPOCH").All()
	assert.Empty(t, logs, "well-formed value should not log")
}

// TestResolveEpochID_InvalidCharsWarnsButAccepts asserts that a value
// outside [A-Za-z0-9_-] emits a WARN at boot but is still accepted verbatim,
// mirroring the session-id soft-validation posture: an operator stuck on the
// env-var knob is never blocked on a stricter validator landing first.
func TestResolveEpochID_InvalidCharsWarnsButAccepts(t *testing.T) {
	t.Setenv("PERSATRIX_EPOCH", "my epoch")

	core, recorded := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	got := resolveEpochID(logger)
	assert.Equal(t, "my epoch", got, "value accepted verbatim")

	logs := recorded.FilterMessageSnippet("PERSATRIX_EPOCH").All()
	require.Len(t, logs, 1)
	assert.Equal(t, zap.WarnLevel, logs[0].Level)
}
