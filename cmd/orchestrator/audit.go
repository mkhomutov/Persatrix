// Package main: audit logger bootstrap helpers (RFC 0009 PR 1b).
//
// Extracted from main.go so the orchestrator entry point stays under the
// 500-line code-review limit enforced by scripts/checks/file_size.py.
package main

import (
	"fmt"
	"os"
	"path/filepath"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/security"
)

const (
	// auditPathEnvVar names the environment variable that overrides the
	// audit log file path used by RFC 0009 PR 1b. Documented in
	// docs/observability.md.
	auditPathEnvVar = "OBSERVABILITY_AUDIT_PATH"

	// defaultAuditPath is the audit-log fallback when auditPathEnvVar is
	// unset. Lives under data/logs/ alongside other operator-facing JSONL
	// artifacts.
	defaultAuditPath = "data/logs/audit.jsonl"

	// auditDisableSentinel opts out of audit logging entirely. Mirrors the
	// existing logbuffer / metrics opt-out convention so operators have a
	// single mental model for "this subsystem is off".
	auditDisableSentinel = "off"
)

// initAuditLogger constructs the orchestrator's audit sink from the
// OBSERVABILITY_AUDIT_PATH env var. Returns (nil, nil) when the operator
// has explicitly opted out via "=off" so callers can branch on the nil
// logger without a separate disabled flag.
func initAuditLogger(logger *zap.Logger) (security.AuditLogger, error) {
	path := os.Getenv(auditPathEnvVar)
	switch path {
	case auditDisableSentinel:
		logger.Warn("audit logger disabled via " + auditPathEnvVar + "=off")
		return nil, nil
	case "":
		path = defaultAuditPath
	}
	if dir := filepath.Dir(path); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return nil, fmt.Errorf("create audit log directory %q: %w", dir, err)
		}
	}
	return security.NewFileAuditLogger(path)
}
