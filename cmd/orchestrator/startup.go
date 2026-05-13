package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/observability/zapenc"
)

// sessionIDEnvVar is the env var read at orchestrator boot to determine
// the per-process default session id stamped on every CreateChannel /
// PublishMessage write. RFC 0031 Phase 1.
const sessionIDEnvVar = "PERSATRIX_SESSION_ID"

// sessionIDPattern is the soft-validation pattern applied at boot. A
// value outside this shape emits a WARN log but is still accepted; hard
// validation lives in Phase 3 CLI's `persatrix session new` (so an
// operator stuck on the env-var fallback is never blocked on a stricter
// CLI landing first).
var sessionIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

// resolveSessionID returns the per-process session id sourced from
// PERSATRIX_SESSION_ID. An unset env var logs INFO and returns "legacy"
// (the synthetic carve-out per RFC 0031 OQ #2). A value containing
// characters outside [A-Za-z0-9_-] emits a WARN but is still accepted
// verbatim — Phase 3 CLI owns hard validation.
func resolveSessionID(logger *zap.Logger) string {
	v := os.Getenv(sessionIDEnvVar)
	if v == "" {
		logger.Info(sessionIDEnvVar+" unset; defaulting to 'legacy' session",
			zap.String("session_id", "legacy"))
		return "legacy"
	}
	if !sessionIDPattern.MatchString(v) {
		logger.Warn(sessionIDEnvVar+" contains characters outside [A-Za-z0-9_-]; accepting verbatim (hard validation in Phase 3 CLI)",
			zap.String("session_id", v))
	}
	return v
}

// validateStartupFlags rejects malformed --env, --deadline-mode, and
// PERSATRIX_LOG_FORMAT values at startup so a typo (--env=test,
// --deadline-mode=dervied, PERSATRIX_LOG_FORMAT=preety) surfaces as a clean
// non-zero exit line rather than silently falling through to a default that
// misleads incident analysis later. Extracted from main() so the rules can
// be exercised without launching a subprocess (ISSUE-0008).
//
// Caller contract: --deadline-mode must already be defaulted via
// [resolveDeadlineMode]; an empty string here is a programming error and
// returns an explicit "invalid" message instead of being silently coerced.
//
// Errors are returned (not Fatal'd or os.Exit'd) so the caller decides the
// failure surface — main() prints to stderr and exits 1, tests just assert.
func validateStartupFlags(env, deadlineMode, logFormat string) error {
	switch env {
	case "development", "staging", "production":
	default:
		return fmt.Errorf("invalid --env value: %s (must be development|staging|production)", env)
	}
	switch deadlineMode {
	case "derived", "static":
	default:
		return fmt.Errorf("invalid --deadline-mode value: %s (must be derived|static)", deadlineMode)
	}
	switch logFormat {
	case "", zapenc.JSONEnvValue, zapenc.PrettyEnvValue:
	default:
		return fmt.Errorf("invalid %s value: %s (must be json|pretty; unset == json)", zapenc.PrettyEnvVar, logFormat)
	}
	return nil
}

// resolveWorkflowsDir canonicalises --workflows-dir into an absolute,
// symlink-resolved path so the HTTP server and the scheduler reference the
// same directory regardless of CWD or symlinks. server.New internally
// re-evaluates symlinks; without this pre-resolve the scheduler stores the
// pre-EvalSymlinks form and the two surfaces drift (PR #33 review F-01).
// Extracted from main() to keep the resolution rules testable and to drop
// main.go below the 500-line review-friendly cap (ISSUE-0008).
func resolveWorkflowsDir(dir string) (string, error) {
	abs, err := filepath.Abs(dir)
	if err != nil {
		return "", fmt.Errorf("resolve --workflows-dir: %w", err)
	}
	canon, err := filepath.EvalSymlinks(abs)
	if err != nil {
		return "", fmt.Errorf("canonicalize --workflows-dir: %w", err)
	}
	return canon, nil
}
