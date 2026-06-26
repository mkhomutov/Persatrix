package archpolicy

import (
	"os/exec"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestForbiddenInternalImports unit-tests the pure classifier that decides
// whether a guarded package's transitive dependency list crosses the
// dependency-direction boundary. Split out from the live `go list` check below
// so the boundary rule is testable with synthetic inputs — the same shape as
// the RFC 0029 personal/society boundary guard (a pure `is_construction_external`
// classifier separated from the runtime hook). The "up-import caught" case is
// the teeth: if this ever stops flagging an in-module dependency, the live gate
// is silently toothless.
func TestForbiddenInternalImports(t *testing.T) {
	const mod = "example.com/proj"

	tests := []struct {
		name    string
		pkg     string
		deps    []string
		allowed []string
		want    []string
	}{
		{
			name: "clean leaf imports only stdlib and external packages",
			pkg:  "example.com/proj/leaf",
			deps: []string{
				"fmt",
				"example.com/proj/leaf", // itself
				"google.golang.org/grpc",
				"google.golang.org/protobuf/runtime/protoimpl",
			},
			allowed: nil,
			want:    nil,
		},
		{
			name: "up-import into an orchestrator-internal package is caught",
			pkg:  "example.com/proj/leaf",
			deps: []string{
				"example.com/proj/leaf",
				"example.com/proj/internal/orchestrator",
			},
			allowed: nil,
			want:    []string{"example.com/proj/internal/orchestrator"},
		},
		{
			name: "an explicitly-allowed in-module leaf dependency is not a violation",
			pkg:  "example.com/proj/leaf",
			deps: []string{
				"example.com/proj/leaf",
				"example.com/proj/internal/generated/walletpb",
			},
			allowed: []string{"example.com/proj/internal/generated/walletpb"},
			want:    nil,
		},
		{
			name:    "the package itself is never reported as a violation",
			pkg:     "example.com/proj/leaf",
			deps:    []string{"example.com/proj/leaf"},
			allowed: nil,
			want:    nil,
		},
		{
			name: "multiple violations are returned sorted and de-duplicated of self/allowed",
			pkg:  "example.com/proj/leaf",
			deps: []string{
				"example.com/proj/leaf",
				"example.com/proj/internal/server",
				"example.com/proj/internal/cost",
				"example.com/proj/cmd/orchestrator",
				"example.com/proj/internal/generated/walletpb",
			},
			allowed: []string{"example.com/proj/internal/generated/walletpb"},
			want: []string{
				"example.com/proj/cmd/orchestrator",
				"example.com/proj/internal/cost",
				"example.com/proj/internal/server",
			},
		},
		{
			name: "a different module's path that merely shares a prefix segment is external",
			pkg:  "example.com/proj/leaf",
			deps: []string{
				"example.com/proj/leaf",
				"example.com/projsomethingelse/internal/x", // not under "example.com/proj/"
			},
			allowed: nil,
			want:    nil,
		},
		{
			name: "a repeated violation is collapsed to a single entry",
			pkg:  "example.com/proj/leaf",
			deps: []string{
				"example.com/proj/leaf",
				"example.com/proj/internal/cost",
				"example.com/proj/internal/cost", // duplicate — must not double-report
			},
			allowed: nil,
			want:    []string{"example.com/proj/internal/cost"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := ForbiddenInternalImports(mod, tt.pkg, tt.deps, tt.allowed)
			assert.Equal(t, tt.want, got)
		})
	}
}

// TestExtractableGoPackagesStayLeaf is the live gate: every package in the
// guarded set must depend on no non-extractable in-module package on the
// current tree. It runs in the existing `go test ./internal/...` CI lane, so a
// PR that makes a guarded package reach up into orchestrator-internal (BUSL)
// code fails without any extra CI wiring.
//
// RFC 0045 §B seeds the Go gate only on `internal/generated/walletpb` — the
// genuinely-leaf, published MIT wire contract. `internal/wallet` is NOT seeded:
// it stays BUSL as the reference server (RFC 0046 §D) and, on the current tree,
// transitively imports `internal/executor/packaging` via `internal/cost`, so it
// is not leaf. It joins the gate only once that engine/metrics split lands
// (RFC 0045 §B; deferred to RFC 0023's extraction).
func TestExtractableGoPackagesStayLeaf(t *testing.T) {
	// Non-vacuity guard. A `for range` over an empty map runs zero subtests and
	// the parent test still reports PASS — so an emptied or renamed guarded set
	// (a future refactor commenting out the walletpb entry, or a bad merge) would
	// leave this gate silently toothless while `go test ./internal/...` stays
	// green. Assert the set is populated and still seeds the walletpb leaf before
	// looping. This mirrors the Python half's test_contract_covers_every_mit_candidate
	// (which pins source_modules against an independent expected set, RFC 0045 §B);
	// without it the two halves are asymmetric and only the Python gate is
	// protected against a silently-dropped guarded entry.
	require.NotEmpty(t, ExtractableGoPackages,
		"the Go MIT↛BUSL guarded set is empty — the gate would be vacuous (RFC 0045 §B)")
	require.Contains(t, ExtractableGoPackages, ModulePath+"/internal/generated/walletpb",
		"the seeded walletpb leaf must stay in the guarded set (RFC 0045 §B)")

	for pkg, allowed := range ExtractableGoPackages {
		t.Run(pkg, func(t *testing.T) {
			deps := goListDeps(t, pkg)
			bad := ForbiddenInternalImports(ModulePath, pkg, deps, allowed)
			assert.Emptyf(t, bad, "%s must stay leaf (RFC 0045 §B MIT↛BUSL gate) but "+
				"transitively imports non-extractable in-module packages: %v\n"+
				"Extracting this package would pull BUSL/orchestrator code under its "+
				"license. Break the new dependency, or move the package out of the "+
				"guarded set with a documented reason.", pkg, bad)
		})
	}
}

// goListDeps returns the full transitive dependency list of pkg via the Go
// toolchain — the same import graph the compiler sees.
func goListDeps(t *testing.T, pkg string) []string {
	t.Helper()
	out, err := exec.Command("go", "list", "-deps", "-f", "{{.ImportPath}}", pkg).Output()
	require.NoErrorf(t, err, "go list -deps %s failed", pkg)
	var deps []string
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		if line = strings.TrimSpace(line); line != "" {
			deps = append(deps, line)
		}
	}
	return deps
}
