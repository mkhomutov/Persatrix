package scheduler

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/executor/packaging"
	"github.com/mkhomutov/persatrix/internal/planner"
)

// TestAttachContextPackage_EmitsWarnings asserts the M3 review fix: every
// entry the packager records in pkg.Metrics.Warnings must surface as a
// structured zap warning so operators can detect compression-pressure
// regressions without parsing the JSON inside agent dispatches. The PR plan's
// sizing-risk note designates structured logs as Phase 1's substitute for
// cost-metrics emission, so the absence of these warnings was a contract gap.
func TestAttachContextPackage_EmitsWarnings(t *testing.T) {
	core, recorded := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	s := &WorkflowScheduler{
		logger:   logger,
		packager: packaging.NewPackager(nil),
	}

	// Force "extreme_compression_capped": a single oversized candidate that
	// cannot fit the budget — admittedTokens == 0 routes through the cap path
	// in packager.Build.
	outputs := map[string]string{
		"big": strings.Repeat("x", 4000), // ~1000 estimated tokens (chars/4)
	}
	step := planner.Step{ID: "s1"}

	_, err := s.attachContextPackage(outputs, "run-warn-1", step, "input", 10)
	require.NoError(t, err)

	entries := recorded.FilterMessage("context_package warning").All()
	require.NotEmpty(t, entries, "expected at least one context_package warning")

	// Verify the warning carries the full diagnostic context — execution_id,
	// step_id, and the metrics needed to triage the workload.
	e := entries[0]
	fields := e.ContextMap()
	assert.Equal(t, "extreme_compression_capped", fields["warning"])
	assert.Equal(t, "run-warn-1", fields["execution_id"])
	assert.Equal(t, "s1", fields["step_id"])
	assert.Contains(t, fields, "compression_ratio")
	assert.Contains(t, fields, "candidates_dropped")
	assert.Contains(t, fields, "tokens_before")
	assert.Contains(t, fields, "tokens_after")
}

// TestAttachContextPackage_NoWarningsWhenClean confirms the warning path is
// silent when the packager admits everything within budget — i.e. the
// structured-log channel does not fire spuriously on healthy workloads.
func TestAttachContextPackage_NoWarningsWhenClean(t *testing.T) {
	core, recorded := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	s := &WorkflowScheduler{
		logger:   logger,
		packager: packaging.NewPackager(nil),
	}

	outputs := map[string]string{
		"small": "hello world",
	}
	step := planner.Step{ID: "s1"}

	_, err := s.attachContextPackage(outputs, "run-clean", step, "input", 1000)
	require.NoError(t, err)

	assert.Empty(t, recorded.FilterMessage("context_package warning").All())
}
