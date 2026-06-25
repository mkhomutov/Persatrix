// Package archpolicy enforces the RFC 0045 open-core dependency-direction
// invariant on the Go side: imports may only point down-tier
// (MIT ← BUSL ← Private). A package intended to be extractable into a permissive
// (MIT) standalone repository must never import a non-extractable
// orchestrator-internal (BUSL) package — otherwise the next mirror/release would
// ship BUSL-licensed source under the extracted repo's grant, a licensing
// violation introduced by a one-line diff (RFC 0045 §M-4).
//
// The Go half of the gate is forward-looking. The flagship budget-lease
// extraction resolved its MIT artifact to a single-language Python library, so
// the Go `internal/cost`/`internal/wallet` packages stay BUSL as the reference
// server (RFC 0046 §D). The only genuinely-leaf, published MIT contract on the
// Go side today is the generated wallet proto package, so that is all the gate
// guards; the registry below is the seam to add future MIT Go packages to.
//
// Enforcement lives in dependency_direction_test.go, which runs in the existing
// `go test ./internal/...` CI lane — no separate lint stage or config file.
package archpolicy

import "sort"

// ModulePath is this repository's Go module path. In-module dependencies share
// it as a prefix; everything else (stdlib, third-party SDKs) does not.
const ModulePath = "github.com/mkhomutov/persatrix"

// ExtractableGoPackages maps each guarded leaf package to the in-module import
// paths it is permitted to depend on. RFC 0045 §B seeds the gate only on
// packages that are already leaf on `main`.
//
//   - internal/generated/walletpb — the generated wallet proto stubs: the
//     published MIT wire contract (RFC 0045 §F / RFC 0046 §D). Leaf today
//     (imports only the protobuf runtime), so its allow-list is empty.
//
// internal/wallet is deliberately absent: it stays BUSL as the reference server
// and transitively imports internal/executor/packaging via internal/cost on the
// current tree, so it is not leaf. It joins the gate only once that split lands
// (RFC 0045 §B).
var ExtractableGoPackages = map[string][]string{
	ModulePath + "/internal/generated/walletpb": {},
}

// ForbiddenInternalImports returns the subset of deps that violate the
// dependency-direction invariant for pkg: in-module packages (sharing
// modulePath as a path prefix) that are neither pkg itself nor on its allowed
// list. deps is the full transitive dependency list (as produced by
// `go list -deps`), so stdlib and third-party entries — which do not share the
// module prefix — are ignored. The result is sorted for stable reporting.
func ForbiddenInternalImports(modulePath, pkg string, deps, allowed []string) []string {
	allowedSet := make(map[string]bool, len(allowed))
	for _, a := range allowed {
		allowedSet[a] = true
	}

	inModulePrefix := modulePath + "/"
	var bad []string
	for _, dep := range deps {
		switch {
		case dep == pkg: // the package itself
			continue
		case allowedSet[dep]: // an explicitly-permitted leaf dependency
			continue
		case len(dep) > len(inModulePrefix) && dep[:len(inModulePrefix)] == inModulePrefix:
			bad = append(bad, dep)
		}
	}

	sort.Strings(bad)
	return bad
}
