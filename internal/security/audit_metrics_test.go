package security

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// fakeAuditMetrics is the in-process recorder used to assert PR 1c's
// metric surface fires from the right call sites without standing up
// an OTLP endpoint. Safe for serial (single-test) use only.
type fakeAuditMetrics struct {
	events    []recordedEvent
	recovered int
	latencies []time.Duration
}

type recordedEvent struct {
	EventType AuditEventType
	Class     string
}

func (f *fakeAuditMetrics) RecordEvent(t AuditEventType, class string) {
	f.events = append(f.events, recordedEvent{t, class})
}

func (f *fakeAuditMetrics) RecordChainRecovered() {
	f.recovered++
}

func (f *fakeAuditMetrics) ObserveEmitLatency(d time.Duration) {
	f.latencies = append(f.latencies, d)
}

// TestAuditMetrics_RecordsEventAndLatencyOnEmit pins that every Emit
// records both the per-event counter and the latency histogram. The
// counter's class label is derived from [classifyAuditEvent] so the
// "security" / "telemetry" partition the docs/observability §13 SLO
// alert depends on stays observable.
func TestAuditMetrics_RecordsEventAndLatencyOnEmit(t *testing.T) {
	fake := &fakeAuditMetrics{}
	l, _ := newTestLogger(t, WithAuditMetrics(fake))
	if err := l.Emit(context.Background(), AuditEvent{EventType: AuditAgentRegistered, AgentID: "a"}); err != nil {
		t.Fatalf("emit registered: %v", err)
	}
	if err := l.Emit(context.Background(), AuditEvent{EventType: AuditCapabilityViolation, AgentID: "a"}); err != nil {
		t.Fatalf("emit violation: %v", err)
	}

	// Three events recorded: bootstrap (security), agent.registered
	// (telemetry), capability.violation (security).
	if len(fake.events) != 3 {
		t.Fatalf("events recorded = %d; want 3 (got %+v)", len(fake.events), fake.events)
	}
	wantClass := []string{"security", "telemetry", "security"}
	for i, want := range wantClass {
		if fake.events[i].Class != want {
			t.Errorf("event[%d] class = %q; want %q (event_type=%s)", i, fake.events[i].Class, want, fake.events[i].EventType)
		}
	}
	if got := len(fake.latencies); got != 3 {
		t.Errorf("latency observations = %d; want 3", got)
	}
}

// TestAuditMetrics_BumpsChainRecoveredOnTruncatedTail seeds a malformed
// tail line, opens the logger, and confirms the dedicated chain-recovery
// counter increments alongside the synthetic chain.recovered event. The
// two counters are not redundant: events_total drives per-type SLOs,
// chain_recovered_total drives integrity alerting and is the right
// surface for "page on any non-zero increment over 5 min" rules.
func TestAuditMetrics_BumpsChainRecoveredOnTruncatedTail(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "audit.jsonl")
	// Seed a truncated tail (no trailing newline, mid-JSON).
	if err := os.WriteFile(path, []byte(`{"event_type":"tool.invoked","ag`), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}

	fake := &fakeAuditMetrics{}
	l, err := NewFileAuditLogger(path, WithAuditMetrics(fake))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = l.Close() })

	if fake.recovered != 1 {
		t.Errorf("chain_recovered_total bumps = %d; want 1", fake.recovered)
	}
	// And the chain.recovered event itself flows through events_total.
	var sawRecovered bool
	for _, e := range fake.events {
		if e.EventType == AuditChainRecovered {
			sawRecovered = true
			if e.Class != "security" {
				t.Errorf("chain.recovered class = %q; want \"security\"", e.Class)
			}
		}
	}
	if !sawRecovered {
		t.Errorf("events_total never recorded chain.recovered (got %+v)", fake.events)
	}
}
