// Package main: audit logger bootstrap helpers (RFC 0009 PR 1b).
//
// Extracted from main.go so the orchestrator entry point stays under the
// 500-line code-review limit enforced by scripts/checks/file_size.py.
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

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
	//
	// Resolved via [filepath.Abs] in initAuditLogger so the file lands in
	// a deterministic location regardless of process cwd (PR #234 review
	// L-2). Without this, running the orchestrator under systemd with
	// `WorkingDirectory=/var/lib/persatrix` would silently land the audit
	// log at `/var/lib/persatrix/data/logs/audit.jsonl`, outside the
	// operator's expected `chmod` scope.
	defaultAuditPath = "data/logs/audit.jsonl"

	// auditDisableSentinel opts out of audit logging entirely. Mirrors the
	// existing logbuffer / metrics opt-out convention so operators have a
	// single mental model for "this subsystem is off".
	//
	// Comparison is case-insensitive (PR #234 review L-3) — operators
	// copying `OFF` from documentation must not silently land on a file
	// literally named "OFF" in cwd.
	auditDisableSentinel = "off"
)

// initAuditLogger constructs the orchestrator's audit sink from the
// OBSERVABILITY_AUDIT_PATH env var. Returns (nil, nil) when the operator
// has explicitly opted out via "=off" (any case) so callers can branch
// on the nil logger without a separate disabled flag.
func initAuditLogger(logger *zap.Logger) (security.AuditLogger, error) {
	path := os.Getenv(auditPathEnvVar)
	switch {
	case strings.EqualFold(path, auditDisableSentinel):
		logger.Warn("audit logger disabled via " + auditPathEnvVar + "=off")
		return nil, nil
	case path == "":
		path = defaultAuditPath
	}
	// Resolve any relative path (default OR operator-set) once at startup so
	// the effective location is stable across the orchestrator lifetime
	// regardless of downstream os.Chdir calls. Without this, an operator
	// setting OBSERVABILITY_AUDIT_PATH="logs/audit.jsonl" and a service
	// runtime that chdirs (e.g. systemd WorkingDirectory) would silently
	// land the audit log outside the operator's expected chmod scope.
	// (PR #234 review L-1 — extends the prior L-2 fix to operator-set paths.)
	if !filepath.IsAbs(path) {
		abs, err := filepath.Abs(path)
		if err != nil {
			return nil, fmt.Errorf("resolve audit log path %q: %w", path, err)
		}
		path = abs
	}
	if dir := filepath.Dir(path); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return nil, fmt.Errorf("create audit log directory %q: %w", dir, err)
		}
	}
	return security.NewFileAuditLogger(path, security.WithLogger(logger))
}
