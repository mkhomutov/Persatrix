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
	"errors"
	"regexp"
	"sync"
	"sync/atomic"
	"time"

	"go.uber.org/zap"
)

// errInvalidExecutionID is returned by Seal when the caller-supplied
// execution ID would be unsafe to use as a filesystem path component.
// Exported via the package's Err* surface in PR 5; for now it is an
// internal sentinel kept package-private to avoid widening the public
// API in a Phase 4a-only PR.
var errInvalidExecutionID = errors.New("logbuffer: invalid execution_id")

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
	// DropClosed — buffer has been closed; no further admits.
	// Distinct from DropBelowLevel so callers (and the future RFC 0019
	// metric) can tell shutdown-races apart from severity filtering.
	DropClosed
	// DropNoExecID — entry has empty ExecutionID. The buffer is
	// per-execution by design; orchestrator-global logs are observable
	// on stdout only.
	DropNoExecID
	// DropInvalidID — ExecutionID failed validExecutionID. Surfaced as
	// its own reason because the value flows directly into a filesystem
	// path (see disk.go); silently bucketing it as DropNoExecID would
	// hide a misuse / potential path-traversal attempt from operators.
	DropInvalidID
)

// executionIDPattern bounds the ExecutionID character set to characters
// safe for use as a single path component on every supported OS:
// alphanumerics plus hyphen, underscore. Length is capped to keep flush
// paths well under the smallest filesystem NAME_MAX (255 on most ext*,
// HFS+, NTFS) once the sequence suffix and ".jsonl" are appended.
//
// The pattern is intentionally stricter than the agent-ID regex from
// schemas/agent.schema.json (which allows leading digits / hyphens at
// the boundary): execution IDs are produced server-side by the
// orchestrator (UUIDs, ULIDs, or hex hashes) so the loose form is
// neither needed nor safe to widen.
var executionIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

// validExecutionID guards every code path that uses ExecutionID as a
// filesystem path component (see disk.flush / disk.read /
// disk.evictIfOverCap). A producer that supplies "../../etc" or
// "a/b" would otherwise escape the configured root and let RemoveAll
// during eviction delete arbitrary directories. Phase 4b (PR 5) will
// expose Append to network input, at which point this validator is
// the single boundary preventing OWASP A03 (Injection) /
// path-traversal abuse — keep it strict.
func validExecutionID(s string) bool {
	return executionIDPattern.MatchString(s)
}

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
	droppedClosed     atomic.Uint64
	droppedNoExecID   atomic.Uint64
	droppedInvalidID  atomic.Uint64
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
// Drop-reason precedence (intentional ordering):
//  1. DropClosed       — buffer is shut down.
//  2. DropNoExecID     — entry has no ExecutionID (orchestrator-global
//     log; observe via stdout).
//  3. DropInvalidID    — ExecutionID fails validExecutionID; protects
//     the disk path layer from traversal (see disk.go).
//  4. DropBelowLevel   — severity below the configured drop level.
//  5. DropRateLimit    — per-execution token bucket exhausted.
//
// Each branch increments its own counter so misuse (closed-buffer
// races, malformed IDs) is observable rather than silently swallowed.
func (b *Buffer) Append(entry Entry) DropReason {
	if b.closed.Load() {
		b.droppedClosed.Add(1)
		return DropClosed
	}
	if entry.ExecutionID == "" {
		b.droppedNoExecID.Add(1)
		return DropNoExecID
	}
	if !validExecutionID(entry.ExecutionID) {
		b.droppedInvalidID.Add(1)
		return DropInvalidID
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
//
// Invalid execution IDs are rejected here as well: the value would
// otherwise reach disk.flush as a path component (see validExecutionID).
func (b *Buffer) Seal(executionID string) error {
	if !validExecutionID(executionID) {
		return errInvalidExecutionID
	}
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
// entries (oldest first). Returns nil for unknown or invalid execution
// IDs (the latter cannot exist in b.rings or on disk because they are
// rejected at Append/Seal).
func (b *Buffer) Snapshot(executionID string) []Entry {
	if !validExecutionID(executionID) {
		return nil
	}
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

// Close is an idempotent shutdown gate that prevents further Append
// calls. Rings that were sealed via Seal() prior to Close remain
// durable on disk and queryable via Snapshot; un-sealed (active)
// rings are intentionally NOT flushed here — the orchestrator's
// terminal hooks own per-execution sealing in the normal path, and
// flushing partial in-flight rings would complicate restart semantics
// (the agent shipper may still be delivering tail entries).
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
	DroppedClosed     uint64
	DroppedNoExecID   uint64
	DroppedInvalidID  uint64
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
		DroppedClosed:     b.droppedClosed.Load(),
		DroppedNoExecID:   b.droppedNoExecID.Load(),
		DroppedInvalidID:  b.droppedInvalidID.Load(),
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
