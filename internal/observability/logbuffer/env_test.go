package logbuffer

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

// TestConfigFromEnv exercises every PERSATRIX_LOGBUFFER_* knob the
// orchestrator entry point promises, including the "empty / malformed
// values fall through to the default" contract documented in
// ConfigFromEnv. Added per PR #173 review nice-to-have: the seven-knob
// mapping is mechanically correct but was previously uncovered.
func TestConfigFromEnv(t *testing.T) {
	defaults := Defaults()

	cases := []struct {
		name   string
		env    map[string]string
		assert func(*testing.T, Config)
	}{
		{
			name: "all defaults when env unset",
			env:  map[string]string{},
			assert: func(t *testing.T, cfg Config) {
				assert.Equal(t, defaults, cfg)
			},
		},
		{
			name: "PER_EXEC override",
			env:  map[string]string{"PERSATRIX_LOGBUFFER_PER_EXEC": "42"},
			assert: func(t *testing.T, cfg Config) {
				assert.Equal(t, 42, cfg.PerExecution)
				assert.Equal(t, defaults.MaxExecutions, cfg.MaxExecutions)
			},
		},
		{
			name: "MAX_EXEC override",
			env:  map[string]string{"PERSATRIX_LOGBUFFER_MAX_EXEC": "7"},
			assert: func(t *testing.T, cfg Config) {
				assert.Equal(t, 7, cfg.MaxExecutions)
			},
		},
		{
			name: "DIR override",
			env:  map[string]string{"PERSATRIX_LOGBUFFER_DIR": "/tmp/persatrix-logs"},
			assert: func(t *testing.T, cfg Config) {
				assert.Equal(t, "/tmp/persatrix-logs", cfg.Dir)
			},
		},
		{
			name: "DISK_MB override is converted to bytes",
			env:  map[string]string{"PERSATRIX_LOGBUFFER_DISK_MB": "8"},
			assert: func(t *testing.T, cfg Config) {
				assert.Equal(t, int64(8*1024*1024), cfg.DiskCapBytes)
			},
		},
		{
			name: "DROP_LEVEL override",
			env:  map[string]string{"PERSATRIX_LOGBUFFER_DROP_LEVEL": "WARN"},
			assert: func(t *testing.T, cfg Config) {
				assert.Equal(t, "WARN", cfg.DropLevel)
			},
		},
		{
			name: "RATE_PER_EXEC override",
			env:  map[string]string{"PERSATRIX_LOGBUFFER_RATE_PER_EXEC": "200"},
			assert: func(t *testing.T, cfg Config) {
				assert.Equal(t, 200, cfg.RatePerExec)
			},
		},
		{
			name: "SUBSCRIBERS override",
			env:  map[string]string{"PERSATRIX_LOGBUFFER_SUBSCRIBERS": "16"},
			assert: func(t *testing.T, cfg Config) {
				assert.Equal(t, 16, cfg.MaxSubscribers)
			},
		},
		{
			name: "malformed integer falls through to default",
			env:  map[string]string{"PERSATRIX_LOGBUFFER_PER_EXEC": "not-a-number"},
			assert: func(t *testing.T, cfg Config) {
				assert.Equal(t, defaults.PerExecution, cfg.PerExecution)
			},
		},
		{
			name: "non-positive integer falls through to default",
			env:  map[string]string{"PERSATRIX_LOGBUFFER_RATE_PER_EXEC": "0"},
			assert: func(t *testing.T, cfg Config) {
				assert.Equal(t, defaults.RatePerExec, cfg.RatePerExec)
			},
		},
		{
			name: "negative integer falls through to default",
			env:  map[string]string{"PERSATRIX_LOGBUFFER_MAX_EXEC": "-3"},
			assert: func(t *testing.T, cfg Config) {
				assert.Equal(t, defaults.MaxExecutions, cfg.MaxExecutions)
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			for k, v := range tc.env {
				t.Setenv(k, v)
			}
			cfg := ConfigFromEnv()
			tc.assert(t, cfg)
		})
	}
}
