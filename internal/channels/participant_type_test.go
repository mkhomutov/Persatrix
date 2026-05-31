package channels

// Unit tests for the participant-type vocabulary anchor
// (`IsValidParticipantType`) — the Go mirror of Python's
// `participant.VALID_PARTICIPANT_TYPES`. The REST chat handler uses this
// to reject an out-of-vocabulary `participant_type` at the request
// boundary (ISSUE-0068 / [RFC 0011 participant-type amendment]).
//
// [RFC 0011 participant-type amendment]: ../../docs/rfcs/0011-amendment-participant-type-wire-propagation.md

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

// TestIsValidParticipantType pins the canonical vocabulary: exactly
// "agent" and "user" are valid; the empty string and any other value are
// not. Empty is intentionally invalid here — callers apply their own
// default (REST chat defaults an omitted field to "user") *before*
// validating, so the validator never sees a legitimately empty value.
func TestIsValidParticipantType(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"agent", true},
		{"user", true},
		{"", false},      // omitted is the caller's default, not this fn's job
		{"User", false},  // case-sensitive — a typo must not slip through
		{"human", false}, // plausible-but-wrong synonym
		{"robot", false},
	}
	for _, tc := range cases {
		assert.Equalf(t, tc.want, IsValidParticipantType(tc.in),
			"IsValidParticipantType(%q)", tc.in)
	}
}
