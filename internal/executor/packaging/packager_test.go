package packaging

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- RFC 0008 PR 1: greedy knapsack + pinned + metrics ---

func TestPackager_Build_AdmitsHighestDensityFirst(t *testing.T) {
	p := NewPackager(nil)
	candidates := []Candidate{
		// Equal score (importance only) but different sizes — smaller wins per density.
		{ID: "big", Content: "x", Tokens: 100, Importance: 0.5},
		{ID: "small", Content: "y", Tokens: 10, Importance: 0.5},
	}
	pkg := p.Build(candidates, QueryContext{StepID: "s1"}, 50)
	require.Len(t, pkg.StepOutputs, 1)
	assert.Equal(t, "small", pkg.StepOutputs[0].ID)
	assert.Equal(t, 1, pkg.Metrics.CandidatesDropped)
}

func TestPackager_Build_PinnedAlwaysAdmitted(t *testing.T) {
	p := NewPackager(nil)
	candidates := []Candidate{
		{ID: "pin", Content: "must-keep", Tokens: 200, Pinned: true, Importance: 0.1},
		{ID: "comp", Content: "lose", Tokens: 10, Importance: 0.9},
	}
	pkg := p.Build(candidates, QueryContext{StepID: "s1"}, 50)
	require.Len(t, pkg.PinnedSections, 1)
	// Budget 50 < pinned 200 → pinned_overflow warning, no competing admitted.
	assert.Contains(t, pkg.Metrics.Warnings, PinnedOverflowKey)
}

func TestPackager_Build_DeterministicTieBreak(t *testing.T) {
	p := NewPackager(nil)
	// Same density (same tokens, same importance, no overlap, no dep) — sort
	// must break ties on ID lexicographic.
	candidates := []Candidate{
		{ID: "z", Content: "a", Tokens: 10, Importance: 0.5},
		{ID: "a", Content: "b", Tokens: 10, Importance: 0.5},
	}
	pkg := p.Build(candidates, QueryContext{StepID: "s1"}, 10)
	require.Len(t, pkg.StepOutputs, 1)
	assert.Equal(t, "a", pkg.StepOutputs[0].ID, "lexicographic tie-break should pick 'a'")
}

func TestPackager_Build_PreservesInputOrder(t *testing.T) {
	p := NewPackager(nil)
	candidates := []Candidate{
		{ID: "b", Content: "x", Tokens: 10, Importance: 0.5},
		{ID: "a", Content: "y", Tokens: 10, Importance: 0.5},
		{ID: "c", Content: "z", Tokens: 10, Importance: 0.5},
	}
	pkg := p.Build(candidates, QueryContext{StepID: "s1"}, 100)
	require.Len(t, pkg.StepOutputs, 3)
	// Output order matches input order, not score order.
	assert.Equal(t, []string{"b", "a", "c"}, []string{
		pkg.StepOutputs[0].ID, pkg.StepOutputs[1].ID, pkg.StepOutputs[2].ID,
	})
}

func TestPackager_Build_HighCompressionWarning(t *testing.T) {
	p := NewPackager(nil)
	// Budget admits 1 of 5 same-sized candidates → ratio 5:1 → high warning.
	candidates := []Candidate{
		{ID: "a", Content: "x", Tokens: 100, Importance: 0.5},
		{ID: "b", Content: "x", Tokens: 100, Importance: 0.5},
		{ID: "c", Content: "x", Tokens: 100, Importance: 0.5},
		{ID: "d", Content: "x", Tokens: 100, Importance: 0.5},
		{ID: "e", Content: "x", Tokens: 100, Importance: 0.5},
	}
	pkg := p.Build(candidates, QueryContext{StepID: "s1"}, 100)
	assert.Equal(t, 5.0, pkg.Metrics.CompressionRatio)
	assert.Contains(t, pkg.Metrics.Warnings, "high_compression_ratio")
}

func TestPackager_Build_ExtremeCompressionCapped(t *testing.T) {
	p := NewPackager(nil)
	// 15 candidates × 100 tokens, budget 100 → ratio would be 15.0, cap at 10.0.
	candidates := make([]Candidate, 15)
	for i := range candidates {
		candidates[i] = Candidate{ID: string(rune('a' + i)), Content: "x", Tokens: 100, Importance: 0.5}
	}
	pkg := p.Build(candidates, QueryContext{StepID: "s1"}, 100)
	assert.Equal(t, ExtremeCompressionCap, pkg.Metrics.CompressionRatio)
	assert.Contains(t, pkg.Metrics.Warnings, "extreme_compression_capped")
}

func TestPackager_Build_NoCompressionWhenAllFit(t *testing.T) {
	p := NewPackager(nil)
	candidates := []Candidate{
		{ID: "a", Content: "x", Tokens: 10, Importance: 0.5},
		{ID: "b", Content: "y", Tokens: 10, Importance: 0.5},
	}
	pkg := p.Build(candidates, QueryContext{StepID: "s1"}, 100)
	assert.Equal(t, 1.0, pkg.Metrics.CompressionRatio)
	assert.Empty(t, pkg.Metrics.Warnings)
	assert.Equal(t, 0, pkg.Metrics.CandidatesDropped)
}

func TestPackager_Build_EmptyCandidates(t *testing.T) {
	p := NewPackager(nil)
	pkg := p.Build(nil, QueryContext{StepID: "s1"}, 100)
	assert.Equal(t, 1.0, pkg.Metrics.CompressionRatio)
	assert.Empty(t, pkg.StepOutputs)
	assert.Empty(t, pkg.PinnedSections)
}

func TestPackager_Build_DependencyBoostWinsAdmission(t *testing.T) {
	p := NewPackager(nil)
	// Two same-token candidates; one is in DependsOn → should win when budget admits one.
	candidates := []Candidate{
		{ID: "unrelated", Content: "x", Tokens: 50, Importance: 0.5},
		{ID: "depped", Content: "y", Tokens: 50, Importance: 0.5},
	}
	pkg := p.Build(candidates, QueryContext{StepID: "s1", DependsOn: []string{"depped"}}, 50)
	require.Len(t, pkg.StepOutputs, 1)
	assert.Equal(t, "depped", pkg.StepOutputs[0].ID)
}

func TestPackager_Build_VersionAndJSONRoundTrip(t *testing.T) {
	p := NewPackager(nil)
	pkg := p.Build([]Candidate{
		{ID: "a", Content: "hello world", Tokens: 5, Importance: 0.7},
	}, QueryContext{StepID: "s1", Input: "hello"}, 100)

	assert.Equal(t, PackageVersion, pkg.Version)
	assert.Equal(t, 0, pkg.BudgetMemoryTokens, "PR 1 emits 0; PR 2 wires non-zero")

	// Wire-shape contract — round-trip through JSON without loss.
	encoded, err := json.Marshal(pkg)
	require.NoError(t, err)
	var decoded Package
	require.NoError(t, json.Unmarshal(encoded, &decoded))
	assert.Equal(t, pkg.Version, decoded.Version)
	assert.Equal(t, pkg.StepOutputs[0].ID, decoded.StepOutputs[0].ID)
	assert.Equal(t, pkg.Metrics.CompressionRatio, decoded.Metrics.CompressionRatio)
}

// --- HeuristicScorer focused tests ---

func TestHeuristicScorer_DependencyBoost(t *testing.T) {
	s := NewHeuristicScorer()
	c := Candidate{ID: "x", Content: "irrelevant"}
	q := QueryContext{Input: "totally different", DependsOn: []string{"x"}}
	got := s.Score(c, q)
	assert.InDelta(t, 0.4, got, 1e-9, "dependency boost contributes 0.4")
}

func TestHeuristicScorer_LexicalOverlap(t *testing.T) {
	s := NewHeuristicScorer()
	c := Candidate{ID: "x", Content: "apple banana cherry"}
	q := QueryContext{Input: "apple cherry"}
	got := s.Score(c, q)
	// Jaccard({apple,banana,cherry}, {apple,cherry}) = 2/3
	// score = 0.4*0 + 0 (not depended) + 0.2 * 2/3 ≈ 0.1333
	assert.InDelta(t, 0.2*2.0/3.0, got, 1e-9)
}

func TestHeuristicScorer_ImportanceClamped(t *testing.T) {
	s := NewHeuristicScorer()
	got := s.Score(Candidate{ID: "x", Importance: 1.5}, QueryContext{})
	assert.InDelta(t, 0.4, got, 1e-9, "importance > 1 clamps to 1")
}
