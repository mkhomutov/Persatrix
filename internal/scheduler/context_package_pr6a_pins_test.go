package scheduler

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/executor/packaging"
	"github.com/mkhomutov/persatrix/internal/planner"
)

// Pin tests for RFC 0008 PR 6a (Go scheduler hygiene). Split into its own
// file so the existing suites stay under the 500-line `scripts/checks/file_size.go`
// soft cap and so the PR 6a contracts can be located by reviewers without
// scanning the older PR-1/PR-1b suites.

// TestAttachContextPackage_DeterministicCandidateOrder pins the M6 fix:
// `attachContextPackage` sorts the input map keys before constructing
// `packaging.Candidate`s, so two identical workloads produce byte-identical
// packages even though Go's `range map` iteration order is randomised. With
// >= 5 outputs of equal density the prior unsorted version had a vanishingly
// small chance of producing the same StepOutputs ordering twice in a row.
func TestAttachContextPackage_DeterministicCandidateOrder(t *testing.T) {
	s := &WorkflowScheduler{
		logger:   zap.NewNop(),
		packager: packaging.NewPackager(nil),
	}
	step := planner.Step{ID: "s1"}
	mkOutputs := func() map[string]string {
		// Five outputs of identical content => identical density. Without
		// the M6 sort, ranging the map produces non-deterministic candidate
		// order and `Packager.Build`'s tie-break-on-input-order contract
		// then surfaces the randomness in `pkg.StepOutputs`.
		return map[string]string{
			"alpha":   "same content",
			"bravo":   "same content",
			"charlie": "same content",
			"delta":   "same content",
			"echo":    "same content",
		}
	}

	pkgA, err := s.attachContextPackage(mkOutputs(), "run-1", step, "input", 1000)
	require.NoError(t, err)
	pkgB, err := s.attachContextPackage(mkOutputs(), "run-1", step, "input", 1000)
	require.NoError(t, err)

	require.Len(t, pkgA.StepOutputs, 5)
	require.Len(t, pkgB.StepOutputs, 5)
	idsA := make([]string, 0, 5)
	idsB := make([]string, 0, 5)
	for i := range pkgA.StepOutputs {
		idsA = append(idsA, pkgA.StepOutputs[i].ID)
		idsB = append(idsB, pkgB.StepOutputs[i].ID)
	}
	assert.Equal(t, idsA, idsB,
		"candidate IDs must order identically across attaches with identical inputs")
	assert.Equal(t, []string{"alpha", "bravo", "charlie", "delta", "echo"}, idsA,
		"sort order is lexicographic on the candidate ID (slices.Sorted on map keys)")
}

// TestPackager_BuildClampsNegativeBudgetToZero pins the L9 contract:
// `Packager.Build`'s `remaining < 0 → 0` clamp covers a negative
// `budgetTokens` argument too. The allocator never emits negatives today,
// but PR 2's `MemoryFacade` may compute `B_step − B_memory_reservation`
// and underflow.
func TestPackager_BuildClampsNegativeBudgetToZero(t *testing.T) {
	p := packaging.NewPackager(nil)
	candidates := []packaging.Candidate{
		{ID: "a", Content: "alpha", Tokens: 5, Importance: 0.5},
		{ID: "b", Content: "bravo", Tokens: 5, Importance: 0.5},
	}
	pkg := p.Build(candidates, packaging.QueryContext{StepID: "s"}, -100)
	assert.Empty(t, pkg.StepOutputs,
		"a negative budget must admit nothing (the clamp prevents the negative remainder from ever satisfying a candidate's token cost)")
	assert.Equal(t, 0, pkg.Metrics.TokensAfter)
}

// TestContextPackage_WireShape_OmitemptyWarnings pins the L10 contract:
// when `packaging.Metrics.Warnings` is empty the JSON wire shape encodes
// `metrics.warnings` as an empty list (or omits it entirely under
// omitempty). A future tag flip from `omitempty` to a default-encoded
// nullable would surface here before Python-side decoders break.
func TestContextPackage_WireShape_OmitemptyWarnings(t *testing.T) {
	s := &WorkflowScheduler{
		logger:   zap.NewNop(),
		packager: packaging.NewPackager(nil),
	}
	outputs := map[string]string{"a": "tiny payload"}
	pkg, err := s.attachContextPackage(outputs, "run-1", planner.Step{ID: "s1"}, "input", 1000)
	require.NoError(t, err)
	require.Empty(t, pkg.Metrics.Warnings, "tiny payload under generous budget emits no warnings")

	encoded, err := json.Marshal(pkg)
	require.NoError(t, err)
	var decoded map[string]any
	require.NoError(t, json.Unmarshal(encoded, &decoded))
	metrics, ok := decoded["metrics"].(map[string]any)
	require.True(t, ok)
	if raw, present := metrics["warnings"]; present {
		// Present-but-empty is also acceptable (the omitempty tag may be
		// removed in the future). The contract is "if present, decode as
		// an empty list, never null".
		warnings, ok := raw.([]any)
		require.True(t, ok, "warnings must decode as a JSON array, not null")
		assert.Empty(t, warnings)
	}
}

// TestWarningSampler_PruneRunDropsBucket pins the M11 contract: after
// `pruneRun(execID)` a fresh `shouldEmit` for the same (run, step,
// warning) tuple emits again, because the per-run bucket has been
// dropped. This is the bookkeeping that prevents unbounded growth of the
// sampler across long-running orchestrators.
func TestWarningSampler_PruneRunDropsBucket(t *testing.T) {
	var sampler warningSampler

	require.True(t, sampler.shouldEmit("run-1", "s1", "high_compression_ratio"))
	require.False(t, sampler.shouldEmit("run-1", "s1", "high_compression_ratio"),
		"second emission within the same run is suppressed")

	sampler.pruneRun("run-1")

	require.True(t, sampler.shouldEmit("run-1", "s1", "high_compression_ratio"),
		"after pruneRun the per-run bucket is empty so the next emission fires again")
}

// TestWarningSampler_PruneRun_NoOpForUnknownRun guards the documented
// no-op behaviour for runs that never emitted a sampled warning.
func TestWarningSampler_PruneRun_NoOpForUnknownRun(t *testing.T) {
	var sampler warningSampler
	// Must not panic.
	sampler.pruneRun("never-saw-this-run")
}

// TestEstimateTokens_RuneCountForMultibyte pins the L7 fix: byte-count
// inflated estimates 2–4× for multibyte UTF-8 strings; rune-count returns
// the codepoint-aligned figure so density ordering is calibrated against a
// stable per-character token estimate regardless of script.
func TestEstimateTokens_RuneCountForMultibyte(t *testing.T) {
	asciiOnly := strings.Repeat("a", 16) // 16 runes, 16 bytes
	cjk := strings.Repeat("漢", 16)       // 16 runes, 48 bytes
	assert.Equal(t, estimateTokens(asciiOnly), estimateTokens(cjk),
		"rune-count makes per-character token estimates script-independent")
}

// TestImportanceForCandidate_Uniform pins the M9 fix: the scheduler emits
// a uniform 0.5 hint and the heuristic scorer owns dependency proximity
// entirely, eliminating the prior double-counting (importance boost +
// scorer's depWeight).
func TestImportanceForCandidate_Uniform(t *testing.T) {
	assert.Equal(t, 0.5, importanceForCandidate())
}
