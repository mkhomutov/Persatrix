package cost

import (
	"fmt"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

func TestTokenCounter_RecordUsage_PerWorkflow(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})

	input, output, usd := tc.WorkflowUsage("wf-1")
	assert.Equal(t, int64(1000), input)
	assert.Equal(t, int64(500), output)
	assert.InDelta(t, 0.0105, usd, 1e-9)
}

func TestTokenCounter_RecordUsage_PerAgent(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 2000, OutputTokens: 1000,
	})

	input, output, usd := tc.AgentUsage("agent-a")
	assert.Equal(t, int64(2000), input)
	assert.Equal(t, int64(1000), output)
	assert.InDelta(t, 0.021, usd, 1e-9)
}

func TestTokenCounter_RecordUsage_GlobalDaily(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-2", AgentID: "agent-b", Model: "claude-haiku",
		InputTokens: 2000, OutputTokens: 1000,
	})

	input, output, usd := tc.GlobalUsage()
	assert.Equal(t, int64(3000), input)
	assert.Equal(t, int64(1500), output)
	// claude-sonnet: 0.0105, claude-haiku: 2000/1M*0.80 + 1000/1M*4.00 = 0.0016 + 0.004 = 0.0056
	assert.InDelta(t, 0.0105+0.0056, usd, 1e-9)
}

func TestTokenCounter_RecordUsage_Accumulates(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 3000, OutputTokens: 1500,
	})

	input, output, _ := tc.WorkflowUsage("wf-1")
	assert.Equal(t, int64(4000), input)
	assert.Equal(t, int64(2000), output)
}

func TestTokenCounter_WorkflowUsage_NoRecords(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	input, output, usd := tc.WorkflowUsage("nonexistent")
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
	assert.Equal(t, 0.0, usd)
}

func TestTokenCounter_AgentUsage_NoRecords(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	input, output, usd := tc.AgentUsage("nonexistent")
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
	assert.Equal(t, 0.0, usd)
}

func TestTokenCounter_Concurrent(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())

	var wg sync.WaitGroup
	for i := range 100 {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			tc.RecordUsage(UsageRecord{
				WorkflowID:   fmt.Sprintf("wf-%d", i%5),
				AgentID:      fmt.Sprintf("agent-%d", i%3),
				Model:        "claude-sonnet",
				InputTokens:  100,
				OutputTokens: 50,
			})
		}(i)
	}
	wg.Wait()

	input, output, _ := tc.GlobalUsage()
	assert.Equal(t, int64(10000), input) // 100 * 100
	assert.Equal(t, int64(5000), output) // 100 * 50
}

func TestTokenCounter_ResetDaily(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})

	tc.ResetDaily()

	input, output, usd := tc.GlobalUsage()
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
	assert.Equal(t, 0.0, usd)

	input, output, usd = tc.WorkflowUsage("wf-1")
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
	assert.Equal(t, 0.0, usd)
}

// --- Atomic snapshot ---

func TestUsageSnapshot_Atomic(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())

	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})

	globalUSD, wfUSD, agUSD := tc.usageSnapshot("wf-1", "agent-a")
	assert.Greater(t, globalUSD, 0.0)
	assert.InDelta(t, globalUSD, wfUSD, 1e-9)
	assert.InDelta(t, globalUSD, agUSD, 1e-9)
}

func TestUsageSnapshot_MissingScopes(t *testing.T) {
	cfg := testConfig()
	tc := NewTokenCounter(cfg, zap.NewNop())

	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})

	globalUSD, wfUSD, agUSD := tc.usageSnapshot("wf-nonexistent", "agent-nonexistent")
	assert.Greater(t, globalUSD, 0.0)
	assert.Equal(t, 0.0, wfUSD)
	assert.Equal(t, 0.0, agUSD)
}

// --- C5: ResetDaily agent scope ---

func TestTokenCounter_ResetDaily_AgentScope(t *testing.T) {
	tc := NewTokenCounter(testConfig(), zap.NewNop())
	tc.RecordUsage(UsageRecord{
		WorkflowID: "wf-1", AgentID: "agent-a", Model: "claude-sonnet",
		InputTokens: 1000, OutputTokens: 500,
	})

	input, output, usd := tc.AgentUsage("agent-a")
	require.Greater(t, input, int64(0))
	require.Greater(t, output, int64(0))
	require.Greater(t, usd, 0.0)

	tc.ResetDaily()

	input, output, usd = tc.AgentUsage("agent-a")
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
	assert.Equal(t, 0.0, usd)
}
