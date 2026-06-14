package registry

import (
	"context"
	"strconv"
	"testing"
)

// Benchmarks backing the ISSUE-0100 decision: the channel mention lift needs the
// display names of a *handful* of channel members, but the old wiring read them
// off a whole-directory List+sort. These compare the two read shapes on a large
// synthetic directory with a small member set — the exact "registry grows hot"
// case the issue is forward-looking about — to confirm the scoped NamesFor is a
// strict improvement (not just a wash) before the lift relies on it.
//
// Run: go test ./internal/registry/ -run x -bench NameLookup -benchmem
// (the filter must match BOTH benchmarks below — "NamesFor" alone would skip the
// whole-directory baseline and report only half the comparison.)
//
// The List path measures the real thing: it calls the production List, so its
// numbers already include deep-copying every agent's Capabilities slice — the
// dominant cost (the ~5000 allocs/op are those per-agent slice copies), exactly
// the work NamesFor skips. No estimation; the deep copy is in the figure.

const benchDirectorySize = 5000 // a large multi-tenant-ish directory
const benchChannelMembers = 6   // a typical small channel

func benchRegistry(tb testing.TB) (*InMemoryRegistry, []string) {
	tb.Helper()
	r := newTestRegistry()
	ctx := context.Background()
	for i := 0; i < benchDirectorySize; i++ {
		id := "agent-" + strconv.Itoa(i)
		if err := r.Register(ctx, sampleAgent(id)); err != nil {
			tb.Fatalf("register %s: %v", id, err)
		}
	}
	// Members scattered across the directory so neither path benefits from locality.
	members := make([]string, benchChannelMembers)
	for i := range members {
		members[i] = "agent-" + strconv.Itoa(i*(benchDirectorySize/benchChannelMembers))
	}
	return r, members
}

// BenchmarkNameLookup_WholeDirectoryList is the pre-ISSUE-0100 shape: List the
// whole directory, then build an id→name map and read off the members' names.
func BenchmarkNameLookup_WholeDirectoryList(b *testing.B) {
	r, members := benchRegistry(b)
	ctx := context.Background()
	b.ResetTimer()
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		agents, err := r.List(ctx)
		if err != nil {
			b.Fatal(err)
		}
		names := make(map[string]string, len(agents))
		for _, a := range agents {
			names[a.ID] = a.Name
		}
		var sink string
		for _, id := range members {
			sink = names[id]
		}
		_ = sink
	}
}

// BenchmarkNameLookup_ScopedNamesFor is the post-ISSUE-0100 shape: one scoped
// read of just the members' names.
func BenchmarkNameLookup_ScopedNamesFor(b *testing.B) {
	r, members := benchRegistry(b)
	ctx := context.Background()
	b.ResetTimer()
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		names, err := r.NamesFor(ctx, members)
		if err != nil {
			b.Fatal(err)
		}
		var sink string
		for _, id := range members {
			sink = names[id]
		}
		_ = sink
	}
}
