package security

import (
	"strings"
	"testing"
)

// RFC 0053 — the watsonx.ai IAM key (WATSONX_API_KEY). Unlike Google's `AIza…`,
// IBM Cloud IAM keys carry NO distinctive standalone prefix, so — rather than
// shape-match a bare key and over-redact ordinary tokens — the redactor pins
// the specific `WATSONX_API_KEY=<value>` assignment surface this RFC introduces
// with a NAMED pattern registered before `generic-secret`, so the watsonx
// secret is attributed to `[REDACTED:watsonx-api-key]` (not the generic
// fallback) and covered by a regression guard. This lives in its own file
// because redactor_test.go is at the 500-line review cap (the sibling
// redactor_google_test.go split precedent).
func TestRedact_WatsonxAPIKey(t *testing.T) {
	r := NewSecretRedactor()
	// A 44-char IBM-key-shaped body over [A-Za-z0-9_-] with no distinctive
	// prefix — deliberately NOT a secret-shaped literal on its own.
	key := "AbCdEf0123456789_ghIjKlMnOpQrStUvWxYz-012345"

	cases := []struct {
		name string
		in   string
	}{
		// Registered before generic-secret, so `WATSONX_API_KEY=…` is scrubbed
		// as the specific watsonx pattern, not the generic fallback (the
		// openai/google precedent).
		{"env", "WATSONX_API_KEY=" + key},
		{"env-lower", "watsonx_api_key=" + key},
		{"env-quoted", `WATSONX_API_KEY="` + key + `"`},
		{"json", `{"watsonx_api_key":"` + key + `"}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := r.Redact(tc.in)
			if !strings.Contains(got, "[REDACTED:watsonx-api-key]") {
				t.Errorf("Redact(%q) = %q; want to contain %q",
					tc.in, got, "[REDACTED:watsonx-api-key]")
			}
			if strings.Contains(got, key) {
				t.Errorf("Redact(%q) = %q; the raw key leaked", tc.in, got)
			}
		})
	}
}
