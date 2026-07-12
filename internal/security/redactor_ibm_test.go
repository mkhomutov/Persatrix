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
		{"colon", "WATSONX_API_KEY: " + key},
		// RFC 0053 review follow-up — the label leaks with SPACE separators too
		// (an `ibmcloud` CLI emit / an env dump / prose), which the strict
		// `[_-]?`-label + `[:=]`-separator form missed. The `watsonx` anchor
		// makes broadening the label to `[ _-]?` and the separator to `[:=\s]+`
		// safe (it cannot fire on generic prose, unlike the shared pattern).
		{"space-label-colon", "WATSONX API KEY: " + key},
		{"space-label-column", "WATSONX API KEY   " + key},
		{"underscore-label-ws", "WATSONX_API_KEY   " + key},
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

// The broadened watsonx label/separator must not over-redact: the `watsonx…
// api…key` anchor plus the `{20,}` value floor means a short word after the
// label (prose, not a key) is left intact, and a context-free bare key with NO
// watsonx label is deliberately NOT shape-matched (IBM keys have no prefix, so
// a blind match would corrupt git SHAs / base64url blobs — the documented
// residual: such a key is only scrubbed when it leaks with a label).
func TestRedact_WatsonxAPIKey_NoOverRedaction(t *testing.T) {
	r := NewSecretRedactor()
	key := "AbCdEf0123456789_ghIjKlMnOpQrStUvWxYz-012345"

	cases := []struct {
		name string
		in   string
	}{
		// Label present but the following token is a short word, not a key.
		{"short-word-after-label", "watsonx api key rotated successfully"},
		// A bare, label-less IBM-shaped key: intentionally out of scope.
		{"bare-unlabelled-key", "request completed for " + key},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := r.Redact(tc.in); got != tc.in {
				t.Errorf("Redact(%q) = %q; want unchanged (no over-redaction)", tc.in, got)
			}
		})
	}
}
