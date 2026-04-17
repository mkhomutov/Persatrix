package main

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

// TestResolveDeadlineMode verifies the deadline mode inference logic extracted
// from main() for testability. (PR 5a, S11)
func TestResolveDeadlineMode(t *testing.T) {
	tests := []struct {
		name     string
		explicit string
		env      string
		want     string
	}{
		{
			name:     "explicit static overrides env",
			explicit: "static",
			env:      "development",
			want:     "static",
		},
		{
			name:     "explicit derived overrides env",
			explicit: "derived",
			env:      "production",
			want:     "derived",
		},
		{
			name:     "production defaults to static",
			explicit: "",
			env:      "production",
			want:     "static",
		},
		{
			name:     "development defaults to derived",
			explicit: "",
			env:      "development",
			want:     "derived",
		},
		{
			name:     "staging defaults to derived",
			explicit: "",
			env:      "staging",
			want:     "derived",
		},
		{
			name:     "unknown env defaults to derived",
			explicit: "",
			env:      "test",
			want:     "derived",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := resolveDeadlineMode(tt.explicit, tt.env)
			assert.Equal(t, tt.want, got)
		})
	}
}
