// Package logbuffer — environment-driven Config builder.
//
// Extracted from cmd/orchestrator/main.go (RFC 0018 PR 5) so the
// orchestrator entry point stays under the project's 500-line file
// budget.  Keeping the env-var → Config mapping next to the Config
// struct itself also localises the documentation that ties the
// PERSATRIX_LOGBUFFER_* operator surface to its in-code defaults.
package logbuffer

import (
	"os"
	"strconv"
)

// ConfigFromEnv reads the seven PERSATRIX_LOGBUFFER_* environment
// variables documented in RFC 0018 § E and merges them onto
// Defaults().  Empty / malformed values fall through to the default
// rather than crashing startup so a typo (e.g. PERSATRIX_LOGBUFFER_DIR
// pointing at an unreadable path) does not take down the orchestrator
// — the buffer logs a warning and the REST endpoints stay 501 if
// construction itself fails.
func ConfigFromEnv() Config {
	cfg := Defaults()
	if v := os.Getenv("PERSATRIX_LOGBUFFER_PER_EXEC"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			cfg.PerExecution = n
		}
	}
	if v := os.Getenv("PERSATRIX_LOGBUFFER_MAX_EXEC"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			cfg.MaxExecutions = n
		}
	}
	if v := os.Getenv("PERSATRIX_LOGBUFFER_DIR"); v != "" {
		cfg.Dir = v
	}
	if v := os.Getenv("PERSATRIX_LOGBUFFER_DISK_MB"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			cfg.DiskCapBytes = int64(n) * 1024 * 1024
		}
	}
	if v := os.Getenv("PERSATRIX_LOGBUFFER_DROP_LEVEL"); v != "" {
		cfg.DropLevel = v
	}
	if v := os.Getenv("PERSATRIX_LOGBUFFER_RATE_PER_EXEC"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			cfg.RatePerExec = n
		}
	}
	if v := os.Getenv("PERSATRIX_LOGBUFFER_SUBSCRIBERS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			cfg.MaxSubscribers = n
		}
	}
	return cfg
}
