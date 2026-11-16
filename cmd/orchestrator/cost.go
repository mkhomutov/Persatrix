package main

import (
	"fmt"
	"path/filepath"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
)

// costConfigRefusal is what main() logs, at FATAL, when the cost config did
// not load. The error beside it names the file and the fault.
const costConfigRefusal = "refusing to start: the cost config did not load, and without it there is no wallet, so no agent could call a model"

// loadCostConfig loads the cost config for start-up and says what an
// operator needs to hear about it.
//
// It returns an error when the config did not load, and main() then refuses
// to start (ISSUE-0204). The wallet is built from this config, and an agent
// that cannot reach a wallet refuses every model call. An orchestrator that
// carried on without it, as this one used to, answered its health check and
// was of no use to any agent.
//
// "Did not load" is every refusal of [cost.LoadCostConfig]: no
// optimization.yaml in the config folder, one that cannot be read, one that
// is not YAML, a `cost:` block with a key it does not have or with no
// `budgets` (ISSUE-0211), and a limit or an `on_exceed` value the loader
// does not accept. The error names the file and gives the loader's reason.
//
// One state gets a WARN (ISSUE-0211): the file loaded and no limit is above
// zero. Zero means no limit, so nothing caps spending. That can be meant
// (the block is there, with zeros), which is why it is a warning and not an
// error.
func loadCostConfig(logger *zap.Logger, configDir string) (*cost.CostConfig, error) {
	costCfg, err := cost.LoadCostConfig(configDir, cost.WithLogger(logger))
	if err != nil {
		return nil, fmt.Errorf("cost config %s did not load: %w", filepath.Join(configDir, cost.ConfigFile), err)
	}
	if costCfg.Budgets.NoSpendingLimit() {
		logger.Warn("no spending limit is set: every limit under cost.budgets is 0 or left out, so nothing caps what agents spend",
			zap.String("configDir", configDir),
		)
	}
	return costCfg, nil
}
