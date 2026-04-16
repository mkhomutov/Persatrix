package defaults

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestDefaults_AllPositive(t *testing.T) {
	assert.Greater(t, DefaultMaxLLMCalls, 0, "DefaultMaxLLMCalls must be positive")
	assert.Greater(t, DefaultMaxTokens, 0, "DefaultMaxTokens must be positive")
	assert.Greater(t, DefaultTimeoutSeconds, 0, "DefaultTimeoutSeconds must be positive")
	assert.Greater(t, DefaultTransportMargin, 0, "DefaultTransportMargin must be positive")
}

func TestDefaults_MinRetryBudgetFraction_InRange(t *testing.T) {
	assert.Greater(t, MinRetryBudgetFraction, 0.0, "MinRetryBudgetFraction must be > 0")
	assert.Less(t, MinRetryBudgetFraction, 1.0, "MinRetryBudgetFraction must be < 1")
}

func TestDefaults_RevisedValues(t *testing.T) {
	// Per RFC 0006 Section B: max_llm_calls lowered to 5, max_tokens raised to 8192.
	assert.Equal(t, 5, DefaultMaxLLMCalls, "DefaultMaxLLMCalls per RFC 0006 Section B")
	assert.Equal(t, 8192, DefaultMaxTokens, "DefaultMaxTokens per RFC 0006 Section B")
	assert.Equal(t, 60, DefaultTimeoutSeconds, "DefaultTimeoutSeconds per RFC 0006 Section D")
}
