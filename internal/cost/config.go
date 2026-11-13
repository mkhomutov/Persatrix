package cost

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"go.uber.org/zap"
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

// NoSpendingLimit reports whether no scope has a limit. A limit of zero
// means no limit ([BudgetEnforcer.CheckBudget] applies one only when it is
// greater than zero), so with all three at zero nothing caps spending.
func (b BudgetThresholds) NoSpendingLimit() bool {
	return b.Global.MaxDailyUSD <= 0 && b.PerWorkflow.DefaultMaxUSD <= 0 && b.PerAgent.DefaultMaxUSD <= 0
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
	Pricing map[string]ModelPricing `yaml:"-"`
	Budgets BudgetThresholds        `yaml:"-"`
	logger  *zap.Logger
}

// validOnExceedValues defines the accepted values for the on_exceed config field.
var validOnExceedValues = map[string]bool{
	"fail":            true,
	"pause_and_alert": true,
	"":                true, // empty means not configured; defaults to "fail" at enforcement time
}

// rawOptimizationFile is optimization.yaml as this loader reads it: the
// `cost:` block, and every other top-level key kept unread in Other.
//
// The file is decoded strictly ([DecodeOptimizationFile]), so a key the
// `cost:` block does not have is an error. Other is what makes that possible
// for one block of a shared file: the top level also holds the blocks other
// loaders own and one block per profile, under names the operator chooses,
// and an inline map takes every key no field claims before the strict check
// can refuse it. Its values are raw nodes, so nothing inside them is checked
// here.
//
// Cost and Budgets are pointers so that a block that is absent can be told
// from one that is present and empty.
type rawOptimizationFile struct {
	Cost  *rawCostSection      `yaml:"cost"`
	Other map[string]yaml.Node `yaml:",inline"`
}

type rawCostSection struct {
	Pricing rawPricingSection `yaml:"pricing"`
	Budgets *BudgetThresholds `yaml:"budgets"`
}

type rawPricingSection struct {
	Models map[string]ModelPricing `yaml:"models"`
}

// DecodeOptimizationFile decodes optimization.yaml into out and refuses any
// key out's fields do not name (ISSUE-0211). The plain decoder drops such a
// key, and in this file a dropped key reads as zero, which for a spending
// limit means no limit: `budgetz:` for `budgets:` used to load every cap as
// unlimited.
//
// out is a struct that names the one block its loader owns and takes the
// rest of the top level in an inline `map[string]yaml.Node`, as
// [rawOptimizationFile] does. An empty file decodes to the zero value.
//
// Exported for internal/wallet, which reads the `wallet:` block of the same
// file.
func DecodeOptimizationFile(data []byte, out any) error {
	dec := yaml.NewDecoder(bytes.NewReader(data))
	dec.KnownFields(true)
	if err := dec.Decode(out); err != nil && !errors.Is(err, io.EOF) {
		return err
	}
	return nil
}

// LoadCostConfig reads the optimization.yaml file from configDir and parses the
// cost section (pricing table and budget thresholds). Validates that budget
// thresholds are non-negative and on_exceed is a recognized value.
//
// It refuses a `cost:` block with a key it does not know, and a file with no
// `cost.budgets` block (ISSUE-0211). A deployment that wants no spending
// limit says so with the block present and its limits at zero, or left out;
// [BudgetThresholds.NoSpendingLimit] then reports it, and the orchestrator
// warns at start-up.
func LoadCostConfig(configDir string, opts ...ConfigOption) (*CostConfig, error) {
	o := configOptions{logger: zap.NewNop()}
	for _, opt := range opts {
		opt(&o)
	}

	path := filepath.Join(configDir, "optimization.yaml")
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read optimization config: %w", err)
	}

	var raw rawOptimizationFile
	if err := DecodeOptimizationFile(data, &raw); err != nil {
		return nil, fmt.Errorf("parse optimization config: %w", err)
	}
	if raw.Cost == nil || raw.Cost.Budgets == nil {
		// A misspelled `cost` lands here too: the top level is open, so
		// `costs:` is one more key this loader does not own.
		return nil, fmt.Errorf("validate cost config: cost.budgets is missing from %s; "+
			"a limit left out or set to 0 means no limit, but the block itself must be there", path)
	}

	cfg := &CostConfig{
		Pricing: raw.Cost.Pricing.Models,
		Budgets: *raw.Cost.Budgets,
		logger:  o.logger,
	}

	if cfg.Pricing == nil {
		cfg.Pricing = make(map[string]ModelPricing)
	}

	// Validate budget thresholds.
	if cfg.Budgets.Global.MaxDailyUSD < 0 {
		return nil, fmt.Errorf("validate cost config: global max_daily_usd must be >= 0, got %f", cfg.Budgets.Global.MaxDailyUSD)
	}
	if cfg.Budgets.PerWorkflow.DefaultMaxUSD < 0 {
		return nil, fmt.Errorf("validate cost config: per_workflow default_max_usd must be >= 0, got %f", cfg.Budgets.PerWorkflow.DefaultMaxUSD)
	}
	if cfg.Budgets.PerAgent.DefaultMaxUSD < 0 {
		return nil, fmt.Errorf("validate cost config: per_agent default_max_usd must be >= 0, got %f", cfg.Budgets.PerAgent.DefaultMaxUSD)
	}
	if !validOnExceedValues[cfg.Budgets.Global.OnExceed] {
		return nil, fmt.Errorf("validate cost config: on_exceed must be \"fail\" or \"pause_and_alert\", got %q", cfg.Budgets.Global.OnExceed)
	}

	return cfg, nil
}

// configOptions holds optional parameters for LoadCostConfig.
type configOptions struct {
	logger *zap.Logger
}

// ConfigOption configures LoadCostConfig behavior.
type ConfigOption func(*configOptions)

// WithLogger sets the logger used by CostConfig for operational diagnostics.
func WithLogger(logger *zap.Logger) ConfigOption {
	return func(o *configOptions) {
		if logger != nil {
			o.logger = logger
		}
	}
}

// EstimateCost computes the estimated cost in USD for a given model and token counts.
// Returns 0 if the model is not in the pricing table (graceful degradation).
// Logs at Debug level when a model is not found to help operators diagnose
// $0 cost tracking caused by model name mismatches between config and usage.
func (c *CostConfig) EstimateCost(model string, inputTokens, outputTokens int64) float64 {
	if c.Pricing == nil {
		return 0
	}
	pricing, ok := c.Pricing[model]
	if !ok {
		if c.logger != nil {
			c.logger.Debug("model not found in pricing table, cost estimate is $0",
				zap.String("model", model),
			)
		}
		return 0
	}
	inputCost := float64(inputTokens) / 1_000_000 * pricing.InputPer1MTokens
	outputCost := float64(outputTokens) / 1_000_000 * pricing.OutputPer1MTokens
	return inputCost + outputCost
}
