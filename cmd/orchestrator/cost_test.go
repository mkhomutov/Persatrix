package main

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

// What start-up says about the cost config (ISSUE-0211).
//
// A limit of zero means no limit. Start-up used to log that state only as
// three zeros on an INFO line, which is what a misspelled `budgets` key left
// behind. A config with no limit above zero now gets a WARN of its own, and
// a config the loader refuses is reported with the loader's reason.

const (
	noLimitWarnMsg    = "no spending limit is set: every limit under cost.budgets is 0 or left out, so nothing caps what agents spend"
	loadFailedWarnMsg = "failed to load cost config, budget enforcement disabled"
)

func costConfigDir(t *testing.T, costBlock string) string {
	t.Helper()
	dir := t.TempDir()
	body := "schema_version: \"0.3\"\nactive_profile: \"default\"\n" + costBlock
	require.NoError(t, os.WriteFile(filepath.Join(dir, "optimization.yaml"), []byte(body), 0o644))
	return dir
}

func budgets(global, perWorkflow, perAgent float64) string {
	return fmt.Sprintf(`cost:
  budgets:
    global:
      max_daily_usd: %g
    per_workflow:
      default_max_usd: %g
    per_agent:
      default_max_usd: %g
`, global, perWorkflow, perAgent)
}

func TestLoadCostConfig_StartupWarnings(t *testing.T) {
	tests := []struct {
		name      string
		costBlock string
		wantNil   bool
		// wantWarns is every WARN line start-up logs, in order.
		wantWarns []string
		// wantInError is text the load-failure line's error must hold.
		wantInError string
	}{
		{name: "three limits set", costBlock: budgets(100, 10, 5)},
		{name: "only the global limit set", costBlock: budgets(100, 0, 0)},
		{name: "only the per-workflow limit set", costBlock: budgets(0, 10, 0)},
		{name: "only the per-agent limit set", costBlock: budgets(0, 0, 5)},
		{
			name: "every limit set to zero", costBlock: budgets(0, 0, 0),
			wantWarns: []string{noLimitWarnMsg},
		},
		{
			name: "an empty budgets block", costBlock: "cost:\n  budgets: {}\n",
			wantWarns: []string{noLimitWarnMsg},
		},
		{
			name: "budgets misspelled", costBlock: "cost:\n  budgetz:\n    global:\n      max_daily_usd: 100\n",
			wantNil: true, wantWarns: []string{loadFailedWarnMsg}, wantInError: "field budgetz not found",
		},
		{
			name: "cost misspelled", costBlock: "costs:\n  budgets:\n    global:\n      max_daily_usd: 100\n",
			wantNil: true, wantWarns: []string{loadFailedWarnMsg}, wantInError: "cost.budgets is missing",
		},
		{
			name: "no cost block", costBlock: "",
			wantNil: true, wantWarns: []string{loadFailedWarnMsg}, wantInError: "cost.budgets is missing",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			core, recorded := observer.New(zap.WarnLevel)
			dir := costConfigDir(t, tt.costBlock)

			cfg := loadCostConfig(zap.New(core), dir)

			assert.Equal(t, tt.wantNil, cfg == nil)
			var warns []string
			for _, entry := range recorded.All() {
				warns = append(warns, entry.Message)
				assert.Equal(t, dir, entry.ContextMap()["configDir"])
				if tt.wantInError != "" {
					assert.Contains(t, fmt.Sprint(entry.ContextMap()["error"]), tt.wantInError)
				}
			}
			assert.Equal(t, tt.wantWarns, warns)
		})
	}
}

// The shipped configs, the root one and each demo's, start without either
// warning.
func TestLoadCostConfig_ShippedConfigsStartQuietly(t *testing.T) {
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
			core, recorded := observer.New(zap.WarnLevel)

			cfg := loadCostConfig(zap.New(core), dir)

			require.NotNil(t, cfg)
			assert.Empty(t, recorded.All())
		})
	}
}
