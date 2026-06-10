package channels

import (
	"errors"
	"testing"
)

// TestResolveMemberPolicy_Table pins the full declared-value contract of
// the single write-boundary constructor: for every vocabulary value, the
// resolved triple {canonical policy, salience_gated, threshold} matches
// what the previously hand-paired ResolveSalienceSignal +
// canonicalRespondPolicy call sites produced. The constructor MUST derive
// the salience signals from the *declared* disposition before normalizing
// — the ordering constraint every call site previously had to remember —
// so `participant` resolves gated even though its canonical `always`
// form, declared bare, would not.
//
// The constructor takes no explicit threshold: every boundary that goes
// through it (the store's AddMember/SetMemberPolicy, the REST create
// handler) carries only the disposition. The one source of operator
// thresholds — the config loader — bypasses the constructor by design,
// and its threshold semantics (`always`+threshold opt-in, chair
// override, threshold-on-non-gating rejection) are pinned at that
// boundary in config_salience_test.go / config_threshold_test.go.
func TestResolveMemberPolicy_Table(t *testing.T) {
	cases := []struct {
		name          string
		declared      RespondPolicy
		wantPolicy    RespondPolicy
		wantGated     bool
		wantThreshold *float64
	}{
		// Legacy triple: identity normalization, no bid (a bare `always`
		// keeps replying unconditionally — v0.3.7 back-compat).
		{"when_mentioned", RespondWhenMentioned, RespondWhenMentioned, false, nil},
		{"always bare keeps replying unconditionally", RespondAlways, RespondAlways, false, nil},
		{"never", RespondNever, RespondNever, false, nil},
		// Dispositions: collapse to the legacy triple; the open-floor
		// pair opts into the bid off the *declared* value.
		{"participant bids with unset threshold (bias-to-silence)", RespondParticipant, RespondAlways, true, nil},
		{"addressed", RespondAddressed, RespondWhenMentioned, false, nil},
		{"observer", RespondObserver, RespondNever, false, nil},
		// `chair` is a participant whose unset threshold picks up the low
		// facilitator default — its whole wire-visible identity.
		{"chair defaults to the low threshold", RespondChair, RespondAlways, true, ptrTo(DefaultChairThreshold)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ResolveMemberPolicy(tc.declared)
			if err != nil {
				t.Fatalf("ResolveMemberPolicy(%q) returned unexpected error: %v", tc.declared, err)
			}
			if got.Policy != tc.wantPolicy {
				t.Errorf("Policy = %q, want %q", got.Policy, tc.wantPolicy)
			}
			if got.SalienceGated != tc.wantGated {
				t.Errorf("SalienceGated = %v, want %v", got.SalienceGated, tc.wantGated)
			}
			switch {
			case tc.wantThreshold == nil && got.Threshold != nil:
				t.Errorf("Threshold = %v, want nil", *got.Threshold)
			case tc.wantThreshold != nil && got.Threshold == nil:
				t.Errorf("Threshold = nil, want %v", *tc.wantThreshold)
			case tc.wantThreshold != nil && *got.Threshold != *tc.wantThreshold:
				t.Errorf("Threshold = %v, want %v", *got.Threshold, *tc.wantThreshold)
			}
		})
	}
}

// TestResolveMemberPolicy_RejectsUnknown pins the validating half: an
// unknown declared value is rejected with ErrInvalidRespondPolicy (the
// same sentinel the store write paths surfaced before centralization),
// and the zero MemberPolicy is returned so a caller that ignores the
// error cannot smuggle a half-resolved triple to the CHECK constraint.
func TestResolveMemberPolicy_RejectsUnknown(t *testing.T) {
	for _, declared := range []RespondPolicy{"", "sometimes", "ALWAYS", "Participant"} {
		got, err := ResolveMemberPolicy(declared)
		if !errors.Is(err, ErrInvalidRespondPolicy) {
			t.Errorf("ResolveMemberPolicy(%q) error = %v, want ErrInvalidRespondPolicy", declared, err)
		}
		if got != (MemberPolicy{}) {
			t.Errorf("ResolveMemberPolicy(%q) = %+v, want zero MemberPolicy on error", declared, got)
		}
	}
}

// ptrTo is a test-local helper for pointer literals in table cases.
func ptrTo(v float64) *float64 { return &v }
