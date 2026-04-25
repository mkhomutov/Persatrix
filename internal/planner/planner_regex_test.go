package planner

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

// --- Regex and format tests ---

func TestStepIDRegex_ValidIDs(t *testing.T) {
	valid := []string{"a", "s1", "plan", "code-review", "step_1", "a1b2c3"}
	for _, id := range valid {
		assert.True(t, stepIDRegex.MatchString(id), "expected valid: %q", id)
	}
}

func TestStepIDRegex_InvalidIDs(t *testing.T) {
	invalid := []string{"", "-start", "end-", "Step1", "has space", "has.dot", "a{b}", "a/b"}
	for _, id := range invalid {
		assert.False(t, stepIDRegex.MatchString(id), "expected invalid: %q", id)
	}
}

func TestResourceIDRegex_ValidIDs(t *testing.T) {
	valid := []string{"ab", "feature-builder", "v01", "a1b2"}
	for _, id := range valid {
		assert.True(t, ResourceIDRegex.MatchString(id), "expected valid: %q", id)
	}
}

func TestResourceIDRegex_InvalidIDs(t *testing.T) {
	// F-60-R2-7: explicit "-a" edge case (leading hyphen, previously only
	// covered implicitly by "-start").
	invalid := []string{"", "-start", "end-", "A-B", "has space", "-", "a-", "-a"}
	for _, id := range invalid {
		assert.False(t, ResourceIDRegex.MatchString(id), "expected invalid: %q", id)
	}
}
