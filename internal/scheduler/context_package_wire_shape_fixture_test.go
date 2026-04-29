package scheduler

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/executor/packaging"
	"github.com/mkhomutov/persatrix/internal/planner"
)

// TestContextPackage_WriteWireShapeFixture (RFC 0008 PR 6a — wire-shape
// contract follow-up) writes a representative Go-produced v1 context-package
// JSON to `tests/fixtures/context_package_v1.json` so the Python wire-shape
// test (`tests/unit/python/test_context_package_wire_shape.py`) can read it
// directly. Without this mutual contract a unilateral tag rename on either
// side would only surface at integration time.
//
// The test is opt-in via the `PERSATRIX_REGEN_FIXTURES=1` environment
// variable. CI runs without the env var so the fixture stays under source
// control and is reviewable in PRs; developers regenerate after wire-shape
// edits with `PERSATRIX_REGEN_FIXTURES=1 go test ./internal/scheduler -run
// TestContextPackage_WriteWireShapeFixture`.
func TestContextPackage_WriteWireShapeFixture(t *testing.T) {
	if os.Getenv("PERSATRIX_REGEN_FIXTURES") != "1" {
		t.Skip("set PERSATRIX_REGEN_FIXTURES=1 to regenerate the cross-language wire-shape fixture")
	}

	s := &WorkflowScheduler{
		logger:   zap.NewNop(),
		packager: packaging.NewPackager(nil),
	}
	outputs := map[string]string{
		"out1": "first step output",
	}
	step := planner.Step{ID: "fixture-step"}
	pkg, err := s.attachContextPackage(outputs, "fixture-run", step, "input", 1000)
	require.NoError(t, err)

	encoded, err := json.MarshalIndent(pkg, "", "  ")
	require.NoError(t, err)
	encoded = append(encoded, '\n')

	_, thisFile, _, ok := runtime.Caller(0)
	require.True(t, ok, "runtime.Caller failed")
	repoRoot := filepath.Join(filepath.Dir(thisFile), "..", "..")
	fixturePath := filepath.Join(repoRoot, "tests", "fixtures", "context_package_v1.json")
	require.NoError(t, os.MkdirAll(filepath.Dir(fixturePath), 0o755))
	require.NoError(t, os.WriteFile(fixturePath, encoded, 0o644)) //nolint:gosec // fixture under source control
	t.Logf("wrote wire-shape fixture: %s (%d bytes)", fixturePath, len(encoded))
}
