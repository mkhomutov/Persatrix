package security

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestEveryAuditEventType_HasSeverityClassification ensures the closed-set
// classifier covers every constant in [AllAuditEventTypes]. Adding a new
// constant without classifying it (security or telemetry) fails CI here.
func TestEveryAuditEventType_HasSeverityClassification(t *testing.T) {
	for _, et := range AllAuditEventTypes() {
		_, sec := securityEvents[et]
		_, tel := telemetryEvents[et]
		if sec == tel {
			// Both true (impossible — disjoint maps) or both false (gap).
			t.Errorf("AuditEventType %q must be classified in exactly one of {securityEvents, telemetryEvents} (security=%v, telemetry=%v)", et, sec, tel)
		}
	}
}

func TestCorrelationID_OmittedInteractionSegmentIsEmpty(t *testing.T) {
	got := CorrelationID("run-1", "step-2", "agent-a", "")
	if got != "run-1:step-2:agent-a:" {
		t.Fatalf("CorrelationID without interaction = %q; want trailing colon preserved", got)
	}
	got = CorrelationID("run-1", "step-2", "agent-a", "int-9")
	if got != "run-1:step-2:agent-a:int-9" {
		t.Fatalf("CorrelationID with interaction = %q", got)
	}
}

func newTestLogger(t *testing.T, opts ...AuditLoggerOption) (AuditLogger, string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "audit.jsonl")
	l, err := NewFileAuditLogger(path, opts...)
	if err != nil {
		t.Fatalf("NewFileAuditLogger: %v", err)
	}
	t.Cleanup(func() { _ = l.Close() })
	return l, path
}

func readEvents(t *testing.T, path string) []AuditEvent {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read audit log: %v", err)
	}
	var events []AuditEvent
	for _, line := range strings.Split(strings.TrimRight(string(b), "\n"), "\n") {
		if line == "" {
			continue
		}
		var ev AuditEvent
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			// Pre-existing malformed lines (e.g. seeded truncated tail) are
			// expected in recovery tests — skip rather than fail.
			continue
		}
		events = append(events, ev)
	}
	return events
}

func TestStartup_BootstrapsOnMissingFile(t *testing.T) {
	_, path := newTestLogger(t)
	events := readEvents(t, path)
	if len(events) != 1 || events[0].EventType != AuditChainBootstrap {
		t.Fatalf("first event = %+v; want chain.bootstrap", events)
	}
	// Bootstrap is security-class — must be fsync'd before NewFileAuditLogger returns.
	if events[0].Checksum == "" {
		t.Fatalf("bootstrap checksum should be set")
	}
}

func TestStartup_RecoversFromTruncatedTail(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "audit.jsonl")
	// Pre-write a complete event followed by a truncated mid-JSON line.
	if err := os.WriteFile(path, []byte(`{"event_type":"agent.registered","checksum":"deadbeef"}`+"\n"+`{"event_type":"tool.invoked","check`), 0o600); err != nil {
		t.Fatalf("seed file: %v", err)
	}
	l, err := NewFileAuditLogger(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = l.Close() })
	events := readEvents(t, path)
	// Last event should be chain.recovered with prior_tail = "unknown".
	last := events[len(events)-1]
	if last.EventType != AuditChainRecovered {
		t.Fatalf("last event type = %s; want chain.recovered", last.EventType)
	}
	if got := last.Detail["prior_tail"]; got != "unknown" {
		t.Fatalf("prior_tail = %v; want \"unknown\"", got)
	}
}

func TestProcessRestart_EmitsChainRestartEvent(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "audit.jsonl")

	l1, err := NewFileAuditLogger(path)
	if err != nil {
		t.Fatalf("open #1: %v", err)
	}
	if err := l1.Emit(context.Background(), AuditEvent{EventType: AuditAgentRegistered, AgentID: "a"}); err != nil {
		t.Fatalf("emit: %v", err)
	}
	if err := l1.Close(); err != nil {
		t.Fatalf("close #1: %v", err)
	}

	l2, err := NewFileAuditLogger(path)
	if err != nil {
		t.Fatalf("open #2: %v", err)
	}
	t.Cleanup(func() { _ = l2.Close() })
	events := readEvents(t, path)
	// Sequence: bootstrap, agent.registered, chain.restart.
	if len(events) < 3 {
		t.Fatalf("got %d events; want >= 3", len(events))
	}
	last := events[len(events)-1]
	if last.EventType != AuditChainRestart {
		t.Fatalf("last event = %s; want chain.restart", last.EventType)
	}
	if last.Detail["prior_tail_checksum"] != events[len(events)-2].Checksum {
		t.Fatalf("chain.restart prior_tail_checksum %v != prior tail checksum %s", last.Detail["prior_tail_checksum"], events[len(events)-2].Checksum)
	}
}

func TestChecksumChain_DetectsTampering(t *testing.T) {
	l, path := newTestLogger(t)
	for i := 0; i < 5; i++ {
		if err := l.Emit(context.Background(), AuditEvent{EventType: AuditAgentRegistered, AgentID: "a", Action: "register"}); err != nil {
			t.Fatalf("emit %d: %v", i, err)
		}
	}
	if err := l.Flush(); err != nil {
		t.Fatalf("flush: %v", err)
	}
	events := readEvents(t, path)
	// events[0] is chain.bootstrap. Recompute the chain from there and
	// confirm every checksum matches what the logger wrote.
	prev := emptyChecksum()
	for i, ev := range events {
		canon, err := canonicalEventJSON(ev, prev)
		if err != nil {
			t.Fatalf("canonical %d: %v", i, err)
		}
		recomputed := hashHex(canon)
		if recomputed != ev.Checksum {
			t.Fatalf("event %d checksum %s != recomputed %s", i, ev.Checksum, recomputed)
		}
		prev = ev.Checksum
	}

	// Mutate the third event in memory; chain breaks at event 4.
	mutated := events
	mutated[2].Action = "tampered"
	prev = emptyChecksum()
	tamperDetected := false
	for i, ev := range mutated {
		canon, _ := canonicalEventJSON(ev, prev)
		recomputed := hashHex(canon)
		if recomputed != ev.Checksum {
			tamperDetected = true
			if i < 2 {
				t.Fatalf("chain broke too early at event %d", i)
			}
			break
		}
		prev = ev.Checksum
	}
	if !tamperDetected {
		t.Fatalf("tamper not detected by checksum chain")
	}
}

func hashHex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func TestFsync_SecurityEventsFlushImmediately(t *testing.T) {
	l, path := newTestLogger(t, WithBatchSize(1000), WithBatchInterval(0))
	// First, bootstrap was written and fsync'd. Capture baseline.
	baseline := fileSize(t, path)

	// Emit a security-class event — must grow file before Emit returns.
	if err := l.Emit(context.Background(), AuditEvent{EventType: AuditCapabilityViolation, AgentID: "a"}); err != nil {
		t.Fatalf("emit: %v", err)
	}
	if fileSize(t, path) == baseline {
		t.Fatalf("security event did not flush before Emit returned")
	}

	beforeTel := fileSize(t, path)
	if err := l.Emit(context.Background(), AuditEvent{EventType: AuditToolInvoked, AgentID: "a"}); err != nil {
		t.Fatalf("emit telemetry: %v", err)
	}
	if fileSize(t, path) != beforeTel {
		t.Fatalf("telemetry event flushed before batch threshold (size %d -> %d)", beforeTel, fileSize(t, path))
	}
}

func TestBatchFlush_CountTrigger(t *testing.T) {
	l, path := newTestLogger(t, WithBatchSize(4), WithBatchInterval(0))
	before := fileSize(t, path)
	for i := 0; i < 3; i++ {
		_ = l.Emit(context.Background(), AuditEvent{EventType: AuditToolInvoked, AgentID: "a"})
	}
	if fileSize(t, path) != before {
		t.Fatalf("flushed before count threshold")
	}
	_ = l.Emit(context.Background(), AuditEvent{EventType: AuditToolInvoked, AgentID: "a"})
	if fileSize(t, path) == before {
		t.Fatalf("batch did not flush at count threshold")
	}
}

func TestBatchFlush_TimerTrigger(t *testing.T) {
	l, path := newTestLogger(t, WithBatchSize(1000), WithBatchInterval(50*time.Millisecond))
	before := fileSize(t, path)
	_ = l.Emit(context.Background(), AuditEvent{EventType: AuditToolInvoked, AgentID: "a"})
	if fileSize(t, path) != before {
		t.Fatalf("flushed before ticker fired")
	}
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if fileSize(t, path) > before {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("ticker did not flush within 1s")
}

func fileSize(t *testing.T, path string) int64 {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	return info.Size()
}

func TestEmit_RejectsAfterClose(t *testing.T) {
	l, _ := newTestLogger(t)
	if err := l.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if err := l.Emit(context.Background(), AuditEvent{EventType: AuditAgentRegistered}); err == nil {
		t.Fatalf("expected error emitting after Close")
	}
}

func TestEmit_RedactsDetail(t *testing.T) {
	red := NewSecretRedactor()
	l, path := newTestLogger(t, WithRedactor(red))
	if err := l.Emit(context.Background(), AuditEvent{
		EventType: AuditCapabilityViolation,
		AgentID:   "a",
		Detail:    map[string]any{"args": "Authorization: Bearer abc.def.ghi=="},
	}); err != nil {
		t.Fatalf("emit: %v", err)
	}
	events := readEvents(t, path)
	last := events[len(events)-1]
	if !strings.Contains(last.Detail["args"].(string), "[REDACTED:bearer-token]") {
		t.Fatalf("redactor did not scrub bearer token; got %v", last.Detail["args"])
	}
}
