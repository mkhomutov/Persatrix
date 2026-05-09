package security

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// PR #233 / PR #234 / PR #236 review follow-up tests, extracted from
// audit_test.go to keep the parent file under the 500-line cap. All
// fixtures land here so a future maintainer can read the close-out
// regressions without paging through the original Phase-1a tests.

// TestBatchFlush_TickerInjected_FlushesOnTick pins PR #233 review
// Should-Fix #1: tests must drive the batch-flush ticker deterministically
// rather than depending on wall-clock progress. The unexported
// withTickerSeam test option replaces the real time.NewTicker channel
// with a caller-controlled chan and signals each ticker-driven flush on
// the supplied done chan, making the synchronisation visible to the test
// without sleeps or polling.
func TestBatchFlush_TickerInjected_FlushesOnTick(t *testing.T) {
	tick := make(chan time.Time, 1)
	flushed := make(chan struct{}, 1)
	l, path := newTestLogger(t,
		WithBatchSize(1000),
		WithBatchInterval(time.Hour),
		withTickerSeam(tick, flushed),
	)
	before := fileSize(t, path)
	if err := l.Emit(context.Background(), AuditEvent{EventType: AuditToolInvoked, AgentID: "a"}); err != nil {
		t.Fatalf("emit: %v", err)
	}
	if fileSize(t, path) != before {
		t.Fatalf("flushed before ticker fired")
	}
	tick <- time.Now()
	select {
	case <-flushed:
	case <-time.After(time.Second):
		t.Fatalf("ticker-driven flush did not signal within 1s")
	}
	if fileSize(t, path) <= before {
		t.Fatalf("ticker did not flush after deterministic tick")
	}
}

// TestBatchFlush_TickerInjected_NoFlushWithoutPending pins the contract
// that the ticker is a no-op when pendingTel is zero — a tick that fires
// against an empty buffer must not perform an fsync. Combined with
// TestBatchFlush_TickerInjected_FlushesOnTick this proves the ticker
// loop is driven entirely by the injected channel.
func TestBatchFlush_TickerInjected_NoFlushWithoutPending(t *testing.T) {
	tick := make(chan time.Time, 1)
	flushed := make(chan struct{}, 1)
	_, path := newTestLogger(t,
		WithBatchSize(1000),
		WithBatchInterval(time.Hour),
		withTickerSeam(tick, flushed),
	)
	before := fileSize(t, path)
	tick <- time.Now()
	select {
	case <-flushed:
		t.Fatalf("flushed signal fired without pending events")
	case <-time.After(50 * time.Millisecond):
		// Expected: ticker observed empty pendingTel and skipped the flush.
	}
	if fileSize(t, path) != before {
		t.Fatalf("file grew on empty-buffer tick: %d -> %d", before, fileSize(t, path))
	}
}

// TestWithClock_DrivesEmitTimestamps pins the documented contract for
// WithClock: the option's func is the source for AuditEvent.Timestamp on
// any Emit that arrives with a zero timestamp. Coverage-gap test from
// PR #233 review (no prior test exercised the clock-injection path).
func TestWithClock_DrivesEmitTimestamps(t *testing.T) {
	pinned := time.Date(2030, 1, 2, 3, 4, 5, 0, time.UTC)
	l, path := newTestLogger(t, WithClock(func() time.Time { return pinned }))
	if err := l.Emit(context.Background(), AuditEvent{EventType: AuditAgentRegistered, AgentID: "a"}); err != nil {
		t.Fatalf("emit: %v", err)
	}
	if err := l.Flush(); err != nil {
		t.Fatalf("flush: %v", err)
	}
	events := readEvents(t, path)
	last := events[len(events)-1]
	if !last.Timestamp.Equal(pinned) {
		t.Fatalf("Emit timestamp = %v; want pinned %v", last.Timestamp, pinned)
	}
}

// TestEmit_DoesNotMutateCallerDetail pins coverage-gap from PR #233
// review: Emit applies redaction inline, but the fix-up must not leak
// back into the caller's Detail map. A caller that re-uses the same
// fixture across multiple Emits must observe its original payload after
// each call.
func TestEmit_DoesNotMutateCallerDetail(t *testing.T) {
	l, _ := newTestLogger(t)
	original := map[string]any{"args": "Authorization: Bearer leaky.token=="}
	if err := l.Emit(context.Background(), AuditEvent{
		EventType: AuditCapabilityViolation,
		AgentID:   "a",
		Detail:    original,
	}); err != nil {
		t.Fatalf("emit: %v", err)
	}
	if got := original["args"]; got != "Authorization: Bearer leaky.token==" {
		t.Fatalf("Emit mutated caller Detail: args = %q", got)
	}
}

// TestVerifyChain_HappyPath pins PR #233 review Nice-to-have #1: an
// exported [VerifyChain] helper recomputes the per-event sha256 chain
// and surfaces tamper / truncation errors. External auditors and a
// future `persatrix audit verify` CLI consume this rather than
// reimplementing [canonicalEventJSON]. Happy-path: a freshly-written
// log validates clean.
func TestVerifyChain_HappyPath(t *testing.T) {
	l, path := newTestLogger(t)
	for i := 0; i < 5; i++ {
		if err := l.Emit(context.Background(), AuditEvent{EventType: AuditAgentRegistered, AgentID: "a"}); err != nil {
			t.Fatalf("emit %d: %v", i, err)
		}
	}
	if err := l.Flush(); err != nil {
		t.Fatalf("flush: %v", err)
	}
	if err := VerifyChain(path); err != nil {
		t.Fatalf("VerifyChain on clean log = %v; want nil", err)
	}
}

// TestVerifyChain_DetectsTampering pins the tamper-detection contract:
// flipping a byte in any event's Detail breaks every checksum from
// that point forward; VerifyChain must surface this, not silently
// accept the stale chain.
func TestVerifyChain_DetectsTampering(t *testing.T) {
	l, path := newTestLogger(t)
	for i := 0; i < 5; i++ {
		if err := l.Emit(context.Background(), AuditEvent{
			EventType: AuditAgentRegistered,
			AgentID:   "a",
			Detail:    map[string]any{"i": i},
		}); err != nil {
			t.Fatalf("emit %d: %v", i, err)
		}
	}
	if err := l.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	tampered := strings.Replace(string(raw), `"i":2`, `"i":99`, 1)
	if tampered == string(raw) {
		t.Fatalf("tamper substitution did not match — fixture changed")
	}
	if err := os.WriteFile(path, []byte(tampered), 0o600); err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if err := VerifyChain(path); err == nil {
		t.Fatalf("VerifyChain on tampered log = nil; want error")
	}
}

// TestVerifyChain_MissingPath returns a typed error rather than a
// silent success — operators running the CLI against a wrong path
// must see the failure surface, not a false-positive "clean" verdict.
func TestVerifyChain_MissingPath(t *testing.T) {
	if err := VerifyChain(filepath.Join(t.TempDir(), "absent.jsonl")); err == nil {
		t.Fatalf("VerifyChain on missing path = nil; want error")
	}
}

// TestLooksLikeSHA256 pins the helper's contract before its
// implementation moves from a hand-rolled hex check to
// [encoding/hex.DecodeString] (PR #233 review Should-Fix #7). Sixty-four
// hex characters are the accepted shape; anything else — non-hex bytes,
// wrong length — is rejected. hex.DecodeString accepts both cases; the
// audit chain only emits lowercase but a hand-edited file with
// uppercase digests is preferable to a spurious chain.recovered.
func TestLooksLikeSHA256(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want bool
	}{
		{name: "valid lower hex", in: strings.Repeat("0123456789abcdef", 4), want: true},
		{name: "uppercase hex accepted", in: strings.Repeat("0123456789ABCDEF", 4), want: true},
		{name: "mixed case accepted", in: strings.Repeat("0123456789aBcDeF", 4), want: true},
		{name: "too short", in: strings.Repeat("a", 63), want: false},
		{name: "too long", in: strings.Repeat("a", 65), want: false},
		{name: "non-hex byte", in: strings.Repeat("a", 63) + "g", want: false},
		{name: "empty", in: "", want: false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := looksLikeSHA256(c.in); got != c.want {
				t.Fatalf("looksLikeSHA256(%q) = %v; want %v", c.in, got, c.want)
			}
		})
	}
}

// TestStartup_NoRedactor_TruncatedTailLeaksRaw documents the contract
// when WithRedactor(nil) explicitly disables redaction: a truncated tail
// containing a secret-shaped fragment lands in Detail verbatim. PR 1b
// made the constructor default to NewSecretRedactor to prevent silent
// leakage in normal use; this test pins the explicit-opt-out behaviour
// so a future "always redact" change does not regress test fixtures
// that intentionally write plaintext (PR #233 review coverage gap).
func TestStartup_NoRedactor_TruncatedTailLeaksRaw(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "audit.jsonl")
	// prior_tail_raw_truncated carries only the bytes after the last
	// newline (see [readLastLine] in audit_chain.go), so the secret has
	// to live on the partial trailing line for this contract test.
	if err := os.WriteFile(path, []byte("{\"complete\":\"line\"}\n{partial Authorization: Bearer hunter2=="), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	l, err := NewFileAuditLogger(path, WithRedactor(nil))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = l.Close() })
	if err := l.Flush(); err != nil {
		t.Fatalf("flush: %v", err)
	}
	events := readEvents(t, path)
	if len(events) == 0 {
		t.Fatalf("no events after recovery")
	}
	rec := events[len(events)-1]
	if rec.EventType != AuditChainRecovered {
		t.Fatalf("last event = %q; want %q", rec.EventType, AuditChainRecovered)
	}
	raw, _ := rec.Detail["prior_tail_raw_truncated"].(string)
	if !strings.Contains(raw, "hunter2") {
		t.Fatalf("expected raw secret in prior_tail_raw_truncated when redactor disabled; got %q", raw)
	}
}
