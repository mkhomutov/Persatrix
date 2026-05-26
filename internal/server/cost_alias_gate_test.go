package server

import (
	"encoding/json"
	"net/http"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
)

// TestCostSummary_AliasRoutedAgent_ReportsNonZeroCost is the RFC 0033 PR 5
// cost-attribution gate (master-plan §Acceptance): an alias-routed agent
// reports correctly-keyed, NON-ZERO cost via GET /api/v1/cost/summary.
//
// It loads the real shipped config so the alias-derived cost.pricing.models
// block (RFC 0033 §F) is exercised end to end. The usage is keyed by the
// physical model the `quality` alias resolves to (claude-sonnet-4-6) — the
// value the Python agent sends on LeaseRequest.model for an alias-routed agent.
// Before PR 5 this read $0 because the pricing table still keyed the retired
// claude-sonnet-4-20250514.
func TestCostSummary_AliasRoutedAgent_ReportsNonZeroCost(t *testing.T) {
	cfg, err := cost.LoadCostConfig(filepath.Join("..", "..", "config"))
	require.NoError(t, err)

	counter := cost.NewTokenCounter(cfg, zap.NewNop())
	reporter := cost.NewCostReporter(counter, cfg, zap.NewNop())

	counter.RecordUsage(cost.UsageRecord{
		WorkflowID:   "wf-1",
		AgentID:      "planner",
		Model:        "claude-sonnet-4-6", // physical id behind the `quality` alias
		InputTokens:  1000,
		OutputTokens: 500,
	})

	srv := testServerWithCost(t, reporter)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/cost/summary", nil)
	require.Equal(t, http.StatusOK, rec.Code)

	var resp cost.GlobalCostSummary
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))

	assert.Greater(t, resp.DailyEstimatedUSD, 0.0,
		"alias-routed cost must not regress to $0 across the re-keying")
	require.Len(t, resp.TopAgents, 1)
	assert.Equal(t, "planner", resp.TopAgents[0].AgentID)
	assert.Greater(t, resp.TopAgents[0].EstimatedUSD, 0.0)
}
