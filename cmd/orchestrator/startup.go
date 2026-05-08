package main

import (
	"fmt"
	"path/filepath"

	"github.com/mkhomutov/persatrix/internal/observability/zapenc"
)

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
