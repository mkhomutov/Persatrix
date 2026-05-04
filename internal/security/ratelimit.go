package security

import (
	"container/list"
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"go.uber.org/zap"
)

// RateLimitConfig configures a [RateLimiter].
//
// CallsPerWindow is the maximum number of admitted calls per agent within
// a rolling WindowSeconds-second window. MaxTrackedAgents bounds the
// per-agent ring map under self-reported agent IDs (PR #232 review SF-1):
// when more distinct IDs are seen than the cap, the LRU drops the
// oldest-touched ring and emits `rate_limit.agent_evicted`. The dropped
// agent's history is gone; its next call starts from an empty ring.
//
// Now is injectable for deterministic tests; defaults to [time.Now].
//
// Auditor is optional; when nil the limiter still enforces but does not
// emit audit events. UnauthenticatedID is the bucket key used for empty
// agent IDs; defaults to `anonymous`.
type RateLimitConfig struct {
	CallsPerWindow    int
	WindowSeconds     int
	MaxTrackedAgents  int
	Enabled           bool
	UnauthenticatedID string
	Now               func() time.Time
	Logger            *zap.Logger
	Auditor           AuditLogger
}

// RateLimiterOption is a functional configuration helper used by tests
// and helper constructors. Production code constructs the
// [RateLimitConfig] directly.
type RateLimiterOption func(*RateLimitConfig)

// RateLimiter enforces a per-agent sliding-window call limit with a
// bounded LRU map of agent rings.
//
// The implementation is conservative in two ways: (1) a per-agent ring
// of admit timestamps is more accurate than a fixed-window counter at
// window boundaries; (2) the agent map is hard-capped to defend against
// self-reported X-Agent-ID flooding attacks until token validation
// lands in Phase 4 (RFC 0009 §B). Eviction emits a telemetry-class
// audit event so operators can correlate spikes with cardinality
// blow-ups.
type RateLimiter struct {
	cfg RateLimitConfig

	mu         sync.Mutex
	rings      map[string]*list.Element // agentID -> LRU node holding *agentRing
	lru        *list.List               // front = MRU, back = LRU
	lastEmitMu sync.Mutex
	lastEmit   map[string]time.Time // throttles `rate_limit.violated` per agent
}

type agentRing struct {
	agentID string
	calls   []time.Time // ring of recent admit timestamps
	head    int         // next slot to write
	count   int         // populated entries (<= cap)
}

// NewRateLimiter constructs a [RateLimiter] from cfg. Returns an error
// when the configuration is malformed (non-positive limits with
// Enabled=true).
func NewRateLimiter(cfg RateLimitConfig) (*RateLimiter, error) {
	if cfg.Enabled {
		if cfg.CallsPerWindow <= 0 {
			return nil, errors.New("ratelimit: CallsPerWindow must be > 0 when enabled")
		}
		if cfg.WindowSeconds <= 0 {
			return nil, errors.New("ratelimit: WindowSeconds must be > 0 when enabled")
		}
	}
	if cfg.MaxTrackedAgents <= 0 {
		cfg.MaxTrackedAgents = 1000
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.Logger == nil {
		cfg.Logger = zap.NewNop()
	}
	if cfg.UnauthenticatedID == "" {
		cfg.UnauthenticatedID = "anonymous"
	}
	return &RateLimiter{
		cfg:      cfg,
		rings:    make(map[string]*list.Element),
		lru:      list.New(),
		lastEmit: make(map[string]time.Time),
	}, nil
}

// Allow reports whether the call from agentID may proceed, recording
// the admission timestamp on success.
//
// An empty agentID is treated as the anonymous bucket so unauthenticated
// callers cannot bypass the limiter; an `rate_limit.unauthenticated_caller`
// audit event is emitted (security-class, fsync'd) so operators can
// detect flooding attempts.
//
// On deny, an `rate_limit.violated` event is emitted (throttled per
// agent to one event per WindowSeconds to avoid amplifying the very
// flood the limiter is mitigating).
func (rl *RateLimiter) Allow(agentID string) bool {
	if !rl.cfg.Enabled {
		return true
	}
	resolved, anon := rl.resolveAgentID(agentID)
	now := rl.cfg.Now()
	cutoff := now.Add(-time.Duration(rl.cfg.WindowSeconds) * time.Second)

	rl.mu.Lock()
	ring, evicted := rl.touchRingLocked(resolved)
	ring.evictOlderThan(cutoff)
	allowed := ring.count < rl.cfg.CallsPerWindow
	if allowed {
		ring.append(now)
	}
	rl.mu.Unlock()

	for _, victim := range evicted {
		rl.emit(AuditEvent{
			Timestamp: now,
			EventType: AuditRateLimitAgentEvicted,
			AgentID:   victim,
			Action:    "rate_limit.evict",
			Resource:  victim,
			Outcome:   "evicted",
		})
	}

	if anon {
		rl.emit(AuditEvent{
			EventType: AuditRateLimitUnauthenticatedCall,
			AgentID:   resolved,
			Action:    "rate_limit.check",
			Resource:  resolved,
			Outcome:   "warn",
		})
	}
	if !allowed && rl.shouldEmitViolation(resolved, now) {
		rl.emit(AuditEvent{
			EventType: AuditRateLimitViolated,
			AgentID:   resolved,
			Action:    "rate_limit.check",
			Resource:  resolved,
			Outcome:   "deny",
			Detail: map[string]any{
				"calls_per_window": rl.cfg.CallsPerWindow,
				"window_seconds":   rl.cfg.WindowSeconds,
			},
		})
	}
	return allowed
}

// Reset clears the ring for agentID (next call starts from empty) and
// purges any associated `lastEmit` throttle entry so the auxiliary map
// cannot grow unbounded under repeated reset-then-deny cycles
// (PR #244 review M-01 follow-up — mirrors evictTailLocked). Calling
// Reset on an unknown agent is a no-op.
func (rl *RateLimiter) Reset(agentID string) {
	resolved, _ := rl.resolveAgentID(agentID)
	rl.mu.Lock()
	if elem, ok := rl.rings[resolved]; ok {
		rl.lru.Remove(elem)
		delete(rl.rings, resolved)
	}
	rl.mu.Unlock()
	rl.lastEmitMu.Lock()
	delete(rl.lastEmit, resolved)
	rl.lastEmitMu.Unlock()
}

// TrackedAgents returns the current agent-map size. Intended for
// metrics / tests; not part of the hot path.
func (rl *RateLimiter) TrackedAgents() int {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	return len(rl.rings)
}

func (rl *RateLimiter) resolveAgentID(id string) (string, bool) {
	if id == "" {
		return rl.cfg.UnauthenticatedID, true
	}
	return id, false
}

// touchRingLocked moves the ring for agentID to the LRU front,
// creating it (and evicting the LRU tail when over the cap) on first
// touch. Returns the ring plus any agent IDs that were evicted; the
// caller emits the audit events after releasing rl.mu so a slow audit
// sink cannot stall the hot path.
//
// PR #244 review L-01: the previous signature took a `now time.Time`
// argument that was never used inside the function (eviction is
// position-driven via the LRU list, not time-driven). Dropped to
// remove the dead `_ = now` suppressor.
func (rl *RateLimiter) touchRingLocked(agentID string) (*agentRing, []string) {
	if elem, ok := rl.rings[agentID]; ok {
		rl.lru.MoveToFront(elem)
		return elem.Value.(*agentRing), nil
	}
	ring := &agentRing{
		agentID: agentID,
		calls:   make([]time.Time, rl.cfg.CallsPerWindow),
	}
	elem := rl.lru.PushFront(ring)
	rl.rings[agentID] = elem
	var evicted []string
	for len(rl.rings) > rl.cfg.MaxTrackedAgents {
		if id := rl.evictTailLocked(); id != "" {
			evicted = append(evicted, id)
		} else {
			break
		}
	}
	return ring, evicted
}

// evictTailLocked drops the LRU tail and returns the evicted agent ID
// (or "" when the LRU is empty). Caller holds rl.mu.
//
// PR #244 review M-02: also purges the corresponding `lastEmit` entry
// so the violation-throttle map cannot grow unbounded after the LRU
// ring map evicts the agent. lastEmitMu is acquired AFTER the caller's
// rl.mu is held; no other code path holds lastEmitMu and then
// attempts rl.mu, so the nested-lock order is safe.
func (rl *RateLimiter) evictTailLocked() string {
	tail := rl.lru.Back()
	if tail == nil {
		return ""
	}
	victim := tail.Value.(*agentRing)
	rl.lru.Remove(tail)
	delete(rl.rings, victim.agentID)
	rl.lastEmitMu.Lock()
	delete(rl.lastEmit, victim.agentID)
	rl.lastEmitMu.Unlock()
	return victim.agentID
}

// shouldEmitViolation throttles `rate_limit.violated` to at most one
// event per agent per window so the audit chain is not flooded by the
// very burst the limiter is rejecting.
func (rl *RateLimiter) shouldEmitViolation(agentID string, now time.Time) bool {
	rl.lastEmitMu.Lock()
	defer rl.lastEmitMu.Unlock()
	last, ok := rl.lastEmit[agentID]
	if ok && now.Sub(last) < time.Duration(rl.cfg.WindowSeconds)*time.Second {
		return false
	}
	rl.lastEmit[agentID] = now
	return true
}

func (rl *RateLimiter) emit(ev AuditEvent) {
	if rl.cfg.Auditor == nil {
		return
	}
	ctx := context.Background()
	if err := rl.cfg.Auditor.Emit(ctx, ev); err != nil {
		rl.cfg.Logger.Debug("rate limiter audit emit failed",
			zap.String("event_type", string(ev.EventType)),
			zap.String("agent_id", ev.AgentID),
			zap.Error(err),
		)
	}
}

func (r *agentRing) append(t time.Time) {
	r.calls[r.head] = t
	r.head = (r.head + 1) % len(r.calls)
	if r.count < len(r.calls) {
		r.count++
	}
}

// evictOlderThan walks the ring and drops entries older than cutoff by
// rebuilding the populated entries in order. The ring is small
// (CallsPerWindow timestamps) so the linear scan is cheaper than a
// per-entry heap.
func (r *agentRing) evictOlderThan(cutoff time.Time) {
	if r.count == 0 {
		return
	}
	// PR #244 review L-02: was previously named `cap`, which shadowed
	// the builtin and obscured the intent. `ringCap` reads as "the
	// fixed capacity of the per-agent ring".
	ringCap := len(r.calls)
	kept := make([]time.Time, 0, r.count)
	start := (r.head - r.count + ringCap) % ringCap
	for i := 0; i < r.count; i++ {
		t := r.calls[(start+i)%ringCap]
		if t.After(cutoff) {
			kept = append(kept, t)
		}
	}
	r.count = len(kept)
	r.head = r.count % ringCap
	for i := 0; i < r.count; i++ {
		r.calls[i] = kept[i]
	}
}

// String implements [fmt.Stringer] for ring debugging in test failures.
func (r *agentRing) String() string {
	return fmt.Sprintf("agentRing{id=%s count=%d/%d head=%d}", r.agentID, r.count, len(r.calls), r.head)
}
