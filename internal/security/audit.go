package security

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"go.uber.org/zap"
)

// Audit-sink defaults. Callers may override via [AuditLoggerOption]s.
const (
	defaultBatchSize     = 64
	defaultBatchInterval = 250 * time.Millisecond
)

// AuditLogger writes [AuditEvent]s to a durable sink with checksum-chained
// tamper evidence (RFC 0009 §G).
//
// The interface is small so future sinks (SIEM, write-once object store) can
// drop in without churning call sites. The Phase 1 implementation ships a
// JSONL file sink ([NewFileAuditLogger]).
type AuditLogger interface {
	// Emit records ev. The event's Timestamp and Checksum are filled by the
	// logger if zero / empty; callers may pre-set them to override (used by
	// the chain-recovery synthetic events).
	//
	// Security-class events are flushed (and fsync'd) before Emit returns.
	// Telemetry-class events may be batched; see [AuditLoggerOption]s for
	// batch size and interval overrides.
	Emit(ctx context.Context, ev AuditEvent) error

	// Flush forces any batched events to disk.
	Flush() error

	// Close flushes pending events and releases the sink. Safe to call once.
	Close() error

	// Path returns the on-disk location backing the logger, or the empty
	// string for non-file-backed sinks (e.g. future SIEM transports).
	// Used by orchestrator startup logs and integration tests so the
	// resolved path is observable without depending on the concrete type
	// (PR #233 review Should-Fix #4).
	Path() string
}

// AuditLoggerOption configures a file-backed AuditLogger.
type AuditLoggerOption func(*fileAuditLogger)

// WithBatchSize overrides the telemetry-class batch flush threshold.
// Values <= 0 are clamped to 1 (effectively "flush every event").
func WithBatchSize(n int) AuditLoggerOption {
	return func(l *fileAuditLogger) {
		if n <= 0 {
			n = 1
		}
		l.batchSize = n
	}
}

// WithBatchInterval overrides the telemetry-class batch flush ticker.
// Values <= 0 disable the ticker (only count-based flush remains).
func WithBatchInterval(d time.Duration) AuditLoggerOption {
	return func(l *fileAuditLogger) {
		l.batchInterval = d
	}
}

// WithRedactor overrides the default [Redactor] (set by [NewFileAuditLogger]
// to [NewSecretRedactor]) used to scrub event fields before serialisation.
// Pass nil to disable redaction entirely — required only for tests that
// intentionally write plaintext fixtures.
//
// PR #233 review Should-Fix #3: prior behaviour defaulted to nil, so any
// caller that forgot WithRedactor silently shipped unredacted secrets to the
// audit log. The constructor now installs [NewSecretRedactor] up front and
// this option only customises the choice.
func WithRedactor(r Redactor) AuditLoggerOption {
	return func(l *fileAuditLogger) {
		l.redactor = r
	}
}

// WithClock injects a clock function used for AuditEvent.Timestamp on
// Emit (when the caller leaves Timestamp zero) and for the
// emit-latency histogram measurement. Tests pass a deterministic clock;
// production defaults to [time.Now].
//
// Note: WithClock does NOT drive the batch-interval ticker — the ticker
// runs off the real Go time wheel. Tests that need deterministic
// ticker control use the unexported [withTickerSeam] seam in this
// package's *_test.go (PR #233 review Should-Fix #1: prior doc claimed
// WithClock drove the ticker, which was misleading).
func WithClock(now func() time.Time) AuditLoggerOption {
	return func(l *fileAuditLogger) {
		if now != nil {
			l.now = now
		}
	}
}

// WithAuditMetrics injects the metrics surface the logger publishes
// per-event counters and an emit-latency histogram through (RFC 0009
// PR 1c). Pass nil — or omit the option — to disable metric emission;
// the logger uses a zero-cost no-op surface in that case so existing
// callers and tests continue to work unchanged.
//
// The orchestrator wires this from the OTEL-backed implementation in
// [observability/metrics.NewAuditMetrics]; production deployments rely
// on the resulting `audit_events_total` / `audit_chain_recovered_total`
// / `audit_emit_latency_seconds` instruments to detect the
// capability-fsync amplification documented in PR #234 review Medium-1.
func WithAuditMetrics(m AuditMetrics) AuditLoggerOption {
	return func(l *fileAuditLogger) {
		if m == nil {
			l.metrics = noopAuditMetrics{}
			return
		}
		l.metrics = m
	}
}

// WithLogger injects a structured logger used for diagnostic warnings
// emitted by the audit logger itself (e.g. chmod self-heal failures).
//
// PR #234 review M-3: the chmod self-heal previously surfaced via
// `fmt.Fprintf(os.Stderr, …)` because no logger was plumbed through.
// JSON-only log collectors route raw stderr to a separate stream and
// commonly drop it, hiding a security-relevant misconfiguration. With a
// logger injected, chmod failures land in the same structured pipeline
// as the rest of orchestrator's warnings. Defaults to [zap.NewNop] so
// existing callers (and tests) continue to work unchanged.
func WithLogger(logger *zap.Logger) AuditLoggerOption {
	return func(l *fileAuditLogger) {
		if logger != nil {
			l.logger = logger
		}
	}
}

// fileAuditLogger is the JSONL append-only sink used in Phase 1.
//
// Thread-safety: all state mutation goes through mu. Flush/Close take mu;
// Emit takes mu briefly, decides per-event whether to fsync, and releases
// mu before any potentially-blocking syscall.
type fileAuditLogger struct {
	path string

	mu             sync.Mutex
	file           *os.File
	writer         *bufio.Writer
	prevChecksum   string
	pendingTel     int // telemetry-class events buffered since last flush
	closed         bool
	tickerStop     chan struct{}
	tickerStopOnce sync.Once

	batchSize     int
	batchInterval time.Duration
	redactor      Redactor
	now           func() time.Time
	logger        *zap.Logger
	metrics       AuditMetrics

	// testTickC and testFlushSignal are unexported test seams (PR #233
	// review Should-Fix #1). When testTickC is non-nil, tickerLoop uses
	// it instead of time.NewTicker(batchInterval).C — the test pushes
	// values on the channel to drive the loop deterministically.
	// testFlushSignal, when non-nil, receives a struct{} after every
	// successful ticker-driven flush so the test can synchronise without
	// polling on file size. Production callers leave these nil and the
	// loop falls back to a real time.Ticker.
	testTickC       <-chan time.Time
	testFlushSignal chan<- struct{}
}

// NewFileAuditLogger opens path for append-only writes and returns an
// [AuditLogger] backed by it.
//
// The file is created with mode 0o600 if it does not exist; the parent
// directory must already exist (callers are responsible for `mkdir -p` —
// the orchestrator wires this from `OBSERVABILITY_AUDIT_PATH`).
//
// Startup chain-recovery semantics (PR #232 review SF-3):
//
//   - File missing or zero-length → seed prevChecksum = sha256("") and emit
//     `chain.bootstrap` (security-class) as the first event.
//   - Tail line parses and its Checksum recomputes correctly → emit
//     `chain.restart` carrying that checksum.
//   - Tail line is unparseable / truncated / checksum mismatch → emit
//     `chain.recovered` with Detail.prior_tail = "unknown".
//
// Returns an error only on filesystem failures (open / stat). Recovery
// outcomes are surfaced as the first synthetic event written to the file.
func NewFileAuditLogger(path string, opts ...AuditLoggerOption) (AuditLogger, error) {
	if path == "" {
		return nil, errors.New("security: audit log path is required")
	}

	tail, prevSum, recovery, reason := inspectTail(path)

	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, fmt.Errorf("security: open audit log %q: %w", path, err)
	}

	l := &fileAuditLogger{
		path:          path,
		file:          f,
		writer:        bufio.NewWriter(f),
		prevChecksum:  prevSum,
		batchSize:     defaultBatchSize,
		batchInterval: defaultBatchInterval,
		now:           time.Now,
		redactor:      NewSecretRedactor(),
		logger:        zap.NewNop(),
		metrics:       noopAuditMetrics{},
	}
	for _, opt := range opts {
		opt(l)
	}

	// PR #233 review Should-Fix #6: a pre-existing audit file may have been
	// created with relaxed permissions before this build's umask was tightened
	// (or by an earlier process running under a different umask). Re-apply
	// 0o600 on every open so the on-disk ACL is self-healing. Errors are
	// logged-and-continue rather than fatal: chmod is unsupported on some
	// filesystems (e.g. FAT-mounted bind volumes in CI) and refusing to
	// open the audit log there would deny operators forensic data for a
	// permission concern that no longer applies.
	//
	// PR #234 review M-3: route the warning through the injected logger
	// (defaults to a nop logger so call sites without [WithLogger] keep
	// the previous "silent best-effort" behaviour rather than spraying
	// stderr in tests). Production callers (cmd/orchestrator) wire a
	// real *zap.Logger so the warning lands in the structured pipeline.
	if chErr := f.Chmod(0o600); chErr != nil && !errors.Is(chErr, os.ErrPermission) {
		l.logger.Warn("audit log chmod 0o600 failed",
			zap.String("path", path),
			zap.Error(chErr),
		)
	}

	// If recovery detected a truncated tail, the existing file may not end
	// with a newline. Insert one so the synthetic chain.recovered event lands
	// on a fresh line and the prior partial record stays parseable as garbage
	// rather than being concatenated into the new event.
	if recovery == recoveryRecovered && fileEndsWithoutNewline(path) {
		if _, err := l.writer.Write([]byte{'\n'}); err != nil {
			_ = l.Close()
			return nil, err
		}
	}

	if err := l.emitRecoveryEvent(recovery, tail, reason); err != nil {
		_ = l.Close()
		return nil, err
	}

	if l.batchInterval > 0 {
		l.tickerStop = make(chan struct{})
		go l.tickerLoop(l.batchInterval, l.tickerStop)
	}
	return l, nil
}

// Emit implements [AuditLogger].
func (l *fileAuditLogger) Emit(ctx context.Context, ev AuditEvent) error {
	if err := ctx.Err(); err != nil {
		return err
	}

	// PR 1c (PR #234 review Medium-1): time the full Emit including
	// mutex acquisition + serialise + write + (for security-class
	// events) fsync. This is the canonical surface the operator alert
	// on `audit_emit_latency_seconds` watches; the histogram makes the
	// capability-fsync amplification visible without requiring the
	// caller to instrument every emit site.
	start := l.now()
	defer func() {
		l.metrics.ObserveEmitLatency(l.now().Sub(start))
	}()

	l.mu.Lock()
	defer l.mu.Unlock()

	if l.closed {
		return errors.New("security: audit logger is closed")
	}

	if ev.Timestamp.IsZero() {
		ev.Timestamp = l.now().UTC()
	}
	if l.redactor != nil {
		// PR #233 deep-review H-1: prior implementation only scrubbed
		// ev.Detail. PR 1b will start emitting tool.invoked events whose
		// Resource is the tool's target URL and whose Action carries the
		// invoked operation — both fields most likely to embed secrets in
		// the wild (e.g. `https://api.example.com/?token=sk-ant-…`). Apply
		// pattern redaction to the string-valued caller fields as well.
		//
		// AgentID and CorrelationID are intentionally NOT redacted: they
		// are caller-controlled identifiers (not free-form strings) whose
		// stability is required to correlate events across the chain. Any
		// caller embedding a secret in an identifier is a misconfiguration
		// the audit log cannot fix without breaking forensic linkage.
		ev.Action = l.redactor.Redact(ev.Action)
		ev.Resource = l.redactor.Redact(ev.Resource)
		if ev.Detail != nil {
			if redacted, ok := l.redactor.RedactStruct(ev.Detail).(map[string]any); ok {
				ev.Detail = redacted
			} else {
				// PR #233 deep-review M-4: RedactStruct may return a
				// non-map (e.g. the depth-exceeded marker string if the
				// top-level Detail map's reflective walk hit the cap).
				// Preserving the original here would silently leak the
				// unredacted payload — drop Detail entirely and signal
				// that redaction failed so an operator can investigate.
				ev.Detail = map[string]any{"_redacted": "depth-exceeded"}
			}
		}
	}
	canonical, err := canonicalEventJSON(ev, l.prevChecksum)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(canonical)
	ev.Checksum = hex.EncodeToString(sum[:])

	line, err := json.Marshal(ev)
	if err != nil {
		return fmt.Errorf("security: marshal audit event: %w", err)
	}
	// PR #233 review Nice-to-have #6: prior `append(line, '\n')` allocated
	// a fresh backing slice on every Emit (json.Marshal already returned a
	// freshly-allocated slice with no headroom). Two writes on the buffered
	// writer drop one alloc per event on the telemetry hot path; the
	// bufio.Writer absorbs both into a single underlying write syscall.
	if _, err := l.writer.Write(line); err != nil {
		return fmt.Errorf("security: write audit event: %w", err)
	}
	if err := l.writer.WriteByte('\n'); err != nil {
		return fmt.Errorf("security: write audit event newline: %w", err)
	}
	l.prevChecksum = ev.Checksum

	// PR 1c — record the per-event counter once the line has been
	// committed to the buffer. Security-class events are about to fsync
	// in flushLocked; telemetry-class events are batched. Either way
	// the line is durable enough that an operator alert on a missing
	// counter increment after a confirmed event is meaningful. Chain-
	// recovery synthetic events also flow through here, so
	// chain.recovered's counter increments alongside the dedicated
	// audit_chain_recovered_total counter set in emitRecoveryEvent
	// (the two counters serve different alerts: events_total drives
	// per-type SLOs, chain_recovered_total drives integrity alerting).
	l.metrics.RecordEvent(ev.EventType, classifyAuditEvent(ev.EventType))

	if IsSecurityEvent(ev.EventType) {
		return l.flushLocked()
	}
	l.pendingTel++
	if l.pendingTel >= l.batchSize {
		return l.flushLocked()
	}
	return nil
}

// Flush implements [AuditLogger].
func (l *fileAuditLogger) Flush() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.flushLocked()
}

func (l *fileAuditLogger) flushLocked() error {
	if err := l.writer.Flush(); err != nil {
		return fmt.Errorf("security: flush audit buffer: %w", err)
	}
	if err := l.file.Sync(); err != nil {
		return fmt.Errorf("security: fsync audit log: %w", err)
	}
	l.pendingTel = 0
	return nil
}

// Close implements [AuditLogger].
func (l *fileAuditLogger) Close() error {
	l.mu.Lock()
	if l.closed {
		l.mu.Unlock()
		return nil
	}
	l.closed = true
	flushErr := l.flushLocked()
	closeErr := l.file.Close()
	l.mu.Unlock()

	if l.tickerStop != nil {
		l.tickerStopOnce.Do(func() { close(l.tickerStop) })
	}

	// PR #233 deep-review: surface both signals during shutdown — closing a
	// file with a pending fsync error is exactly when both matter. Prior
	// behaviour silently dropped closeErr if flushErr was non-nil.
	return errors.Join(flushErr, closeErr)
}

func (l *fileAuditLogger) tickerLoop(d time.Duration, stop <-chan struct{}) {
	var tickC <-chan time.Time
	if l.testTickC != nil {
		tickC = l.testTickC
	} else {
		t := time.NewTicker(d)
		defer t.Stop()
		tickC = t.C
	}
	for {
		select {
		case <-stop:
			return
		case <-tickC:
			l.mu.Lock()
			if l.closed {
				l.mu.Unlock()
				return
			}
			flushed := false
			if l.pendingTel > 0 {
				_ = l.flushLocked()
				flushed = true
			}
			l.mu.Unlock()
			if flushed && l.testFlushSignal != nil {
				select {
				case l.testFlushSignal <- struct{}{}:
				default:
				}
			}
		}
	}
}

// Path returns the on-disk path of a file-backed logger. Used by tests and
// the main wiring (PR 1b) to surface the resolved location in startup logs.
func (l *fileAuditLogger) Path() string {
	return filepath.Clean(l.path)
}
