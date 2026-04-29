package cost

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/executor/packaging"
)

// TestNewContextPackageMetrics verifies the helper copies fields out of
// the supplied packaging.Package and stamps the derived
// `candidates_admitted` count from `len(pkg.StepOutputs)`. RFC 0008 PR 1b
// — admitted is intentionally cost-record-only (the wire shape derives it
// from len(step_outputs)). PR 6a / N9 changed the signature to take the
// package directly so callers cannot miswire the admitted count.
func TestNewContextPackageMetrics(t *testing.T) {
	t.Run("nil package returns nil", func(t *testing.T) {
		assert.Nil(t, NewContextPackageMetrics(nil))
	})
	t.Run("happy path copies fields", func(t *testing.T) {
		pkg := &packaging.Package{
			StepOutputs: []packaging.AdmittedSection{
				{ID: "a"}, {ID: "b"}, {ID: "c"}, {ID: "d"},
			},
			Metrics: packaging.Metrics{
				TokensBefore:      200,
				TokensAfter:       150,
				CompressionRatio:  1.33,
				CandidatesDropped: 2,
			},
		}
		got := NewContextPackageMetrics(pkg)
		require.NotNil(t, got)
		assert.Equal(t, 200, got.TokensBefore)
		assert.Equal(t, 150, got.TokensAfter)
		assert.Equal(t, 1.33, got.CompressionRatio)
		assert.Equal(t, 4, got.CandidatesAdmitted, "admitted == len(pkg.StepOutputs)")
		assert.Equal(t, 2, got.CandidatesDropped)
	})
}

// TestStepCostEntry_ContextPackageOmitemptyShape confirms the cost endpoint
// JSON shape stays backward-compatible: when a step did not opt into
// context packaging, ContextPackage stays nil and `omitempty` strips it
// entirely. Pre-PR-1b consumers therefore see no shape change.
func TestStepCostEntry_ContextPackageOmitemptyShape(t *testing.T) {
	t.Run("nil ContextPackage omitted", func(t *testing.T) {
		entry := StepCostEntry{
			StepID: "s1", AgentID: "a1", Model: "m", InputTokens: 1, OutputTokens: 2, EstimatedUSD: 0.001,
		}
		raw, err := json.Marshal(entry)
		require.NoError(t, err)
		assert.NotContains(t, string(raw), "context_package",
			"omitempty must drop the field for legacy steps")
	})
	t.Run("non-nil ContextPackage emitted", func(t *testing.T) {
		entry := StepCostEntry{
			StepID: "s1", AgentID: "a1", Model: "m", InputTokens: 1, OutputTokens: 2,
			ContextPackage: &ContextPackageMetrics{
				TokensBefore: 100, TokensAfter: 50, CompressionRatio: 2.0,
				CandidatesAdmitted: 3, CandidatesDropped: 1,
			},
		}
		raw, err := json.Marshal(entry)
		require.NoError(t, err)
		assert.Contains(t, string(raw), `"context_package":`)
		assert.Contains(t, string(raw), `"candidates_admitted":3`)
		assert.Contains(t, string(raw), `"candidates_dropped":1`)
	})
}
