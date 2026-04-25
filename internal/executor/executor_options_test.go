package executor

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/registry"
)

func TestNewGRPCExecutor_Defaults(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, nil)

	assert.Equal(t, 30*time.Second, exec.timeout)
	assert.Equal(t, 3, exec.maxRetries)
	assert.NotNil(t, exec.logger)
}

func TestNewGRPCExecutor_WithOptions(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(),
		WithTimeout(5*time.Second),
		WithMaxRetries(1),
	)

	assert.Equal(t, 5*time.Second, exec.timeout)
	assert.Equal(t, 1, exec.maxRetries)
}

func TestGRPCExecutor_Close(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop())

	err := exec.Close()
	assert.NoError(t, err)
}

func TestWithMaxRetries_NegativeClamped(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(), WithMaxRetries(-1))

	assert.Equal(t, 0, exec.maxRetries)
}

func TestWithTimeout_ZeroClamped(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(), WithTimeout(0))

	assert.Equal(t, time.Second, exec.timeout)
}

func TestWithTimeout_NegativeClamped(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(), WithTimeout(-5*time.Second))

	assert.Equal(t, time.Second, exec.timeout)
}

func TestWithTimeout_FiveMinutes(t *testing.T) {
	// Validates the production config: 5-minute timeout for multi-iteration
	// LLM tool loops that exceed the default 30s.
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(), WithTimeout(5*time.Minute))

	assert.Equal(t, 5*time.Minute, exec.timeout)
}

func TestWithDeadlineMode(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(), WithDeadlineMode(DeadlineModeDerived))
	assert.Equal(t, DeadlineModeDerived, exec.deadlineMode)
}

func TestNewGRPCExecutor_DefaultsToStaticMode(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop())
	assert.Equal(t, DeadlineModeStatic, exec.deadlineMode)
}

// TestNewGRPCExecutor_UnrecognizedDeadlineMode verifies that an unrecognized
// deadline mode string falls back to static mode with a warning log, matching
// the documented contract on WithDeadlineMode.
func TestNewGRPCExecutor_UnrecognizedDeadlineMode(t *testing.T) {
	reg := registry.NewInMemoryRegistry(zap.NewNop())
	exec := NewGRPCExecutor(reg, zap.NewNop(), WithDeadlineMode("invalid"))

	assert.Equal(t, DeadlineModeStatic, exec.deadlineMode,
		"unrecognized mode should fall back to static")
}
