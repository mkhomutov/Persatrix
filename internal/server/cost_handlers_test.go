package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// testServerWithCost creates a Server with a CostReporter wired in.
func testServerWithCost(t *testing.T, reporter *cost.CostReporter) *Server {
	t.Helper()
	dir := t.TempDir()
	logger := zap.NewNop()
	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)
	srv, err := New("127.0.0.1:0", dir, store, reg, pl, logger, WithCostReporter(reporter))
	require.NoError(t, err)
	return srv
}

func TestCostSummary_NoCostReporter_Returns503(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/cost/summary", nil)
	assert.Equal(t, http.StatusServiceUnavailable, rec.Code)

	var resp errorResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "SERVICE_UNAVAILABLE", resp.Code)
	assert.Contains(t, resp.Error, "cost tracking is not configured")
}

func TestCostSummary_EmptyState_ReturnsZeroTotals(t *testing.T) {
	cfg := &cost.CostConfig{
		Pricing: map[string]cost.ModelPricing{
			"claude-sonnet": {InputPer1MTokens: 3.0, OutputPer1MTokens: 15.0},
		},
	}
	counter := cost.NewTokenCounter(cfg, zap.NewNop())
	reporter := cost.NewCostReporter(counter, cfg, zap.NewNop())

	srv := testServerWithCost(t, reporter)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/cost/summary", nil)
	assert.Equal(t, http.StatusOK, rec.Code)

	var resp cost.GlobalCostSummary
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, int64(0), resp.DailyInputTokens)
	assert.Equal(t, int64(0), resp.DailyOutputTokens)
	assert.InDelta(t, 0.0, resp.DailyEstimatedUSD, 1e-9)
	assert.Empty(t, resp.TopAgents)
	assert.False(t, resp.ReportedAt.IsZero())
}

func TestCostSummary_WithUsage_ReturnsCorrectTotals(t *testing.T) {
	cfg := &cost.CostConfig{
		Pricing: map[string]cost.ModelPricing{
			"claude-sonnet": {InputPer1MTokens: 3.0, OutputPer1MTokens: 15.0},
		},
	}
	counter := cost.NewTokenCounter(cfg, zap.NewNop())
	reporter := cost.NewCostReporter(counter, cfg, zap.NewNop())

	// Record some usage.
	counter.RecordUsage(cost.UsageRecord{
		WorkflowID:   "wf-1",
		AgentID:      "agent-a",
		Model:        "claude-sonnet",
		InputTokens:  1000,
		OutputTokens: 500,
	})

	srv := testServerWithCost(t, reporter)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/cost/summary", nil)
	assert.Equal(t, http.StatusOK, rec.Code)

	var resp cost.GlobalCostSummary
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, int64(1000), resp.DailyInputTokens)
	assert.Equal(t, int64(500), resp.DailyOutputTokens)
	// 1000/1M * 3.0 + 500/1M * 15.0 = 0.003 + 0.0075 = 0.0105
	assert.InDelta(t, 0.0105, resp.DailyEstimatedUSD, 1e-9)
	require.Len(t, resp.TopAgents, 1)
	assert.Equal(t, "agent-a", resp.TopAgents[0].AgentID)
	assert.True(t, resp.ReportedAt.Before(time.Now().Add(time.Second)))
}

func TestCostSummary_ContentType(t *testing.T) {
	cfg := &cost.CostConfig{
		Pricing: map[string]cost.ModelPricing{},
	}
	counter := cost.NewTokenCounter(cfg, zap.NewNop())
	reporter := cost.NewCostReporter(counter, cfg, zap.NewNop())

	srv := testServerWithCost(t, reporter)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/cost/summary", nil)
	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Contains(t, rec.Result().Header.Get("Content-Type"), "application/json")
}

func TestCostSummary_MethodNotAllowed(t *testing.T) {
	cfg := &cost.CostConfig{
		Pricing: map[string]cost.ModelPricing{},
	}
	counter := cost.NewTokenCounter(cfg, zap.NewNop())
	reporter := cost.NewCostReporter(counter, cfg, zap.NewNop())

	srv := testServerWithCost(t, reporter)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/cost/summary", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	// Go 1.22 ServeMux returns 405 for wrong method.
	assert.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}
