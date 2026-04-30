package security

import (
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
	// PR #233 review: lock the cap-32 contract by asserting positive
	// behaviour at *both* ends of the chain. We deliberately avoid pinning
	// the exact boundary node \u2014 that depends on the implementation detail
	// of how `depth` is incremented per kind of hop \u2014 and instead require
	// only that:
	//   (a) the recursion ran far enough to redact at least one tag, AND
	//   (b) the cap actually fired before the chain was fully walked,
	//       leaving at least one deep tag in its original (unredacted)
	//       form.
	//
	// Note: when the depth cap fires, the marker (a `string`) is not
	// assignable to a `*linkedNode` field, so the walk falls back to the
	// original pointer (see the `reflect.Struct` branch in walk()). The
	// observable signature of the cap is therefore an unredacted Tag deep
	// in the chain, not the marker string itself.
	var redactedCount, plainCount int
	for n := head; n != nil; n = n.Next {
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
	if plainCount == 0 {
		t.Errorf("walk redacted every tag (depth cap did not fire on a %d-deep chain)", depth)
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
