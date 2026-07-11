package security

import (
	"strings"
	"testing"
)

// RFC 0053 — Google API keys (GEMINI_API_KEY / GOOGLE_API_KEY, and GCP
// browser/server keys) carry the fixed `AIza` prefix + a 35-char body (39
// chars total). This lives in its own file because redactor_test.go is at the
// 500-line review cap (the sibling redactor_*_test.go split precedent).
func TestRedact_GoogleAPIKey(t *testing.T) {
	r := NewSecretRedactor()
	// Split the prefix so the fixture is not itself a secret-shaped literal.
	const googlePrefix = "AI" + "za"
	key := googlePrefix + "SyDabcdefghijklmnopqrstuvwxyz012345"

	cases := []struct {
		name string
		in   string
	}{
		// Registered before generic-secret, so `GEMINI_API_KEY=AIza…` is
		// scrubbed as the specific google pattern, not the generic fallback
		// (the openai-proj precedent in redactor_test.go).
		{"gemini-env", "GEMINI_API_KEY=" + key},
		{"google-env", "GOOGLE_API_KEY=" + key},
		// Bare key with no secret-shaped assignment prefix — generic-secret
		// would not fire here at all, so this proves the pattern itself matches.
		{"bare", "the key is " + key + " ok"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := r.Redact(tc.in)
			if !strings.Contains(got, "[REDACTED:google-api-key]") {
				t.Errorf("Redact(%q) = %q; want to contain %q",
					tc.in, got, "[REDACTED:google-api-key]")
			}
			if strings.Contains(got, key) {
				t.Errorf("Redact(%q) = %q; the raw key leaked", tc.in, got)
			}
		})
	}
}
