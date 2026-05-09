package security

import (
	"testing"
	"time"
)

// BenchmarkRedactStruct_RepresentativeDetail measures [SecretRedactor.RedactStruct]
// against a payload shape representative of a real `tool.invoked` audit
// event Detail: 10–20 fields, 2–3 levels of nesting, a mix of clean
// strings, secret-shaped strings, and non-string types (PR #233 review
// Nice-to-have #8). The numbers inform a v0.4.0 sync.Pool decision —
// if the per-event allocation cost dominates the audit hot path under
// realistic load, pooling the reflect.Value scratch space and the
// visited-set map becomes worth the API churn.
func BenchmarkRedactStruct_RepresentativeDetail(b *testing.B) {
	r := NewSecretRedactor()
	payload := buildBenchmarkPayload()
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = r.RedactStruct(payload)
	}
}

// BenchmarkRedactStruct_MapAnyDetail mirrors AuditEvent.Detail's actual
// shape (`map[string]any`) so a future Pool-or-not decision compares
// against the type the audit logger really hands in.
func BenchmarkRedactStruct_MapAnyDetail(b *testing.B) {
	r := NewSecretRedactor()
	detail := map[string]any{
		"agent_id":    "agent-1234",
		"tool":        "http_request",
		"args":        "Authorization: Bearer abc.def.ghi==",
		"url":         "https://api.example.com/?token=sk-ant-abcdef0123456789abcdef",
		"trace_id":    "0123456789abcdef0123456789abcdef",
		"latency_ms":  123,
		"retry_count": 0,
		"meta": map[string]any{
			"correlation_id": "run-1:step-2:agent-3:int-4",
			"ts":             time.Date(2026, 5, 9, 12, 0, 0, 0, time.UTC),
			"flags":          []string{"clean", "ok"},
		},
	}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = r.RedactStruct(detail)
	}
}

type benchInner struct {
	Tag       string
	Headers   map[string]string
	Tags      []string
	CreatedAt time.Time
}

type benchPayload struct {
	AgentID   string
	Action    string
	Resource  string
	APIKey    string
	Bearer    string
	Greeting  string
	When      time.Time
	Count     int
	LatencyMs float64
	Tags      []string
	Headers   map[string]string
	Inner     *benchInner
	NestedAny map[string]any
}

func buildBenchmarkPayload() *benchPayload {
	return &benchPayload{
		AgentID:   "agent-1234",
		Action:    "http_request",
		Resource:  "https://api.example.com/v1/resource",
		APIKey:    "sk-ant-abcdef0123456789abcdef0123456789",
		Bearer:    "Authorization: Bearer abc.def.ghi==",
		Greeting:  "hello world",
		When:      time.Date(2026, 5, 9, 12, 0, 0, 0, time.UTC),
		Count:     42,
		LatencyMs: 128.5,
		Tags:      []string{"clean", "Bearer xyz==", "neutral", "AKIAABCDEFGHIJKLMNOP"},
		Headers: map[string]string{
			"Authorization": "Bearer aaa.bbb.ccc==",
			"X-Trace-Id":    "abc-123",
			"User-Agent":    "persatrix/0.3.0",
		},
		Inner: &benchInner{
			Tag:       "password=hunter2",
			Headers:   map[string]string{"Set-Cookie": "session=secret-value"},
			Tags:      []string{"a", "b", "c"},
			CreatedAt: time.Date(2026, 5, 9, 12, 0, 0, 0, time.UTC),
		},
		NestedAny: map[string]any{
			"trace": "ok",
			"key":   "sk-ant-abcdef0123456789abcdef",
			"deep": map[string]any{
				// Split prefix to avoid GitHub push-time secret scanner false positive.
				"slack": "xo" + "xb-1234567890-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx",
				"nums":  []int{1, 2, 3},
			},
		},
	}
}
