package main

import (
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
)

// loadCostConfig loads the cost config for start-up and says what an
// operator needs to hear about it. It returns nil when the config did not
// load; main then builds no cost tracking and no wallet service.
//
// Two states get a WARN (ISSUE-0211):
//
//   - The loader refused the file. Since ISSUE-0211 that includes a key the
//     `cost:` block does not have and a file with no `cost.budgets`, which
//     both used to load as "no limit".
//   - The file loaded and no limit is above zero. Zero means no limit, so
//     nothing caps spending. That can be meant (the block is there, with
//     zeros), which is why it is a warning and not an error.
func loadCostConfig(logger *zap.Logger, configDir string) *cost.CostConfig {
	costCfg, err := cost.LoadCostConfig(configDir, cost.WithLogger(logger))
	if err != nil {
		logger.Warn("failed to load cost config, budget enforcement disabled",
			zap.String("configDir", configDir),
			zap.Error(err),
		)
		return nil
	}
	if costCfg.Budgets.NoSpendingLimit() {
		logger.Warn("no spending limit is set: every limit under cost.budgets is 0 or left out, so nothing caps what agents spend",
			zap.String("configDir", configDir),
		)
	}
	return costCfg
}
