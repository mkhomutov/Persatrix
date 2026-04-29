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
	// Build a 64-deep linked list, each carrying a redactable tag.
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
	// Ensure no panic and that the function returns. We don't try to
	// over-specify behaviour at the depth boundary — just that the function
	// terminates and produces *something*.
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
