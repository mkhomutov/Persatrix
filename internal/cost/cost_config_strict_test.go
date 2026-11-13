package cost

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// A misspelled key in the `cost:` block is refused, not read as "no limit"
// (ISSUE-0211).
//
// A limit of zero means no limit, and the loader used to drop any key it did
// not know. So `budgetz:` for `budgets:` loaded all three spending caps as
// zero: the file an operator edits to control spending turned the control
// off, with nothing said at load.

// shippedCost is the `cost:` block the repository ships, as the base every
// case below changes one thing in.
const shippedCost = `schema_version: "0.3"
active_profile: "default"
default:
  caching:
    exact:
      enabled: true
cost:
  pricing:
    models:
      "some-model":
        input_per_1m_tokens: 3.00
        output_per_1m_tokens: 15.00
  budgets:
    global:
      max_daily_usd: 100.00
      alert_at_percent: [50, 80, 95]
      on_exceed: "pause_and_alert"
    per_workflow:
      default_max_usd: 10.00
    per_agent:
      default_max_usd: 5.00
wallet:
  ttl_seconds: 60
`

func loadCost(t *testing.T, body string) (*CostConfig, error) {
	t.Helper()
	dir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(dir, "optimization.yaml"), []byte(body), 0o644))
	return LoadCostConfig(dir)
}

// swap replaces exactly one occurrence, so a case cannot silently change
// nothing.
func swap(t *testing.T, body, old, replacement string) string {
	t.Helper()
	require.Equal(t, 1, strings.Count(body, old), "%q must occur once in the base", old)
	return strings.Replace(body, old, replacement, 1)
}

func TestLoadCostConfig_RefusesAKeyItDoesNotKnow(t *testing.T) {
	tests := []struct {
		name        string
		old, new    string
		wantInError string
	}{
		{"budgets misspelled", "  budgets:\n", "  budgetz:\n", "budgetz"},
		{"pricing misspelled", "  pricing:\n", "  pricings:\n", "pricings"},
		{"models misspelled", "    models:\n", "    model:\n", "model"},
		{"a price key misspelled", "input_per_1m_tokens: 3.00", "input_per_1m_token: 3.00", "input_per_1m_token"},
		{"global misspelled", "    global:\n", "    globl:\n", "globl"},
		{"per_workflow misspelled", "    per_workflow:\n", "    per_workflows:\n", "per_workflows"},
		{"per_agent misspelled", "    per_agent:\n", "    per_agnet:\n", "per_agnet"},
		{"max_daily_usd misspelled", "max_daily_usd: 100.00", "max_daily_usdd: 100.00", "max_daily_usdd"},
		{"a default_max_usd misspelled", "default_max_usd: 5.00", "default_max: 5.00", "default_max"},
		{"alert_at_percent misspelled", "alert_at_percent:", "alert_at_percents:", "alert_at_percents"},
		{"on_exceed misspelled", "on_exceed:", "on_exceeded:", "on_exceeded"},
		{"an extra key beside pricing and budgets", "cost:\n", "cost:\n  enabled: true\n", "enabled"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := loadCost(t, swap(t, shippedCost, tt.old, tt.new))

			require.Error(t, err)
			assert.Contains(t, err.Error(), "parse optimization config")
			assert.Contains(t, err.Error(), "field "+tt.wantInError+" not found",
				"the error names the key the file has")
		})
	}
}

func TestLoadCostConfig_RefusesAFileWithNoBudgets(t *testing.T) {
	tests := []struct {
		name     string
		old, new string
	}{
		// `cost` is a top-level key, and the top level also holds profiles
		// under names the operator chooses, so a strict decode cannot tell a
		// misspelled `cost` from a profile. The missing block is what tells.
		{"cost misspelled", "cost:\n", "costs:\n"},
		{"no budgets under cost", `  budgets:
    global:
      max_daily_usd: 100.00
      alert_at_percent: [50, 80, 95]
      on_exceed: "pause_and_alert"
    per_workflow:
      default_max_usd: 10.00
    per_agent:
      default_max_usd: 5.00
`, ""},
		{"budgets with no value", `  budgets:
    global:
      max_daily_usd: 100.00
      alert_at_percent: [50, 80, 95]
      on_exceed: "pause_and_alert"
    per_workflow:
      default_max_usd: 10.00
    per_agent:
      default_max_usd: 5.00
`, "  budgets:\n"},
		{"cost with no value", `cost:
  pricing:
    models:
      "some-model":
        input_per_1m_tokens: 3.00
        output_per_1m_tokens: 15.00
  budgets:
    global:
      max_daily_usd: 100.00
      alert_at_percent: [50, 80, 95]
      on_exceed: "pause_and_alert"
    per_workflow:
      default_max_usd: 10.00
    per_agent:
      default_max_usd: 5.00
`, "cost:\n"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := loadCost(t, swap(t, shippedCost, tt.old, tt.new))

			require.Error(t, err)
			assert.Contains(t, err.Error(), "cost.budgets is missing")
		})
	}
}

func TestLoadCostConfig_LoadsWhatItShould(t *testing.T) {
	tests := []struct {
		name     string
		old, new string
		// want is global, per-workflow, per-agent.
		want        [3]float64
		wantNoLimit bool
	}{
		{name: "the base file", old: "", new: "", want: [3]float64{100, 10, 5}},
		{
			// Every other loader owns its own part of this file.
			name: "keys this loader does not own, at the top level",
			old:  "wallet:\n", new: "aggressive:\n  caching:\n    exact:\n      enabled: false\nmodels:\n  aliases: {}\nmemory_budget:\n  tokens: 1\nwallet:\n",
			want: [3]float64{100, 10, 5},
		},
		{
			name: "an empty budgets mapping says no limits, out loud",
			old: `  budgets:
    global:
      max_daily_usd: 100.00
      alert_at_percent: [50, 80, 95]
      on_exceed: "pause_and_alert"
    per_workflow:
      default_max_usd: 10.00
    per_agent:
      default_max_usd: 5.00
`, new: "  budgets: {}\n",
			wantNoLimit: true,
		},
		{
			name: "one limit left out is no limit at that scope only",
			old:  "    per_agent:\n      default_max_usd: 5.00\n", new: "",
			want: [3]float64{100, 10, 0},
		},
		{
			name: "a value shared through an anchor defined outside the block",
			old:  "default_max_usd: 10.00", new: "default_max_usd: *cap",
			want: [3]float64{100, 5, 5},
		},
		{
			name: "no pricing block",
			old: `  pricing:
    models:
      "some-model":
        input_per_1m_tokens: 3.00
        output_per_1m_tokens: 15.00
`, new: "",
			want: [3]float64{100, 10, 5},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			body := shippedCost
			if tt.old != "" {
				body = swap(t, body, tt.old, tt.new)
			}
			if strings.Contains(body, "*cap") {
				// The anchor sits in a profile, above the cost block.
				body = swap(t, body, "      enabled: true\n", "      enabled: true\n  shared_cap: &cap 5.00\n")
			}

			cfg, err := loadCost(t, body)

			require.NoError(t, err)
			got := [3]float64{
				cfg.Budgets.Global.MaxDailyUSD,
				cfg.Budgets.PerWorkflow.DefaultMaxUSD,
				cfg.Budgets.PerAgent.DefaultMaxUSD,
			}
			assert.Equal(t, tt.want, got)
			assert.Equal(t, tt.wantNoLimit, cfg.Budgets.NoSpendingLimit())
			assert.NotNil(t, cfg.Pricing)
		})
	}
}

func TestBudgetThresholds_NoSpendingLimit(t *testing.T) {
	tests := []struct {
		name                          string
		global, perWorkflow, perAgent float64
		want                          bool
	}{
		{"all three zero", 0, 0, 0, true},
		{"only a global limit", 1, 0, 0, false},
		{"only a per-workflow limit", 0, 1, 0, false},
		{"only a per-agent limit", 0, 0, 1, false},
		{"all three set", 100, 10, 5, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			b := BudgetThresholds{
				Global:      GlobalBudget{MaxDailyUSD: tt.global},
				PerWorkflow: PerWorkflowBudget{DefaultMaxUSD: tt.perWorkflow},
				PerAgent:    PerAgentBudget{DefaultMaxUSD: tt.perAgent},
			}
			assert.Equal(t, tt.want, b.NoSpendingLimit())
		})
	}
}

// Every optimization.yaml the repository ships loads, with the caps it
// states: the root config and each demo's.
func TestLoadCostConfig_EveryShippedConfigLoads(t *testing.T) {
	root := filepath.Join("..", "..", "config")
	dirs := []string{root}
	demos, err := filepath.Glob(filepath.Join(root, "demo", "*", "optimization.yaml"))
	require.NoError(t, err)
	require.Len(t, demos, 7, "a demo was added or removed: this list is every shipped config")
	for _, f := range demos {
		dirs = append(dirs, filepath.Dir(f))
	}
	for _, dir := range dirs {
		t.Run(dir, func(t *testing.T) {
			cfg, err := LoadCostConfig(dir)

			require.NoError(t, err)
			assert.InDelta(t, 100.0, cfg.Budgets.Global.MaxDailyUSD, 1e-9)
			assert.InDelta(t, 10.0, cfg.Budgets.PerWorkflow.DefaultMaxUSD, 1e-9)
			assert.InDelta(t, 5.0, cfg.Budgets.PerAgent.DefaultMaxUSD, 1e-9)
			assert.False(t, cfg.Budgets.NoSpendingLimit())
			assert.NotEmpty(t, cfg.Pricing)
		})
	}
}
