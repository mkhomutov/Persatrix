package planner

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"
)

func nopLogger() *zap.Logger {
	return zap.NewNop()
}

func observedLogger() (*zap.Logger, *observer.ObservedLogs) {
	core, logs := observer.New(zapcore.WarnLevel)
	return zap.New(core), logs
}

// --- ResolveInputs: step output references ---

func TestResolveInputs_StepOutput(t *testing.T) {
	step := Step{ID: "review", Input: "Review this: {{ steps.implement.output }}"}
	outputs := map[string]string{"implement": "func main() {}"}

	result, err := ResolveInputs(step, outputs, nil, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "Review this: func main() {}", result)
}

func TestResolveInputs_MultipleStepOutputs(t *testing.T) {
	step := Step{
		ID:    "revise",
		Input: "{{ steps.implement.output }}\nFeedback: {{ steps.review.output }}",
	}
	outputs := map[string]string{
		"implement": "code here",
		"review":    "needs changes",
	}

	result, err := ResolveInputs(step, outputs, nil, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "code here\nFeedback: needs changes", result)
}

func TestResolveInputs_MissingStepOutput(t *testing.T) {
	step := Step{ID: "review", Input: "{{ steps.missing.output }}"}

	_, err := ResolveInputs(step, map[string]string{}, nil, nopLogger())
	require.Error(t, err)
	assert.Contains(t, err.Error(), "unresolved step output reference")
	assert.Contains(t, err.Error(), "missing")
}

// --- ResolveInputs: variable references ---

func TestResolveInputs_Variable(t *testing.T) {
	step := Step{ID: "plan", Input: "{{ user_request }}"}
	vars := map[string]string{"user_request": "build a login page"}

	result, err := ResolveInputs(step, nil, vars, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "build a login page", result)
}

func TestResolveInputs_MissingVariable(t *testing.T) {
	step := Step{ID: "plan", Input: "{{ missing_var }}"}

	_, err := ResolveInputs(step, nil, map[string]string{}, nopLogger())
	require.Error(t, err)
	assert.Contains(t, err.Error(), "unresolved variable reference")
	assert.Contains(t, err.Error(), "missing_var")
}

// --- ResolveInputs: mixed references ---

func TestResolveInputs_MixedStepAndVariable(t *testing.T) {
	step := Step{
		ID:    "implement",
		Input: "Request: {{ user_request }}\nPlan: {{ steps.plan.output }}",
	}
	outputs := map[string]string{"plan": "step 1, step 2"}
	vars := map[string]string{"user_request": "build auth"}

	result, err := ResolveInputs(step, outputs, vars, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "Request: build auth\nPlan: step 1, step 2", result)
}

// --- ResolveInputs: passthrough (no templates) ---

func TestResolveInputs_NoTemplates(t *testing.T) {
	step := Step{ID: "simple", Input: "just a plain string"}

	result, err := ResolveInputs(step, nil, nil, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "just a plain string", result)
}

func TestResolveInputs_EmptyInput(t *testing.T) {
	step := Step{ID: "empty", Input: ""}

	result, err := ResolveInputs(step, nil, nil, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "", result)
}

// --- ResolveInputs: single template as entire input ---

func TestResolveInputs_SingleTemplateInput(t *testing.T) {
	step := Step{ID: "plan", Input: "{{ user_request }}"}
	vars := map[string]string{"user_request": "hello world"}

	result, err := ResolveInputs(step, nil, vars, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "hello world", result)
}

// --- ResolveInputs: empty/nil maps ---

func TestResolveInputs_NilMaps(t *testing.T) {
	step := Step{ID: "simple", Input: "no templates here"}

	result, err := ResolveInputs(step, nil, nil, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "no templates here", result)
}

func TestResolveInputs_EmptyMaps(t *testing.T) {
	step := Step{ID: "simple", Input: "no templates here"}

	result, err := ResolveInputs(step, map[string]string{}, map[string]string{}, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "no templates here", result)
}

// --- ResolveInputs: extra unused outputs ---

func TestResolveInputs_ExtraUnusedOutputs(t *testing.T) {
	step := Step{ID: "review", Input: "{{ steps.plan.output }}"}
	outputs := map[string]string{
		"plan":      "the plan",
		"implement": "unused code",
		"review":    "unused review",
	}

	result, err := ResolveInputs(step, outputs, nil, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "the plan", result)
}

// --- ResolveInputs: malformed patterns passthrough ---

func TestResolveInputs_MalformedPatternsPassthrough(t *testing.T) {
	tests := []struct {
		name  string
		input string
	}{
		{"empty braces", "{{ }}"},
		{"incomplete step ref", "{{ steps. }}"},
		{"hyphen in variable name", "{{no-spaces}}"},
		{"expression in condition", "{{ steps.review.output.approved == false }}"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			step := Step{ID: "test", Input: tt.input}
			result, err := ResolveInputs(step, map[string]string{}, map[string]string{}, nopLogger())
			require.NoError(t, err)
			assert.Equal(t, tt.input, result, "malformed pattern should pass through unchanged")
		})
	}
}

// --- ResolveInputs: single-pass (no re-scan) ---

func TestResolveInputs_SinglePassNoRescan(t *testing.T) {
	// Output value itself contains a template pattern — must NOT be re-resolved.
	step := Step{ID: "next", Input: "{{ steps.prev.output }}"}
	outputs := map[string]string{"prev": "{{ user_request }}"}
	vars := map[string]string{"user_request": "should not appear"}

	result, err := ResolveInputs(step, outputs, vars, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "{{ user_request }}", result, "substituted values must not be re-scanned")
}

// --- ResolveInputs: suspicious pattern warning ---

func TestResolveInputs_SuspiciousPatternWarning(t *testing.T) {
	logger, logs := observedLogger()

	step := Step{ID: "test", Input: "{{ steps.review.output.approved == false }}"}
	result, err := ResolveInputs(step, map[string]string{}, map[string]string{}, logger)
	require.NoError(t, err)
	assert.Equal(t, step.Input, result)

	require.Equal(t, 1, logs.Len(), "expected exactly one warning")
	entry := logs.All()[0]
	assert.Equal(t, zapcore.WarnLevel, entry.Level)
	assert.Contains(t, entry.Message, "suspicious")
}

func TestResolveInputs_NoWarningForValidTemplates(t *testing.T) {
	logger, logs := observedLogger()

	step := Step{ID: "review", Input: "{{ steps.plan.output }}"}
	outputs := map[string]string{"plan": "the plan"}

	_, err := ResolveInputs(step, outputs, nil, logger)
	require.NoError(t, err)

	assert.Equal(t, 0, logs.Len(), "valid templates should not produce warnings")
}

func TestResolveInputs_WarningFromSubstitutedValue(t *testing.T) {
	// A substituted value containing {{ }} should trigger a warning
	// (it passes through un-resolved since we don't re-scan).
	logger, logs := observedLogger()

	step := Step{ID: "next", Input: "{{ steps.prev.output }}"}
	outputs := map[string]string{"prev": "value with {{ suspicious }}"}

	result, err := ResolveInputs(step, outputs, nil, logger)
	require.NoError(t, err)
	assert.Equal(t, "value with {{ suspicious }}", result)

	// The suspicious {{ suspicious }} in the result triggers a warning.
	require.Equal(t, 1, logs.Len())
	assert.Contains(t, logs.All()[0].Message, "suspicious")
}

// --- ResolveInputs: multi-line block scalar ---

func TestResolveInputs_MultiLineInput(t *testing.T) {
	step := Step{
		ID:    "revise",
		Input: "Code:\n{{ steps.implement.output }}\n\nReview:\n{{ steps.review.output }}",
	}
	outputs := map[string]string{
		"implement": "func main() {\n\tfmt.Println(\"hello\")\n}",
		"review":    "LGTM\nApproved",
	}

	result, err := ResolveInputs(step, outputs, nil, nopLogger())
	require.NoError(t, err)
	expected := "Code:\nfunc main() {\n\tfmt.Println(\"hello\")\n}\n\nReview:\nLGTM\nApproved"
	assert.Equal(t, expected, result)
}

// --- ResolveInputs: whitespace variations ---

func TestResolveInputs_WhitespaceVariations(t *testing.T) {
	tests := []struct {
		name     string
		input    string
		expected string
	}{
		{"extra spaces", "{{  user_request  }}", "hello"},
		{"tabs", "{{\tuser_request\t}}", "hello"},
		{"mixed whitespace", "{{  \t user_request  \t }}", "hello"},
		{"single space", "{{ user_request }}", "hello"},
	}

	vars := map[string]string{"user_request": "hello"}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			step := Step{ID: "test", Input: tt.input}
			result, err := ResolveInputs(step, nil, vars, nopLogger())
			require.NoError(t, err)
			assert.Equal(t, tt.expected, result)
		})
	}
}

// --- ResolveInputs: step ID edge cases ---

func TestResolveInputs_StepIDWithUnderscoresAndHyphens(t *testing.T) {
	step := Step{ID: "test", Input: "{{ steps.my-step_01.output }}"}
	outputs := map[string]string{"my-step_01": "result"}

	result, err := ResolveInputs(step, outputs, nil, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "result", result)
}

func TestResolveInputs_SingleCharStepID(t *testing.T) {
	step := Step{ID: "test", Input: "{{ steps.a.output }}"}
	outputs := map[string]string{"a": "result"}

	result, err := ResolveInputs(step, outputs, nil, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "result", result)
}

// --- ResolveInputs: variable name edge cases ---

func TestResolveInputs_VariableWithUnderscore(t *testing.T) {
	step := Step{ID: "test", Input: "{{ _private }}"}
	vars := map[string]string{"_private": "secret"}

	result, err := ResolveInputs(step, nil, vars, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "secret", result)
}

// --- templateRegex reuses stepIDPattern ---

func TestTemplateRegex_UsesStepIDPattern(t *testing.T) {
	// Verify that the template regex accepts the same step ID formats as stepIDRegex.
	validIDs := []string{"a", "ab", "a-b", "a_b", "step-01", "my_step_2"}
	for _, id := range validIDs {
		assert.Regexp(t, stepIDRegex, id, "stepIDRegex should match %q", id)
		input := "{{ steps." + id + ".output }}"
		assert.Regexp(t, templateRegex, input, "templateRegex should match %q", input)
	}

	// Invalid step IDs should fail both.
	invalidIDs := []string{"-bad", "bad-", "_bad", "bad_", "BAD", "has space"}
	for _, id := range invalidIDs {
		assert.NotRegexp(t, stepIDRegex, id, "stepIDRegex should NOT match %q", id)
		input := "{{ steps." + id + ".output }}"
		assert.NotRegexp(t, templateRegex, input, "templateRegex should NOT match %q", input)
	}
}

// --- ResolveInputs: nil logger guard ---

func TestResolveInputs_NilLogger(t *testing.T) {
	step := Step{ID: "test", Input: "{{ steps.plan.output }}"}
	outputs := map[string]string{"plan": "the plan"}

	result, err := ResolveInputs(step, outputs, nil, nil)
	require.NoError(t, err)
	assert.Equal(t, "the plan", result)
}

func TestResolveInputs_NilLoggerWithSuspiciousPattern(t *testing.T) {
	step := Step{ID: "test", Input: "{{ steps.plan.output }} and {{ BAD_PATTERN }}"}
	outputs := map[string]string{"plan": "the plan"}

	result, err := ResolveInputs(step, outputs, nil, nil)
	require.NoError(t, err)
	assert.Equal(t, "the plan and {{ BAD_PATTERN }}", result)
}

// --- ResolveInputs: adjacent templates ---

func TestResolveInputs_AdjacentTemplates(t *testing.T) {
	step := Step{ID: "test", Input: "{{ user_request }}{{ project_name }}"}
	vars := map[string]string{"user_request": "hello", "project_name": "world"}

	result, err := ResolveInputs(step, nil, vars, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "helloworld", result)
}

// --- ResolveInputs: empty-string substitution ---

func TestResolveInputs_EmptyStringSubstitution(t *testing.T) {
	step := Step{ID: "test", Input: "prefix {{ steps.plan.output }} suffix"}
	outputs := map[string]string{"plan": ""}

	result, err := ResolveInputs(step, outputs, nil, nopLogger())
	require.NoError(t, err)
	assert.Equal(t, "prefix  suffix", result)
}
