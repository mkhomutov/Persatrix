package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/observability/zapenc"
)

// TestValidateStartupFlags pins the env / --deadline-mode / PERSATRIX_LOG_FORMAT
// startup-flag validation extracted from main() (ISSUE-0008). The pre-extraction
// implementation inlined three sequential `switch + os.Exit(1)` blocks; the
// helper consolidates them into a single error-returning function so main()
// stays under the 500-line review-friendly cap and the validation rules stay
// independently testable without launching a subprocess.
func TestValidateStartupFlags(t *testing.T) {
	tests := []struct {
		name         string
		env          string
		deadlineMode string
		logFormat    string
		wantErr      string // substring match; empty == expect nil
	}{
		// Happy paths — every accepted combination must validate cleanly.
		{name: "development+derived+empty-log-format", env: "development", deadlineMode: "derived", logFormat: ""},
		{name: "staging+derived+json", env: "staging", deadlineMode: "derived", logFormat: zapenc.JSONEnvValue},
		{name: "production+static+pretty", env: "production", deadlineMode: "static", logFormat: zapenc.PrettyEnvValue},

		// Bad --env surfaces with the exact phrasing the operator used to see
		// from the inline switch in main(). Anchoring the substring on
		// "invalid --env value" pins the message — operators grep for it.
		{name: "typo env", env: "test", deadlineMode: "derived", logFormat: "", wantErr: "invalid --env value: test"},
		{name: "empty env", env: "", deadlineMode: "derived", logFormat: "", wantErr: "invalid --env value:"},

		// Bad --deadline-mode. Caller is responsible for resolving an empty
		// string via resolveDeadlineMode before calling — an empty value here
		// reaches us only on a defaulting bug and must not pass silently.
		{name: "typo deadline", env: "production", deadlineMode: "dervied", logFormat: "", wantErr: "invalid --deadline-mode value: dervied"},
		{name: "empty deadline", env: "production", deadlineMode: "", logFormat: "", wantErr: "invalid --deadline-mode value:"},

		// Bad PERSATRIX_LOG_FORMAT must mention the env-var name so a fat-finger
		// (PERSATRIX_LOG_FORMAT=preety) is recognisable at a glance in a
		// startup-failure log line.
		{name: "typo log format", env: "production", deadlineMode: "static", logFormat: "preety", wantErr: "PERSATRIX_LOG_FORMAT"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateStartupFlags(tt.env, tt.deadlineMode, tt.logFormat)
			if tt.wantErr == "" {
				assert.NoError(t, err)
				return
			}
			require.Error(t, err)
			assert.Contains(t, err.Error(), tt.wantErr)
		})
	}
}

// TestResolveWorkflowsDir pins the abs+EvalSymlinks resolution main() used
// to perform inline. The helper is the single point that translates the
// operator-supplied --workflows-dir into the canonical path that both the
// HTTP server and the scheduler must agree on (PR #33 review F-01).
func TestResolveWorkflowsDir(t *testing.T) {
	t.Run("relative path becomes absolute and canonical", func(t *testing.T) {
		// Build an actual directory; EvalSymlinks fails on non-existent paths.
		base := t.TempDir()
		workflows := filepath.Join(base, "workflows")
		require.NoError(t, os.MkdirAll(workflows, 0o755))

		// Use a relative path by chdir'ing into base; restore on exit so the
		// test does not pollute sibling tests sharing the package's working dir.
		origWD, err := os.Getwd()
		require.NoError(t, err)
		t.Cleanup(func() {
			_ = os.Chdir(origWD)
		})
		require.NoError(t, os.Chdir(base))

		got, err := resolveWorkflowsDir("workflows")
		require.NoError(t, err)

		assert.True(t, filepath.IsAbs(got), "resolved path must be absolute, got %q", got)

		// EvalSymlinks(workflows) is the truth oracle — match against it
		// rather than the raw join so the test passes on hosts where the
		// temp dir itself is symlinked (macOS /var → /private/var).
		want, err := filepath.EvalSymlinks(workflows)
		require.NoError(t, err)
		assert.Equal(t, want, got)
	})

	t.Run("missing dir returns canonicalize error", func(t *testing.T) {
		// filepath.Abs succeeds for any string; EvalSymlinks is the gate that
		// surfaces "directory does not exist" at startup. A missing
		// --workflows-dir must abort init rather than silently degrade — both
		// the server and the scheduler need a valid path.
		_, err := resolveWorkflowsDir(filepath.Join(t.TempDir(), "does-not-exist"))
		require.Error(t, err)
		assert.Contains(t, err.Error(), "canonicalize")
	})
}
