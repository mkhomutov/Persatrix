package security

import (
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestRedact_AllDefaultPatterns(t *testing.T) {
	r := NewSecretRedactor()
	cases := []struct {
		name string
		in   string
		want string // must contain
	}{
		{"anthropic", "key=sk-ant-abcdef0123456789abcdef0123456789", "[REDACTED:anthropic-api-key]"},
		{"openai", "use sk-abcdef0123456789abcdef", "[REDACTED:openai-api-key]"},
		// PR #233 review MF-1 regression: real-world OpenAI keys carry `-`
		// and `_` in the suffix (e.g. `sk-proj-…`). The pre-fix pattern
		// truncated at the first `-` and left the rest of the secret in
		// plain text. Pin the full-key shape here.
		{"openai-proj", "OPENAI_API_KEY=sk-proj-AbCd_efgh-ijkl0123456789xyz", "[REDACTED:openai-api-key]"},
		{"bearer", "Authorization: Bearer abc.def.ghi==", "[REDACTED:bearer-token]"},
		{"aws", "AKIAABCDEFGHIJKLMNOP", "[REDACTED:aws-access-key]"},
		{"generic-secret", "password = hunter2", "[REDACTED:generic-secret]"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := r.Redact(tc.in)
			if !strings.Contains(got, tc.want) {
				t.Errorf("Redact(%q) = %q; want to contain %q", tc.in, got, tc.want)
			}
		})
	}
}

// TestRedact_AdditionalPatterns pins PR #233 review Nice-to-have #2:
// GitHub Personal Access Tokens, GCP service-account JSON keys, Slack
// bot/user tokens, and Stripe live keys are realistic for the
// orchestrator's MCP / container deployment story and must not appear
// verbatim in audit log Detail.
//
// Test fixtures intentionally split the prefix from the body via Go
// string concatenation so the source file never contains a literal
// `sk_live_…` / `xoxb-…` / `ghp_…` token shape — GitHub's push-time
// secret scanner false-positives on those literals even when they're
// obvious test data. The runtime concatenation produces the full
// secret shape that the redactor regex must still match.
func TestRedact_AdditionalPatterns(t *testing.T) {
	r := NewSecretRedactor()
	const (
		ghPrefix     = "gh" + "p_"
		ghSecPrefix  = "gh" + "s_"
		ghPatPrefix  = "github" + "_pat_"
		slackBPrefix = "xo" + "xb-"
		slackPPrefix = "xo" + "xp-"
		stripeSk     = "sk" + "_live_"
		stripePk     = "pk" + "_live_"
	)
	cases := []struct {
		name string
		in   string
		want string
	}{
		// GitHub fine-grained tokens are `github_pat_` + 22-char prefix +
		// `_` + 59-char body (= 82-char suffix). Realistic fixture:
		{"github-pat", "GH_PAT=" + ghPatPrefix + "11AAAAAAAA0BCDEFGHIJ_KLMNOPQRSTUVWXYZabcdefghij0123456789klmnopqrstuvwxyzABCDEFGH", "[REDACTED:github-token]"},
		// Classic GitHub PAT prefixes (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`)
		{"github-classic-ghp", "GH_TOKEN=" + ghPrefix + "abcdefghijklmnopqrstuvwxyz0123456789", "[REDACTED:github-token]"},
		{"github-classic-ghs", "GH_TOKEN=" + ghSecPrefix + "abcdefghijklmnopqrstuvwxyz0123456789", "[REDACTED:github-token]"},
		// Slack bot / user tokens
		{"slack-bot", "SLACK_TOKEN=" + slackBPrefix + "1234567890-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx", "[REDACTED:slack-token]"},
		{"slack-user", "SLACK_TOKEN=" + slackPPrefix + "1234567890-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx", "[REDACTED:slack-token]"},
		// Stripe live keys (publishable + secret)
		{"stripe-secret", "STRIPE=" + stripeSk + "abcdefghijklmnopqrstuvwx0123456789", "[REDACTED:stripe-key]"},
		{"stripe-publishable", "STRIPE=" + stripePk + "abcdefghijklmnopqrstuvwx0123456789", "[REDACTED:stripe-key]"},
		// GCP service-account private-key marker. Real keys span multiple
		// lines; the redactor only needs to scrub the BEGIN marker — its
		// presence is itself a leak signal that warrants alerting.
		{"gcp-private-key", "key=-----BEGIN PRIVATE KEY-----\\nMIIEvQI…", "[REDACTED:gcp-private-key]"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := r.Redact(tc.in)
			if !strings.Contains(got, tc.want) {
				t.Errorf("Redact(%q) = %q; want to contain %q", tc.in, got, tc.want)
			}
		})
	}
}

func TestRedact_LeavesCleanStringAlone(t *testing.T) {
	r := NewSecretRedactor()
	if got := r.Redact("hello world"); got != "hello world" {
		t.Errorf("clean string mutated: %q", got)
	}
}

type fixtureNested struct {
	Inner *fixtureNested
	Tag   string
}

type fixtureFlat struct {
	APIKey   string
	Greeting string
	When     time.Time
	Count    int
	Tags     []string
	Headers  map[string]string
	Nested   *fixtureNested
}

func TestRedactStruct_NestedStructs(t *testing.T) {
	r := NewSecretRedactor()
	in := &fixtureFlat{
		APIKey: "sk-ant-abcdef0123456789abcdef",
		Nested: &fixtureNested{
			Tag:   "bearer abc.def.ghi==",
			Inner: &fixtureNested{Tag: "ok"},
		},
	}
	out, ok := r.RedactStruct(in).(*fixtureFlat)
	if !ok {
		t.Fatalf("RedactStruct returned wrong type %T", r.RedactStruct(in))
	}
	if !strings.Contains(out.APIKey, "[REDACTED:") {
		t.Errorf("APIKey not redacted: %q", out.APIKey)
	}
	if !strings.Contains(out.Nested.Tag, "[REDACTED:bearer-token]") {
		t.Errorf("Nested.Tag not redacted: %q", out.Nested.Tag)
	}
	if out.Nested.Inner.Tag != "ok" {
		t.Errorf("Inner.Tag mutated: %q", out.Nested.Inner.Tag)
	}
}

func TestRedactStruct_SkipsNonStrings(t *testing.T) {
	r := NewSecretRedactor()
	now := time.Date(2026, 4, 29, 0, 0, 0, 0, time.UTC)
	in := &fixtureFlat{When: now, Count: 42, Greeting: "hello"}
	out := r.RedactStruct(in).(*fixtureFlat)
	if !out.When.Equal(now) {
		t.Errorf("time.Time mutated: %v", out.When)
	}
	if out.Count != 42 {
		t.Errorf("int mutated: %d", out.Count)
	}
	if out.Greeting != "hello" {
		t.Errorf("clean greeting mutated: %q", out.Greeting)
	}
}

func TestRedactStruct_MapValues(t *testing.T) {
	r := NewSecretRedactor()
	in := &fixtureFlat{Headers: map[string]string{
		"Authorization": "Bearer xyz123==",
		"X-Trace-Id":    "abc-123",
	}}
	out := r.RedactStruct(in).(*fixtureFlat)
	if !strings.Contains(out.Headers["Authorization"], "[REDACTED:bearer-token]") {
		t.Errorf("map value not redacted: %q", out.Headers["Authorization"])
	}
	if _, ok := out.Headers["Authorization"]; !ok {
		t.Errorf("map key dropped")
	}
	if out.Headers["X-Trace-Id"] != "abc-123" {
		t.Errorf("clean value mutated: %q", out.Headers["X-Trace-Id"])
	}
}

func TestRedactStruct_SliceValues(t *testing.T) {
	r := NewSecretRedactor()
	in := &fixtureFlat{Tags: []string{"clean", "bearer abc=="}}
	out := r.RedactStruct(in).(*fixtureFlat)
	if out.Tags[0] != "clean" {
		t.Errorf("clean slice element mutated: %q", out.Tags[0])
	}
	if !strings.Contains(out.Tags[1], "[REDACTED:bearer-token]") {
		t.Errorf("slice element not redacted: %q", out.Tags[1])
	}
}

type cyclic struct {
	Self *cyclic
	Tag  string
}

func TestRedactStruct_CyclicInputSafe(t *testing.T) {
	r := NewSecretRedactor()
	c := &cyclic{Tag: "Bearer aaa.bbb=="}
	c.Self = c
	// Must not panic / overflow.
	out := r.RedactStruct(c)
	if out == nil {
		t.Fatalf("RedactStruct returned nil on cyclic input")
	}
	// Tag at the top should still be redacted.
	cc, ok := out.(*cyclic)
	if !ok {
		t.Fatalf("unexpected output type %T", out)
	}
	if !strings.Contains(cc.Tag, "[REDACTED:") {
		t.Errorf("top-level Tag not redacted: %q", cc.Tag)
	}
}

type linkedNode struct {
	Next *linkedNode
	Tag  string
}

func TestRedactStruct_DeepNestingBounded(t *testing.T) {
	// Build a 64-deep linked list, each carrying a redactable tag. The
	// reflective walk increments depth at every struct/pointer/slice hop,
	// so a 64-node chain comfortably exceeds reflectionDepthCap=32 and
	// guarantees the cap fires somewhere in the middle of the chain.
	const depth = 64
	nodes := make([]*linkedNode, depth)
	for i := range nodes {
		nodes[i] = &linkedNode{Tag: "Bearer xyz=="}
	}
	for i := 0; i < depth-1; i++ {
		nodes[i].Next = nodes[i+1]
	}
	r := NewSecretRedactor()
	out := r.RedactStruct(nodes[0])
	if out == nil {
		t.Fatalf("nil output")
	}
	head, ok := out.(*linkedNode)
	if !ok {
		t.Fatalf("unexpected output type %T", out)
	}
	// PR #233 deep-review H-2: when the depth cap fires on a non-string
	// field (here a `*linkedNode`), the marker is not assignable to the
	// destination type. The previous contract was "fall back to the
	// original value" — which silently leaked any secrets living past
	// the cap. The new contract is "leave the destination at its zero
	// value" so the cap actually contains the leak.
	//
	// Observable signature: the chain still has at least one redacted
	// tag near the head (recursion did run), but at some point the
	// `Next` pointer is nil (the cap zeroed it), terminating the chain
	// before all 64 nodes are visited.
	var redactedCount, plainCount, visitedNodes int
	for n := head; n != nil; n = n.Next {
		visitedNodes++
		switch {
		case strings.Contains(n.Tag, "[REDACTED:"):
			redactedCount++
		case n.Tag == "Bearer xyz==":
			plainCount++
		}
	}
	if redactedCount == 0 {
		t.Errorf("walk never redacted any tag (recursion did not run)")
	}
	if plainCount != 0 {
		t.Errorf("walk left %d unredacted tags reachable from head; depth-cap leak fix regressed (visited=%d/%d)", plainCount, visitedNodes, depth)
	}
	if visitedNodes >= depth {
		t.Errorf("walk reached all %d nodes; depth cap did not fire (visited=%d)", depth, visitedNodes)
	}
}

// TestRedactStruct_PointerCycleBounded pins PR #233 review NTH-6: when
// the per-call visited-pointer set re-enters a known address, the walk
// must terminate with the depth-exceeded marker rather than recursing
// forever. We use a self-referential struct (different from the simpler
// TestRedactStruct_CyclicInputSafe above) and assert termination plus
// top-level redaction \u2014 the pointer-cap path returns the marker as a
// bare string when the pointee type is non-string, which the parent
// struct walk discards due to assignability (same fallback as the depth
// cap). Observable contract: function returns, top-level fields are
// redacted, no panic.
func TestRedactStruct_PointerCycleBounded(t *testing.T) {
	r := NewSecretRedactor()
	c := &cyclic{Tag: "sk-ant-abcdef0123456789abcdef0123456789"}
	c.Self = c
	done := make(chan any, 1)
	go func() { done <- r.RedactStruct(c) }()
	select {
	case out := <-done:
		cc, ok := out.(*cyclic)
		if !ok {
			t.Fatalf("unexpected output type %T", out)
		}
		if !strings.Contains(cc.Tag, "[REDACTED:anthropic-api-key]") {
			t.Errorf("top-level Tag not redacted on cycle: %q", cc.Tag)
		}
	case <-time.After(2 * time.Second):
		t.Fatalf("RedactStruct did not terminate on pointer cycle")
	}
}

func TestRedactStruct_NilInput(t *testing.T) {
	r := NewSecretRedactor()
	if got := r.RedactStruct(nil); got != nil {
		t.Errorf("RedactStruct(nil) = %v; want nil", got)
	}
}

// TestRedactStruct_PointerCycle_NonStringPointee_ZerosField pins
// PR #233 review Should-Fix #5: when the per-call visited-pointer set
// detects a cycle on a non-string pointee, the walk emits the sentinel
// up the stack. The sentinel must NOT be assigned into the destination
// pointer field (it has the wrong type — that's the whole point of
// the cycle-cap path); it must zero the field instead so the cycle is
// terminated and the original (potentially secret-bearing) subtree is
// not silently re-copied. Distinct from
// [TestRedactStruct_CyclicInputSafe] / [TestRedactStruct_PointerCycleBounded]
// which only assert termination + top-level redaction, not the field-
// zeroing contract.
func TestRedactStruct_PointerCycle_NonStringPointee_ZerosField(t *testing.T) {
	r := NewSecretRedactor()
	c := &cyclic{Tag: "Bearer cycle.secret=="}
	c.Self = c
	out, ok := r.RedactStruct(c).(*cyclic)
	if !ok {
		t.Fatalf("RedactStruct returned wrong type %T", r.RedactStruct(c))
	}
	if !strings.Contains(out.Tag, "[REDACTED:bearer-token]") {
		t.Errorf("Tag not redacted on cycle: %q", out.Tag)
	}
	// PR #233 review Should-Fix #5: the cycle-cap returned the sentinel
	// (a typed marker, not assignable to *cyclic), so the parent struct
	// walk's switch must have fallen into the isDepthMarker arm and
	// left the field at its zero value (nil pointer). A regression that
	// drops the sentinel back to the original *cyclic value would
	// silently reintroduce the cycle and leak the unredacted subtree.
	if out.Self != nil {
		t.Errorf("Self pointer was not zeroed on cycle detection; got %p (cycle re-formed; H-2 leak fix regressed)", out.Self)
	}
}

// TestIsDepthMarker_RejectsCallerDataEqualToLiteral pins PR #233 review
// Nice-to-have #3: the sentinel uses a typed string ([depthMarker]) so
// isDepthMarker keys on the reflect type rather than string content.
// Caller data that happens to equal [redactDepthExceededMarker] byte-
// for-byte must NOT false-match, otherwise an attacker who can plant
// the literal string in a Detail field could trigger the depth-cap
// zero-out path on benign sibling fields.
func TestIsDepthMarker_RejectsCallerDataEqualToLiteral(t *testing.T) {
	plain := reflect.ValueOf(redactDepthExceededMarker) // bare string, not the sentinel type
	if isDepthMarker(plain) {
		t.Fatalf("isDepthMarker(plain string) = true; sentinel must key on type, not content")
	}
	sentinel := reflect.ValueOf(depthMarker(redactDepthExceededMarker))
	if !isDepthMarker(sentinel) {
		t.Fatalf("isDepthMarker(typed sentinel) = false; want true")
	}
}

// TestRedactStruct_CallerDataEqualToMarker_NotZeroed pins the same
// false-positive resistance from the user-facing surface: a caller
// passing a struct whose string field's CONTENT equals the literal
// marker must observe that field unchanged in the redacted copy
// (modulo regex redaction, which the marker doesn't match). A pre-
// fix implementation that string-compared in isDepthMarker would have
// zeroed sibling fields when the marker arm fired; the typed sentinel
// prevents this.
func TestRedactStruct_CallerDataEqualToMarker_NotZeroed(t *testing.T) {
	r := NewSecretRedactor()
	type carrier struct {
		Tag    string
		Marker string
	}
	in := carrier{Tag: "Bearer leaky==", Marker: redactDepthExceededMarker}
	out := r.RedactStruct(in).(carrier)
	if !strings.Contains(out.Tag, "[REDACTED:bearer-token]") {
		t.Errorf("Tag not redacted: %q", out.Tag)
	}
	if out.Marker != redactDepthExceededMarker {
		t.Errorf("Marker field mutated: %q; caller-supplied marker-literal must survive verbatim", out.Marker)
	}
}

func TestRedactStruct_MapAnyInDetail(t *testing.T) {
	// AuditEvent.Detail is map[string]any — the audit logger calls
	// RedactStruct on it. Confirm nested any-typed values are walked.
	r := NewSecretRedactor()
	in := map[string]any{
		"args": "Bearer abc.def==",
		"meta": map[string]any{
			"trace": "ok",
			"key":   "sk-ant-abcdef0123456789abcdef",
		},
	}
	out, ok := r.RedactStruct(in).(map[string]any)
	if !ok {
		t.Fatalf("wrong output type %T", r.RedactStruct(in))
	}
	if !strings.Contains(out["args"].(string), "[REDACTED:bearer-token]") {
		t.Errorf("top-level args not redacted: %v", out["args"])
	}
	meta := out["meta"].(map[string]any)
	if !strings.Contains(meta["key"].(string), "[REDACTED:") {
		t.Errorf("nested key not redacted: %v", meta["key"])
	}
}

func TestAddPattern_RejectsBadRegex(t *testing.T) {
	r := NewSecretRedactor()
	if err := r.AddPattern("bad", "(unterminated"); err == nil {
		t.Fatalf("expected error for malformed regex")
	}
}

// TestRedact_GenericSecretInJSON pins PR #233 review MF-2: the prior
// `\S+` value class was greedy and unbounded, so on a JSON payload the
// generic-secret match swallowed the closing quote and the next field,
// corrupting log parsers and risking obscuring an adjacent secret-shaped
// value. The bounded `[^\s,"'}\]]+` value class keeps the match scoped to
// the secret's own value and leaves neighbouring fields intact.
func TestRedact_GenericSecretInJSON(t *testing.T) {
	r := NewSecretRedactor()
	in := `{"password":"hunter2","next":"keep-me"}`
	got := r.Redact(in)
	if !strings.Contains(got, "[REDACTED:generic-secret]") {
		t.Fatalf("password value not redacted: %q", got)
	}
	if !strings.Contains(got, `"next":"keep-me"`) {
		t.Errorf("adjacent field corrupted by greedy match: %q", got)
	}
}

// TestRedact_GenericSecretConsumesTrailingQuote pins PR #233 review
// Nice-to-have #7: the prior bounded value class excluded `"` so a JSON
// payload's closing quote was left as a stray `"` after the marker
// (`{[REDACTED:generic-secret]","next":…}`). The pattern now optionally
// consumes the trailing quote so the redacted output is well-formed:
// `{[REDACTED:generic-secret],"next":…}`.
func TestRedact_GenericSecretConsumesTrailingQuote(t *testing.T) {
	r := NewSecretRedactor()
	in := `{"password":"hunter2","next":"keep-me"}`
	got := r.Redact(in)
	if strings.Contains(got, `]","`) {
		t.Errorf("stray trailing quote left after marker: %q", got)
	}
	if !strings.Contains(got, `[REDACTED:generic-secret],"next"`) {
		t.Errorf("expected redacted form `[REDACTED:generic-secret],\"next\"…`; got %q", got)
	}
	if !strings.Contains(got, `"next":"keep-me"`) {
		t.Errorf("adjacent field corrupted: %q", got)
	}
}

// TestRedact_PatternOrdering pins the documented order-dependency between
// `anthropic-api-key` and `openai-api-key`: the more specific Anthropic
// prefix must win on `sk-ant-…` strings, even though the OpenAI pattern
// would also technically match the suffix shape. (PR #233 review noted
// the ordering was relied on but not asserted.)
func TestRedact_PatternOrdering(t *testing.T) {
	r := NewSecretRedactor()
	got := r.Redact("k=sk-ant-abcdef0123456789abcdef0123456789")
	if !strings.Contains(got, "[REDACTED:anthropic-api-key]") {
		t.Errorf("Anthropic pattern did not win on sk-ant- prefix: %q", got)
	}
	if strings.Contains(got, "[REDACTED:openai-api-key]") {
		t.Errorf("OpenAI pattern incorrectly fired on Anthropic key: %q", got)
	}
}

// TestRedact_GenericSecretStopsAtURLAndShellDelimiters pins PR #233
// deep-review L-1: the bounded value class must also stop at `;` and
// `&` so URL-encoded forms (`password=hunter2&next=foo`) and shell-style
// key-value pairs (`password=hunter2; next=foo`) do not over-redact the
// adjacent field. The previous class `[^\s,"'}\]\[]+` only excluded JSON
// delimiters, so non-JSON payloads carrying secrets adjacent to other
// data would have the next field swallowed by the match — the same class
// of leak/corruption that motivated MF-2 for JSON, applied to non-JSON
// transports (HTTP query strings, CLI logs, env-style records).
func TestRedact_GenericSecretStopsAtURLAndShellDelimiters(t *testing.T) {
	r := NewSecretRedactor()
	cases := []struct {
		name string
		in   string
		// keep is a substring that must remain intact (the adjacent field
		// the previous greedy class would have swallowed).
		keep string
	}{
		{name: "url-form", in: "password=hunter2&next=foo", keep: "next=foo"},
		{name: "shell-style", in: "password=hunter2; next=foo", keep: "next=foo"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := r.Redact(tc.in)
			if !strings.Contains(got, "[REDACTED:generic-secret]") {
				t.Fatalf("password value not redacted: %q", got)
			}
			if !strings.Contains(got, tc.keep) {
				t.Errorf("adjacent field corrupted by greedy match: got %q, expected to contain %q", got, tc.keep)
			}
		})
	}
}
