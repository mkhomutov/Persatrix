package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
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
// PERSATRIX_SESSION_ID. An unset env var logs INFO and returns the
// synthetic carve-out [channels.DefaultSessionID] (RFC 0031 OQ #2). A
// value containing characters outside [A-Za-z0-9_-] emits a WARN but is
// still accepted verbatim — Phase 3 CLI owns hard validation.
//
// The fallback identifier is sourced from [channels.DefaultSessionID]
// (not a local "legacy" literal) so the carve-out lives in one place —
// PR #335 review L2. The boot-log message hard-codes the literal in the
// human-readable text because that string is what an operator greps for
// in incident triage; the structured `session_id` field carries the
// canonical value.
func resolveSessionID(logger *zap.Logger) string {
	v := os.Getenv(sessionIDEnvVar)
	if v == "" {
		logger.Info(sessionIDEnvVar+" unset; defaulting to 'legacy' session",
			zap.String("session_id", channels.DefaultSessionID))
		return channels.DefaultSessionID
	}
	if !sessionIDPattern.MatchString(v) {
		logger.Warn(sessionIDEnvVar+" contains characters outside [A-Za-z0-9_-]; accepting verbatim (hard validation in Phase 3 CLI)",
			zap.String("session_id", v))
	}
	return v
}

// epochIDEnvVar is the env var read once at orchestrator boot to determine
// the per-process run/test-isolation epoch emitted on the `persatrix-epoch`
// gRPC header on every outbound dispatch. ISSUE-0085 PR 4 (the structural
// half of the F-3 fix).
const epochIDEnvVar = "PERSATRIX_EPOCH"

// resolveEpochID returns the per-process epoch sourced from PERSATRIX_EPOCH.
// An unset env var logs INFO and returns the canonical [channels.DefaultEpochID]
// ("live") — the single-world default that leaves production behaviour
// unchanged. A value containing characters outside [A-Za-z0-9_-] emits a WARN
// but is still accepted verbatim, mirroring [resolveSessionID]'s
// soft-validation posture: an operator stuck on the env-var knob is never
// blocked on a stricter validator landing first.
//
// The fallback is sourced from [channels.DefaultEpochID] (not a local "live"
// literal) so the cross-language sentinel lives in one place — mirroring
// [resolveSessionID]'s use of [channels.DefaultSessionID] (PR #335 review L2).
// That exported constant is the value the persona-memory epoch migration (v12)
// backfills onto pre-existing rows and is pinned to byte-match
// `agents.epoch_id.DEFAULT_EPOCH_ID` by channels' cross-language lock-step
// test; reusing it here means a single-world deployment can never split across
// two defaults (Go emitting one, Python filtering on another). The boot-log
// message hard-codes 'live' in the human-readable text because that is what an
// operator greps for in incident triage; the structured `epoch_id` field
// carries the canonical value.
//
// Unlike the session id (per-room, resolved per request by the
// SessionResolver), the epoch is a single process-global value: `live` in
// production, a per-job id in CI. It is resolved once here at boot and ferried
// to the dispatcher via [channels.WithEpoch].
func resolveEpochID(logger *zap.Logger) string {
	v := os.Getenv(epochIDEnvVar)
	if v == "" {
		logger.Info(epochIDEnvVar+" unset; defaulting to 'live' epoch",
			zap.String("epoch_id", channels.DefaultEpochID))
		return channels.DefaultEpochID
	}
	if !sessionIDPattern.MatchString(v) {
		logger.Warn(epochIDEnvVar+" contains characters outside [A-Za-z0-9_-]; accepting verbatim",
			zap.String("epoch_id", v))
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
