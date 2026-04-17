package cost

import (
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

// ModelPricing holds per-model token pricing in USD per 1M tokens.
type ModelPricing struct {
	InputPer1MTokens  float64 `yaml:"input_per_1m_tokens"`
	OutputPer1MTokens float64 `yaml:"output_per_1m_tokens"`
}

// BudgetThresholds holds spending limits at each scope.
type BudgetThresholds struct {
	Global      GlobalBudget      `yaml:"global"`
	PerWorkflow PerWorkflowBudget `yaml:"per_workflow"`
	PerAgent    PerAgentBudget    `yaml:"per_agent"`
}

// GlobalBudget configures the global daily spending limit.
type GlobalBudget struct {
	MaxDailyUSD    float64   `yaml:"max_daily_usd"`
	AlertAtPercent []float64 `yaml:"alert_at_percent"`
	OnExceed       string    `yaml:"on_exceed"`
}

// PerWorkflowBudget configures the per-workflow spending limit.
type PerWorkflowBudget struct {
	DefaultMaxUSD float64 `yaml:"default_max_usd"`
}

// PerAgentBudget configures the per-agent spending limit.
type PerAgentBudget struct {
	DefaultMaxUSD float64 `yaml:"default_max_usd"`
}

// CostConfig holds pricing and budget configuration loaded from optimization.yaml.
type CostConfig struct {
	Pricing    map[string]ModelPricing `yaml:"-"`
	Budgets    BudgetThresholds        `yaml:"-"`
	rawPricing rawPricingSection       `yaml:"-"`
}

// rawOptimizationFile mirrors the top-level structure of optimization.yaml for parsing.
type rawOptimizationFile struct {
	Cost rawCostSection `yaml:"cost"`
}

type rawCostSection struct {
	Pricing rawPricingSection `yaml:"pricing"`
	Budgets BudgetThresholds  `yaml:"budgets"`
}

type rawPricingSection struct {
	Models map[string]ModelPricing `yaml:"models"`
}

// LoadCostConfig reads the optimization.yaml file from configDir and parses the
// cost section (pricing table and budget thresholds).
func LoadCostConfig(configDir string) (*CostConfig, error) {
	path := filepath.Join(configDir, "optimization.yaml")
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read optimization config: %w", err)
	}

	var raw rawOptimizationFile
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("parse optimization config: %w", err)
	}

	cfg := &CostConfig{
		Pricing: raw.Cost.Pricing.Models,
		Budgets: raw.Cost.Budgets,
	}

	if cfg.Pricing == nil {
		cfg.Pricing = make(map[string]ModelPricing)
	}

	return cfg, nil
}

// EstimateCost computes the estimated cost in USD for a given model and token counts.
// Returns 0 if the model is not in the pricing table (graceful degradation).
func (c *CostConfig) EstimateCost(model string, inputTokens, outputTokens int64) float64 {
	pricing, ok := c.Pricing[model]
	if !ok {
		return 0
	}
	inputCost := float64(inputTokens) / 1_000_000 * pricing.InputPer1MTokens
	outputCost := float64(outputTokens) / 1_000_000 * pricing.OutputPer1MTokens
	return inputCost + outputCost
}
