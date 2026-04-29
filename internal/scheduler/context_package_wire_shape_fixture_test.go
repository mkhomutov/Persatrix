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

// wireShapeFixturePath returns the absolute path to the canonical
// cross-language wire-shape fixture. Centralised so both the writer
// (`TestContextPackage_WriteWireShapeFixture`) and the always-on equality
// pin (`TestContextPackage_FixtureMatchesCurrentWireShape`) agree on the
// location without duplicating the relative-path arithmetic.
func wireShapeFixturePath(t *testing.T) string {
	t.Helper()
	_, thisFile, _, ok := runtime.Caller(0)
	require.True(t, ok, "runtime.Caller failed")
	repoRoot := filepath.Join(filepath.Dir(thisFile), "..", "..")
	return filepath.Join(repoRoot, "tests", "fixtures", "context_package_v1.json")
}

// buildWireShapeFixturePackage builds the canonical v1 context package used
// by both the regeneration writer and the always-on equality test. Keeping
// this helper as the single producer guarantees that both tests assert
// against bit-identical inputs — if the writer and the equality test ever
// drift, the contract is meaningless.
func buildWireShapeFixturePackage(t *testing.T) *packaging.Package {
	t.Helper()
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
	return pkg
}

// marshalWireShapeFixture renders a package the same way the on-disk
// fixture is rendered: 2-space indent + trailing newline. Centralising the
// encoding shape ensures byte-equality comparisons in the equality pin
// don't fail purely on whitespace.
func marshalWireShapeFixture(t *testing.T, pkg *packaging.Package) []byte {
	t.Helper()
	encoded, err := json.MarshalIndent(pkg, "", "  ")
	require.NoError(t, err)
	return append(encoded, '\n')
}

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
// TestContextPackage_WriteWireShapeFixture`. The always-on companion test
// `TestContextPackage_FixtureMatchesCurrentWireShape` enforces that the
// checked-in fixture stays current between regenerations so a Go-side wire
// drift cannot land silently.
func TestContextPackage_WriteWireShapeFixture(t *testing.T) {
	if os.Getenv("PERSATRIX_REGEN_FIXTURES") != "1" {
		t.Skip("set PERSATRIX_REGEN_FIXTURES=1 to regenerate the cross-language wire-shape fixture")
	}

	pkg := buildWireShapeFixturePackage(t)
	encoded := marshalWireShapeFixture(t, pkg)
	fixturePath := wireShapeFixturePath(t)
	require.NoError(t, os.MkdirAll(filepath.Dir(fixturePath), 0o755))
	require.NoError(t, os.WriteFile(fixturePath, encoded, 0o644)) //nolint:gosec // fixture under source control
	t.Logf("wrote wire-shape fixture: %s (%d bytes)", fixturePath, len(encoded))
}

// TestContextPackage_FixtureMatchesCurrentWireShape (RFC 0008 PR 6a — PR 227
// review follow-up) is the always-on counterpart to the opt-in writer above.
// It rebuilds the same package, marshals it identically, and asserts byte
// equality against the checked-in fixture.
//
// Why this is needed: the opt-in writer is `t.Skip`'d under CI, so a Go-side
// wire-shape change (e.g. a struct-tag rename, a new field, or a regression
// in `attachContextPackage`'s determinism contract) could land while the
// checked-in fixture went stale. The only protection would be the Python
// consumer noticing at integration time, and only after someone manually
// regenerated the fixture — which the original review (`docs/pr-reviews/
// pr-227-deep-review.md`, "Should Fix #2") flagged as a coverage gap.
//
// On failure the test points the developer at the regen command rather than
// silently rewriting the fixture: a wire-shape change should be a deliberate,
// reviewable diff, not a side effect of running tests.
func TestContextPackage_FixtureMatchesCurrentWireShape(t *testing.T) {
	pkg := buildWireShapeFixturePackage(t)
	got := marshalWireShapeFixture(t, pkg)

	fixturePath := wireShapeFixturePath(t)
	want, err := os.ReadFile(fixturePath) //nolint:gosec // path is computed from runtime.Caller, not user input.
	require.NoErrorf(t, err, "fixture %s missing — regenerate with PERSATRIX_REGEN_FIXTURES=1 go test ./internal/scheduler -run TestContextPackage_WriteWireShapeFixture", fixturePath)

	require.Equalf(t, string(want), string(got),
		"checked-in wire-shape fixture is stale relative to attachContextPackage output.\n"+
			"If this is a deliberate wire-shape change, regenerate with:\n"+
			"  PERSATRIX_REGEN_FIXTURES=1 go test ./internal/scheduler -run TestContextPackage_WriteWireShapeFixture\n"+
			"and re-run the Python consumer suite (tests/unit/python/test_context_package_wire_shape.py).\n"+
			"Fixture path: %s", fixturePath)
}
