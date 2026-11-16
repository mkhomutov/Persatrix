package main

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

// What start-up says about the cost config (ISSUE-0211, ISSUE-0204).
//
// A limit of zero means no limit. Start-up used to log that state only as
// three zeros on an INFO line, which is what a misspelled `budgets` key left
// behind. A config with no limit above zero now gets a WARN of its own.
//
// A config the loader refuses is an error, and main() does not start on it
// (ISSUE-0204). It used to be a WARN, after which the orchestrator ran with
// no wallet service: it answered its health check while every agent's model
// call was refused.

const noLimitWarnMsg = "no spending limit is set: every limit under cost.budgets is 0 or left out, so nothing caps what agents spend"

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
		// wantWarns is every WARN line start-up logs, in order.
		wantWarns []string
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
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			core, recorded := observer.New(zap.WarnLevel)
			dir := costConfigDir(t, tt.costBlock)

			cfg, err := loadCostConfig(zap.New(core), dir)

			require.NoError(t, err)
			require.NotNil(t, cfg)
			var warns []string
			for _, entry := range recorded.All() {
				warns = append(warns, entry.Message)
				assert.Equal(t, dir, entry.ContextMap()["configDir"])
			}
			assert.Equal(t, tt.wantWarns, warns)
		})
	}
}

// Every way the cost config fails to load is an error that names the file
// and the fault. main() refuses to start on it, so the error is the whole of
// what an operator is told.
func TestLoadCostConfig_AFileThatDoesNotLoadIsAnError(t *testing.T) {
	writeBody := func(costBlock string) func(*testing.T, string) {
		return func(t *testing.T, path string) {
			body := "schema_version: \"0.3\"\nactive_profile: \"default\"\n" + costBlock
			require.NoError(t, os.WriteFile(path, []byte(body), 0o644))
		}
	}
	tests := []struct {
		name string
		// put writes the case's optimization.yaml at path, or leaves it out.
		put func(t *testing.T, path string)
		// wantInError is the loader's reason, as the error must hold it.
		wantInError string
		// wantIs, when set, is an error the returned one must wrap.
		wantIs error
	}{
		{
			name: "no file in the config folder", put: func(*testing.T, string) {},
			wantInError: "read optimization config", wantIs: fs.ErrNotExist,
		},
		{
			name: "a folder where the file should be",
			put: func(t *testing.T, path string) {
				require.NoError(t, os.Mkdir(path, 0o755))
			},
			wantInError: "read optimization config",
		},
		{
			name: "not YAML", put: writeBody("cost: [\n"),
			wantInError: "parse optimization config",
		},
		{
			name:        "budgets misspelled",
			put:         writeBody("cost:\n  budgetz:\n    global:\n      max_daily_usd: 100\n"),
			wantInError: "field budgetz not found",
		},
		{
			name:        "cost misspelled",
			put:         writeBody("costs:\n  budgets:\n    global:\n      max_daily_usd: 100\n"),
			wantInError: "cost.budgets is missing",
		},
		{
			name: "no cost block", put: writeBody(""),
			wantInError: "cost.budgets is missing",
		},
		{
			name: "a limit below zero", put: writeBody(budgets(-1, 10, 5)),
			wantInError: "global max_daily_usd must be >= 0",
		},
		{
			name:        "an on_exceed value the loader does not know",
			put:         writeBody("cost:\n  budgets:\n    global:\n      on_exceed: shrug\n"),
			wantInError: "on_exceed must be",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			core, recorded := observer.New(zap.WarnLevel)
			dir := t.TempDir()
			path := filepath.Join(dir, "optimization.yaml")
			tt.put(t, path)

			cfg, err := loadCostConfig(zap.New(core), dir)

			require.Error(t, err)
			assert.Nil(t, cfg, "no config comes back with the error: there is nothing to run on")
			assert.Contains(t, err.Error(), path, "the error names the file")
			assert.Contains(t, err.Error(), tt.wantInError, "the error names the fault")
			if tt.wantIs != nil {
				assert.ErrorIs(t, err, tt.wantIs)
			}
			assert.Empty(t, recorded.All(), "a refusal is main()'s to report, once")
		})
	}
}

// The shipped configs, the root one and each demo's, load, and start without
// a warning. This is also what shows that no shipped start relies on an
// orchestrator that runs without a cost config (ISSUE-0204): each compose
// file and each demo mounts one of these files.
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

			cfg, err := loadCostConfig(zap.New(core), dir)

			require.NoError(t, err)
			require.NotNil(t, cfg)
			assert.Empty(t, recorded.All())
		})
	}
}
