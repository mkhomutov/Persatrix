package server

// The v0.3.14 origin-set pin (ISSUE-0082 Part 2 PR 2).
//
// The plan's top-ranked risk is a MISSED dispatch origin: it fails open — no
// error, no red test, just a silent collapse to the shared `'local'` tenant.
// principal.go removes the fail-open hazard structurally (the principal is
// threaded once, in the middleware that wraps the root mux), but "which routes
// put a principal on the wire?" still has to be a stated, reviewed answer
// rather than a reader's inference. [dispatchOriginClassification] is that
// answer; this file is what stops it going stale.
//
// The test parses THIS PACKAGE'S OWN SOURCE for mux registrations rather than
// introspecting a built server, because `net/http.ServeMux` exposes no way to
// enumerate its patterns and a hand-listed fixture would just be a second
// table to forget. Adding a route without classifying it is therefore
// compile-green and test-RED — the inversion the plan asked for.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// registeredRoutePatterns returns every method-qualified mux pattern the
// non-test sources of this package register, via `mux.Handle` / `HandleFunc`
// (any receiver: `s.mux`, the bare `mux` params in auth_handlers.go and ui.go,
// and the `root` mux in server.go).
//
// Only patterns whose first token is an HTTP method are collected. The
// catch-all `root.Handle("/", apiH)` is a mount, not a route — it delegates to
// the API mux whose own patterns are already collected — and the policy mux's
// parallel `reg(...)` calls in auth_policy.go use a different helper, so
// neither pollutes the set.
func registeredRoutePatterns(t *testing.T) map[string]string {
	t.Helper()
	entries, err := os.ReadDir(".")
	require.NoError(t, err)

	methods := map[string]bool{
		"GET": true, "POST": true, "PUT": true, "PATCH": true,
		"DELETE": true, "HEAD": true, "OPTIONS": true,
	}
	found := make(map[string]string) // pattern -> file it was registered in
	fset := token.NewFileSet()
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(fset, filepath.Join(".", name), nil, 0)
		require.NoError(t, err, "parsing %s", name)

		ast.Inspect(file, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok || len(call.Args) == 0 {
				return true
			}
			sel, ok := call.Fun.(*ast.SelectorExpr)
			if !ok || (sel.Sel.Name != "Handle" && sel.Sel.Name != "HandleFunc") {
				return true
			}
			lit, ok := call.Args[0].(*ast.BasicLit)
			if !ok || lit.Kind != token.STRING {
				return true
			}
			pattern, err := strconv.Unquote(lit.Value)
			if err != nil {
				return true
			}
			if method, _, hasMethod := strings.Cut(pattern, " "); hasMethod && methods[method] {
				found[pattern] = name
			}
			return true
		})
	}
	require.NotEmpty(t, found, "route scan found nothing — the AST walk has drifted from how routes are registered")
	return found
}

// TestPrincipalOriginTableCoversEveryRoute is the plan's route-table gate:
// every registered route carries a verdict, and every verdict names a live
// route. A new endpoint fails here until its author decides — and records —
// whether it puts an authenticated principal on the wire.
func TestPrincipalOriginTableCoversEveryRoute(t *testing.T) {
	registered := registeredRoutePatterns(t)

	var unclassified []string
	for pattern, file := range registered {
		if _, ok := dispatchOriginClassification[pattern]; !ok {
			unclassified = append(unclassified, pattern+"  (registered in "+file+")")
		}
	}
	sort.Strings(unclassified)
	assert.Empty(t, unclassified,
		"every dispatch-originating route must be classified in dispatchOriginClassification "+
			"(principal.go). A route left out fails OPEN — it silently writes persona memory "+
			"under the shared 'local' tenant. Classify it as originPrincipalBearing if it can "+
			"reach channels.GRPCMessageDispatcher.Dispatch, originNonDispatching otherwise:\n  "+
			strings.Join(unclassified, "\n  "))

	var stale []string
	for pattern := range dispatchOriginClassification {
		if _, ok := registered[pattern]; !ok {
			stale = append(stale, pattern)
		}
	}
	sort.Strings(stale)
	assert.Empty(t, stale,
		"dispatchOriginClassification names routes this package no longer registers — "+
			"a renamed or removed endpoint left a verdict behind:\n  "+strings.Join(stale, "\n  "))
}

// TestPrincipalBearingRoutesAreExactlyTheDispatchOrigins pins the three
// origins by name. The exhaustiveness test above cannot catch a route that is
// classified but classified WRONG, and the two failure directions differ in
// kind: a dispatch origin mislabelled originNonDispatching is the fail-open
// defect itself, while a read route mislabelled originPrincipalBearing is a
// documentation lie a reviewer would act on. Both are caught by naming the
// set, so any change to it is a deliberate edit here.
func TestPrincipalBearingRoutesAreExactlyTheDispatchOrigins(t *testing.T) {
	var bearing []string
	for pattern, origin := range dispatchOriginClassification {
		if origin == originPrincipalBearing {
			bearing = append(bearing, pattern)
		}
	}
	sort.Strings(bearing)
	assert.Equal(t, []string{
		"POST /api/v1/agents/{id}/chat",
		"POST /api/v1/channels/{id}/convene",
		"POST /api/v1/channels/{id}/messages",
	}, bearing)
}
