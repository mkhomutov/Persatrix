package redact

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestNoopRedactor_ReturnsInputUnchanged(t *testing.T) {
	r := NoopRedactor{}
	in := map[string]any{"k": "v", "n": 42}
	out := r.Redact(in)

	assert.Equal(t, in, out)
}

func TestNoopRedactor_SatisfiesInterface(t *testing.T) {
	var _ Redactor = NoopRedactor{}
}
