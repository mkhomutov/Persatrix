// budget.go contains execution-limit resolution, token recording, and cost metadata helpers.
package scheduler

import (
	"context"
	"errors"
	"strconv"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/defaults"
	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/state"
)

// ErrBudgetExceeded is returned when a step dispatch is rejected by the budget enforcer.
// Callers can use errors.Is(err, ErrBudgetExceeded) to programmatically distinguish
// budget failures from agent execution failures without string matching.
// The REST API layer (PR 4b) maps this to HTTP 429 with a structured error body.
var ErrBudgetExceeded = errors.New("budget exceeded")

// resolveStepLimits implements the three-level cascade for execution limits:
// workflow step config → agent config → system defaults (RFC 0006 Section A).
// Zero values at any level mean "not configured" and fall through to the next level.
func (s *WorkflowScheduler) resolveStepLimits(ctx context.Context, step planner.Step) executor.StepLimits {
	limits := executor.StepLimits{
		MaxLLMCalls:    defaults.DefaultMaxLLMCalls,
		MaxTokens:      defaults.DefaultMaxTokens,
		TimeoutSeconds: defaults.DefaultTimeoutSeconds,
	}

	// Middle tier: agent config overrides system defaults.
	if s.registry != nil {
		agent, err := s.registry.Get(ctx, step.AgentID)
		if err == nil {
			// F-04: Warn on negative agent-level limits. The planner rejects
			// negative step-level limits at parse time, but agent config values
			// arrive via the registry without validation. Negative values fail
			// the > 0 check and silently fall through to defaults; log a warning
			// so operators can detect misconfiguration.
			if agent.MaxLLMCalls < 0 {
				s.logger.Warn("negative agent-level MaxLLMCalls, using default",
					zap.String("agent_id", step.AgentID),
					zap.Int("value", agent.MaxLLMCalls),
				)
			}
			if agent.MaxTokens < 0 {
				s.logger.Warn("negative agent-level MaxTokens, using default",
					zap.String("agent_id", step.AgentID),
					zap.Int("value", agent.MaxTokens),
				)
			}
			if agent.TimeoutSeconds < 0 {
				s.logger.Warn("negative agent-level TimeoutSeconds, using default",
					zap.String("agent_id", step.AgentID),
					zap.Int("value", agent.TimeoutSeconds),
				)
			}
			if agent.MaxLLMCalls > 0 {
				limits.MaxLLMCalls = agent.MaxLLMCalls
			}
			if agent.MaxTokens > 0 {
				limits.MaxTokens = agent.MaxTokens
			}
			if agent.TimeoutSeconds > 0 {
				limits.TimeoutSeconds = agent.TimeoutSeconds
			}
		} else {
			s.logger.Warn("failed to look up agent config for limit resolution, using defaults",
				zap.String("agent_id", step.AgentID),
				zap.Error(err),
			)
		}
	}

	// Highest tier: step config overrides agent config.
	if step.MaxLLMCalls > 0 {
		limits.MaxLLMCalls = step.MaxLLMCalls
	}
	if step.MaxTokens > 0 {
		limits.MaxTokens = step.MaxTokens
	}
	if step.TimeoutSeconds > 0 {
		limits.TimeoutSeconds = step.TimeoutSeconds
	}

	return limits
}

// recordStepUsage records token usage from a completed step dispatch.
// Uses resolveStepTokenData to parse tokens and compute cost — the same helper
// used by buildStepMetadata — ensuring parity between what's recorded in the
// cost system and what's reported in step metadata. (PR 5a, M-01 fix)
//
// registryModel is the model resolved from the agent registry before dispatch.
// It is used as a fallback when the executor response metadata does not include
// a "model" key. This avoids a redundant registry lookup post-dispatch.
// (PR #86 review: reduce double resolveAgentModel call)
func (s *WorkflowScheduler) recordStepUsage(workflowID string, step planner.Step, result *executor.ExecuteResult, registryModel string) {
	if s.tokenCounter == nil || result == nil {
		return
	}

	data := s.resolveStepTokenData(result, registryModel, step.ID)

	// Log when using tokens_used fallback so operators can identify agents that
	// should provide granular input_tokens/output_tokens data. (PR #86 review S-03)
	if data.usedTokensFallback {
		s.logger.Info("using tokens_used fallback (all tokens mapped to output, cost may be overestimated)",
			zap.String("step_id", step.ID),
			zap.String("agent_id", step.AgentID),
			zap.Int64("tokensUsed", data.outputTokens),
		)
	}

	if data.inputTokens == 0 && data.outputTokens == 0 {
		s.logger.Warn("no token usage in step response metadata, recording zero",
			zap.String("step_id", step.ID),
			zap.String("agent_id", step.AgentID),
		)
	}

	s.tokenCounter.RecordUsage(cost.UsageRecord{
		WorkflowID:   workflowID,
		AgentID:      step.AgentID,
		Model:        data.model,
		InputTokens:  data.inputTokens,
		OutputTokens: data.outputTokens,
	})

	// NOTE (S-04): When costReporter is nil but tokenCounter is non-nil, the
	// TokenCounter running totals include data that CostReporter step entries
	// don't. This is expected — the counter tracks all usage for budget
	// enforcement, while the reporter tracks per-step cost entries for the cost
	// endpoint. Comparing counter totals vs reporter sums will show a discrepancy.
	if s.costReporter != nil {
		// PR #86 review S-04: Log when a non-empty model has no pricing entry,
		// causing $0 cost despite non-zero tokens. Helps operators diagnose
		// unpriced models without adding a logger to CostConfig.EstimateCost.
		if data.estimatedCostUSD == 0 && data.model != "" && (data.inputTokens > 0 || data.outputTokens > 0) {
			s.logger.Debug("model not in pricing table, step cost recorded as $0",
				zap.String("step_id", step.ID),
				zap.String("model", data.model),
				zap.Int64("inputTokens", data.inputTokens),
				zap.Int64("outputTokens", data.outputTokens),
			)
		}
		s.costReporter.RecordStepCost(workflowID, cost.StepCostEntry{
			StepID:       step.ID,
			AgentID:      step.AgentID,
			Model:        data.model,
			InputTokens:  data.inputTokens,
			OutputTokens: data.outputTokens,
			EstimatedUSD: data.estimatedCostUSD,
		})
	}
}

// stepTokenData holds resolved token, model, and cost data for a completed step.
// Both recordStepUsage and buildStepMetadata use this to ensure cost estimation
// parity. Without this shared helper, buildStepMetadata computed $0 cost when
// only tokens_used was reported while recordStepUsage correctly fell back to the
// pessimistic tokens_used → outputTokens mapping. (PR 5a, M-01 fix)
type stepTokenData struct {
	inputTokens        int64
	outputTokens       int64
	tokensUsed         int // total for display (tokens_used or input+output)
	llmCallCount       int
	model              string
	estimatedCostUSD   float64
	usedTokensFallback bool // true when tokens_used was mapped to outputTokens
}

// resolveStepTokenData parses token usage, model, and cost from an executor result.
// It implements the tokens_used → outputTokens pessimistic fallback (see
// recordStepUsage doc) and computes the estimated cost from the pricing table.
// stepID is included in warning logs for diagnosability (M-03 fix).
func (s *WorkflowScheduler) resolveStepTokenData(result *executor.ExecuteResult, registryModel, stepID string) stepTokenData {
	var data stepTokenData
	if result == nil || result.Metadata == nil {
		return data
	}

	data.inputTokens = parseMetadataInt64(result.Metadata, "input_tokens", s.logger, stepID)
	data.outputTokens = parseMetadataInt64(result.Metadata, "output_tokens", s.logger, stepID)
	rawTokensUsed := parseMetadataInt64(result.Metadata, "tokens_used", s.logger, stepID)

	// Fall back to combined tokens_used → outputTokens when per-direction tokens
	// are absent. Output tokens are priced higher than input tokens, so this
	// intentionally overestimates cost — making budget checks more conservative.
	if data.inputTokens == 0 && data.outputTokens == 0 {
		data.outputTokens = rawTokensUsed
		data.usedTokensFallback = rawTokensUsed > 0
	}

	// Total for display: prefer explicit tokens_used (more accurate total from
	// agent when both per-direction and total are reported), fall back to sum.
	if rawTokensUsed > 0 {
		data.tokensUsed = int(rawTokensUsed)
	} else if data.inputTokens > 0 || data.outputTokens > 0 {
		data.tokensUsed = int(data.inputTokens + data.outputTokens)
	}

	data.llmCallCount = int(parseMetadataInt64(result.Metadata, "llm_call_count", s.logger, stepID))

	// Resolve model: prefer response metadata, fall back to registry.
	data.model = result.Metadata["model"]
	if data.model == "" {
		data.model = registryModel
	}

	// Compute estimated cost from pricing table.
	if s.tokenCounter != nil {
		data.estimatedCostUSD = s.tokenCounter.Config().EstimateCost(data.model, data.inputTokens, data.outputTokens)
	}

	return data
}

// buildStepMetadata constructs a StepExecutionMetadata from the executor result
// and cost data. Uses resolveStepTokenData to ensure cost estimation is consistent
// with recordStepUsage. Missing metadata fields degrade gracefully to zero values.
// stepID is passed for diagnosable warning logs (M-03 fix).
func (s *WorkflowScheduler) buildStepMetadata(result *executor.ExecuteResult, registryModel, stepID string) *state.StepExecutionMetadata {
	if result == nil {
		return nil
	}

	data := s.resolveStepTokenData(result, registryModel, stepID)

	return &state.StepExecutionMetadata{
		TokensUsed:       data.tokensUsed,
		LLMCallCount:     data.llmCallCount,
		RetryCount:       result.RetryCount,
		CacheHit:         result.CacheHit,
		WallTimeMs:       result.WallTimeMs,
		EstimatedCostUSD: data.estimatedCostUSD,
	}
}

// resolveAgentModel looks up the model configured for an agent in the registry.
// Returns an empty string if the registry is nil or the agent is not found
// (graceful degradation — empty model makes EstimateCost return $0, effectively
// skipping the budget check for that step).
func (s *WorkflowScheduler) resolveAgentModel(ctx context.Context, agentID string) string {
	if s.registry == nil {
		return ""
	}
	agent, err := s.registry.Get(ctx, agentID)
	if err != nil {
		// PR #86 review S-01: Log at Debug level when registry lookup fails.
		// This covers the non-nil-registry error path (e.g., agent deregistered
		// between scheduling and dispatch) and aids test coverage validation.
		s.logger.Debug("agent not found in registry for model resolution",
			zap.String("agent_id", agentID),
			zap.Error(err),
		)
		return ""
	}
	return agent.Model
}

// parseMetadataInt64 parses an int64 value from a metadata map.
// Returns 0 if the key is absent or the value is not a valid integer.
func parseMetadataInt64(metadata map[string]string, key string, logger *zap.Logger, stepID string) int64 {
	val, ok := metadata[key]
	if !ok || val == "" {
		return 0
	}
	n, err := strconv.ParseInt(val, 10, 64)
	if err != nil {
		logger.Warn("failed to parse metadata value as int64",
			zap.String("step_id", stepID),
			zap.String("key", key),
			zap.String("value", val),
			zap.Error(err),
		)
		return 0
	}
	// Security: clamp negative values to zero to prevent adversarial agents from
	// reporting negative token counts, which would decrease the running total in
	// TokenCounter and effectively bypass budget enforcement. (PR #86 review F-01)
	if n < 0 {
		logger.Warn("negative token value clamped to zero",
			zap.String("step_id", stepID),
			zap.String("key", key),
			zap.Int64("value", n),
		)
		return 0
	}
	return n
}
