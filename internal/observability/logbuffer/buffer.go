// Package logbuffer implements the orchestrator-side per-execution ring
// buffer with on-disk durability described in
// docs/rfcs/0018-structured-logging-framework.md § E and the corresponding
// PR plan (Phase 4a).
//
// Wire ingest (LogService.StreamLogs) and HTTP surfacing of the buffer
// land in Phase 4b/4c (RFC 0018 PRs 5–6); this package only owns the
// in-memory ring + disk store + rate limiter.
package logbuffer

import (
	"sync"
	"sync/atomic"
	"time"

	"go.uber.org/zap"
)

// Entry is the orchestrator's in-memory representation of a structured
// log line. It is intentionally decoupled from the wire proto
// (persatrix.v1.LogEntry) so the on-disk JSONL layout is independent of
// proto evolution.
type Entry struct {
	SchemaVersion   string         `json:"schema_version"`
	Timestamp       time.Time      `json:"timestamp"`
	Level           string         `json:"level"`
	ServiceKind     string         `json:"service_kind,omitempty"`
	ServiceInstance string         `json:"service_instance,omitempty"`
	ServiceRole     string         `json:"service_role,omitempty"`
	Message         string         `json:"message"`
	ExecutionID     string         `json:"execution_id,omitempty"`
	StepID          string         `json:"step_id,omitempty"`
	AgentID         string         `json:"agent_id,omitempty"`
	RequestID       string         `json:"request_id,omitempty"`
	TraceID         string         `json:"trace_id,omitempty"`
	SpanID          string         `json:"span_id,omitempty"`
	Attributes      map[string]any `json:"attributes,omitempty"`
	Source          *Source        `json:"source,omitempty"`
}

// Source mirrors LogEntry.Source on the wire.
type Source struct {
	File     string `json:"file,omitempty"`
	Line     uint32 `json:"line,omitempty"`
	Function string `json:"function,omitempty"`
}

// DropReason classifies why an entry was rejected by Append. Surfaced by
// the buffer's drop counters; future RFC 0019 metrics will read these
// (this PR exposes counters but does not wire metric instruments).
type DropReason int

const (
	// DropNone is the zero value used when no drop occurred.
	DropNone DropReason = iota
	// DropBelowLevel — entry severity below PERSATRIX_LOGBUFFER_DROP_LEVEL.
	DropBelowLevel
	// DropRateLimit — per-execution token bucket exhausted.
	DropRateLimit
)

// Config holds buffer + disk + rate-limit settings. Defaults match
// RFC § Resolved Decisions #2.
type Config struct {
	// PerExecution is the per-execution ring capacity (entries).
	// Default: PERSATRIX_LOGBUFFER_PER_EXEC=1000.
	PerExecution int
	// MaxExecutions is the LRU cap across executions.
	// Default: PERSATRIX_LOGBUFFER_MAX_EXEC=50.
	MaxExecutions int
	// Dir is the on-disk root for sealed-ring flushes.
	// Default: PERSATRIX_LOGBUFFER_DIR=data/logs.
	Dir string
	// DiskCapBytes is the soft cap for on-disk usage; oldest sealed
	// executions are evicted first when exceeded.
	// Default: PERSATRIX_LOGBUFFER_DISK_MB=512.
	DiskCapBytes int64
	// DropLevel is the minimum severity admitted to the ring; lower
	// severities are dropped without affecting stdout emission.
	// Default: PERSATRIX_LOGBUFFER_DROP_LEVEL=DEBUG.
	DropLevel string
	// RatePerExec is the token-bucket refill rate (entries / second)
	// per execution. Default: PERSATRIX_LOGBUFFER_RATE_PER_EXEC=1000.
	RatePerExec int
}

// Defaults returns a Config with all RFC-pinned defaults.
func Defaults() Config {
	return Config{
		PerExecution:  1000,
		MaxExecutions: 50,
		Dir:           "data/logs",
		DiskCapBytes:  512 * 1024 * 1024,
		DropLevel:     "DEBUG",
		RatePerExec:   1000,
	}
}

// Buffer is the orchestrator-wide log buffer. Safe for concurrent use.
type Buffer struct {
	cfg    Config
	logger *zap.Logger

	mu     sync.RWMutex
	rings  map[string]*executionRing // execution_id → ring
	lru    []string                  // execution_id ordered oldest→newest by last access
	disk   *diskStore
	closed atomic.Bool

	// Counters surfaced for tests and (future) metric instrumentation.
	droppedBelowLevel atomic.Uint64
	droppedRate       atomic.Uint64
	evictedActive     atomic.Uint64 // active rings evicted by LRU
	evictedSealed     atomic.Uint64 // sealed rings evicted (after disk flush)

	// Per-execution one-shot WARN gate so a noisy execution emits
	// exactly one rate-limit warning per process lifetime, per
	// RFC § E "single throttled WARN log per execution".
	rateWarnedMu sync.Mutex
	rateWarned   map[string]struct{}
}

// New constructs a Buffer, applying defaults for any zero-valued Config
// field, ensuring the on-disk directory exists with 0700 permissions,
// and warm-loading any pre-existing sealed executions from disk.
func New(cfg Config, logger *zap.Logger) (*Buffer, error) {
	if logger == nil {
		logger = zap.NewNop()
	}
	cfg = applyDefaults(cfg)

	disk, err := newDiskStore(cfg.Dir, cfg.DiskCapBytes, logger)
	if err != nil {
		return nil, err
	}

	b := &Buffer{
		cfg:        cfg,
		logger:     logger,
		rings:      make(map[string]*executionRing),
		disk:       disk,
		rateWarned: make(map[string]struct{}),
	}
	if err := b.warmLoad(); err != nil {
		return nil, err
	}
	return b, nil
}

func applyDefaults(cfg Config) Config {
	d := Defaults()
	if cfg.PerExecution <= 0 {
		cfg.PerExecution = d.PerExecution
	}
	if cfg.MaxExecutions <= 0 {
		cfg.MaxExecutions = d.MaxExecutions
	}
	if cfg.Dir == "" {
		cfg.Dir = d.Dir
	}
	if cfg.DiskCapBytes <= 0 {
		cfg.DiskCapBytes = d.DiskCapBytes
	}
	if cfg.DropLevel == "" {
		cfg.DropLevel = d.DropLevel
	}
	if cfg.RatePerExec <= 0 {
		cfg.RatePerExec = d.RatePerExec
	}
	return cfg
}

// Append admits an entry into the appropriate execution ring, applying
// the drop-level filter and per-execution rate limiter. Returns the
// reason for any drop (DropNone if admitted).
//
// Entries with empty ExecutionID are dropped silently — the buffer is
// only meaningful inside a workflow execution; orchestrator-global logs
// are observable on stdout.
func (b *Buffer) Append(entry Entry) DropReason {
	if b.closed.Load() {
		return DropBelowLevel
	}
	if entry.ExecutionID == "" {
		return DropBelowLevel
	}
	if !levelGE(entry.Level, b.cfg.DropLevel) {
		b.droppedBelowLevel.Add(1)
		return DropBelowLevel
	}

	ring := b.getOrCreateRing(entry.ExecutionID)
	if !ring.allow(entry.Level) {
		b.droppedRate.Add(1)
		b.warnRateOnce(entry.ExecutionID)
		return DropRateLimit
	}
	ring.append(entry)
	return DropNone
}

// Seal marks the named execution's ring as sealed (no further admit
// after seal is allowed only for entries of the same execution; in
// practice executors stop emitting once the workflow terminates) and
// flushes the ring's contents to disk in a single batch.
//
// Sealed rings remain queryable via Snapshot until LRU-evicted.
func (b *Buffer) Seal(executionID string) error {
	b.mu.RLock()
	ring, ok := b.rings[executionID]
	b.mu.RUnlock()
	if !ok {
		return nil
	}
	entries := ring.seal()
	if len(entries) == 0 {
		ring.markFlushed()
		return nil
	}
	if err := b.disk.flush(executionID, entries); err != nil {
		return err
	}
	ring.markFlushed()
	return nil
}

// Snapshot returns a copy of the named execution's currently-buffered
// entries (oldest first). Returns nil for unknown execution IDs.
func (b *Buffer) Snapshot(executionID string) []Entry {
	b.mu.RLock()
	ring, ok := b.rings[executionID]
	b.mu.RUnlock()
	if !ok {
		// Try to load sealed entries from disk on miss — supports
		// post-restart queries before warm-load promotes them.
		return b.disk.read(executionID)
	}
	return ring.snapshot()
}

// Close flushes all sealed rings (idempotent) and prevents further
// appends. Active (un-sealed) rings are intentionally not flushed —
// they correspond to in-flight executions whose terminal hook will
// call Seal in the normal path.
func (b *Buffer) Close() error {
	if !b.closed.CompareAndSwap(false, true) {
		return nil
	}
	return nil
}

// Stats returns a snapshot of the buffer's internal counters. Intended
// for tests and for the future metrics wiring referenced in RFC 0019.
type Stats struct {
	ActiveRings       int
	DroppedBelowLevel uint64
	DroppedRate       uint64
	EvictedActive     uint64
	EvictedSealed     uint64
}

// Stats returns counter snapshots.
func (b *Buffer) Stats() Stats {
	b.mu.RLock()
	n := len(b.rings)
	b.mu.RUnlock()
	return Stats{
		ActiveRings:       n,
		DroppedBelowLevel: b.droppedBelowLevel.Load(),
		DroppedRate:       b.droppedRate.Load(),
		EvictedActive:     b.evictedActive.Load(),
		EvictedSealed:     b.evictedSealed.Load(),
	}
}

func (b *Buffer) warnRateOnce(executionID string) {
	b.rateWarnedMu.Lock()
	_, already := b.rateWarned[executionID]
	if !already {
		b.rateWarned[executionID] = struct{}{}
	}
	b.rateWarnedMu.Unlock()
	if already {
		return
	}
	b.logger.Warn(
		"log buffer rate limit exceeded",
		zap.String("execution_id", executionID),
		zap.Int("rate_per_exec", b.cfg.RatePerExec),
	)
}
