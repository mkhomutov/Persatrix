package security

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"go.uber.org/zap"
)

// ViolationType labels the class of repeated bad behaviour the circuit
// breaker tracks. The set is closed: each constant maps to a row in the
// RFC 0009 §H quarantine threshold table.
type ViolationType string

const (
	ViolationCapability ViolationType = "capability"
	ViolationRateLimit  ViolationType = "rate_limit"
	ViolationToolDenied ViolationType = "tool_denied"
	ViolationInputFlag  ViolationType = "input_flagged"
)

// ThresholdRule defines the count + rolling window after which an agent
// is quarantined for a given [ViolationType].
//
// Count and Window must both be > 0 for active rules; [NewCircuitBreaker]
// rejects any non-Disabled rule that violates either bound (ISSUE-0001).
//
// Disabled is the explicit no-op seam: when true, [RecordViolation]
// records nothing toward the rolling counter for that violation type
// and the breaker can never open on it. Count and Window are ignored on
// disabled rules. Tests that need to suppress the breaker should set
// Disabled rather than rely on the previous "Window: 0 → never open"
// implicit (which silently disabled the rule and is now a config error).
type ThresholdRule struct {
	Count    int
	Window   time.Duration
	Disabled bool
}

// CircuitBreakerConfig configures a [CircuitBreaker]. Thresholds is the
// closed map of (violation -> rule); a violation type missing from the
// map is recorded but never opens the breaker. Now is injectable for
// deterministic tests.
type CircuitBreakerConfig struct {
	Thresholds map[ViolationType]ThresholdRule
	Now        func() time.Time
	Logger     *zap.Logger
	Auditor    AuditLogger
}

// CircuitBreaker tracks per-(agent, violationType) rolling counters and
// quarantines agents whose count crosses the configured threshold within
// the rolling window. Quarantine persists until [Unquarantine] is called
// (no automatic recovery in v0.3.0 per RFC 0009 §H).
type CircuitBreaker struct {
	cfg CircuitBreakerConfig

	mu          sync.Mutex
	violations  map[string]map[ViolationType][]time.Time
	quarantined map[string]quarantineEntry

	// quarantinedCount mirrors len(quarantined) for lock-free reads on
	// the request hot path (PR #244 round-2 review L-05). Without this,
	// every anonymous REST/gRPC request acquires `cb.mu` purely to
	// answer HasAnyQuarantined(), which serialises an otherwise
	// independent flow of traffic — a noticeable contention source
	// under the exact DoS conditions the breaker is meant to handle.
	//
	// Invariant: quarantinedCount.Load() == int32(len(quarantined)) at
	// every point where `cb.mu` is not held by another goroutine.
	// Both write sites (open in RecordViolation, decrement in
	// Unquarantine) bracket the map mutation under `cb.mu`, so a
	// concurrent reader can observe at most a one-event lag — which is
	// the same TOCTOU window the existing mutex-guarded reader had.
	quarantinedCount atomic.Int32
}

type quarantineEntry struct {
	since  time.Time
	reason ViolationType
}

// NewCircuitBreaker constructs a [CircuitBreaker] from cfg.
//
// Each non-Disabled rule in cfg.Thresholds must have Count > 0 and
// Window > 0; otherwise the breaker would silently never open for that
// violation type (ISSUE-0001). Rules with Disabled: true bypass the
// bounds check and are treated as no-ops at record time.
func NewCircuitBreaker(cfg CircuitBreakerConfig) (*CircuitBreaker, error) {
	if cfg.Thresholds == nil {
		return nil, errors.New("circuitbreaker: Thresholds is required")
	}
	for vt, rule := range cfg.Thresholds {
		if rule.Disabled {
			continue
		}
		if rule.Window <= 0 {
			return nil, fmt.Errorf("circuitbreaker: %s rule has Window=%s; must be > 0 (set Disabled: true to suppress)", vt, rule.Window)
		}
		if rule.Count <= 0 {
			return nil, fmt.Errorf("circuitbreaker: %s rule has Count=%d; must be > 0 (set Disabled: true to suppress)", vt, rule.Count)
		}
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.Logger == nil {
		cfg.Logger = zap.NewNop()
	}
	return &CircuitBreaker{
		cfg:         cfg,
		violations:  make(map[string]map[ViolationType][]time.Time),
		quarantined: make(map[string]quarantineEntry),
	}, nil
}

// RecordViolation appends a violation observation for agentID and opens
// the breaker (emitting `agent.quarantined`) when the rolling count
// crosses the threshold for vt. Repeated calls after the breaker is
// already open are recorded but do not re-emit the quarantine event.
//
// ISSUE-0007: ctx is the inbound request context. It is threaded through
// to the auditor (via [context.WithoutCancel] inside [emit]) so trace
// IDs survive into the audit chain. A nil ctx is tolerated and falls
// back to [context.Background] for non-request paths.
func (cb *CircuitBreaker) RecordViolation(ctx context.Context, agentID string, vt ViolationType) {
	if agentID == "" {
		return
	}
	rule, hasRule := cb.cfg.Thresholds[vt]
	if hasRule && rule.Disabled {
		// Explicit no-op seam (ISSUE-0001): disabled rules contribute
		// nothing to the rolling counter and never open the breaker.
		return
	}
	now := cb.cfg.Now()

	cb.mu.Lock()
	if _, alreadyOpen := cb.quarantined[agentID]; alreadyOpen {
		cb.mu.Unlock()
		return
	}
	per := cb.violations[agentID]
	if per == nil {
		per = make(map[ViolationType][]time.Time)
		cb.violations[agentID] = per
	}
	if !hasRule {
		// Record but never open — telemetry only.
		per[vt] = append(per[vt], now)
		cb.mu.Unlock()
		return
	}
	cutoff := now.Add(-rule.Window)
	kept := per[vt][:0:0]
	for _, t := range per[vt] {
		if t.After(cutoff) {
			kept = append(kept, t)
		}
	}
	kept = append(kept, now)
	per[vt] = kept
	shouldOpen := len(kept) >= rule.Count
	if shouldOpen {
		cb.quarantined[agentID] = quarantineEntry{since: now, reason: vt}
		cb.quarantinedCount.Store(int32(len(cb.quarantined)))
		// PR #244 review M-03 (partial): clear the per-agent violation
		// history when the breaker opens so the entry does not linger
		// after a future Unquarantine restores the agent. A full LRU/TTL
		// over `violations` is deferred — it would require a new config
		// knob and the practical bound (distinct violators within any
		// active window) is much smaller than the rate-limiter
		// cardinality this guards against.
		delete(cb.violations, agentID)
	}
	cb.mu.Unlock()

	if shouldOpen {
		cb.emit(ctx, AuditEvent{
			Timestamp: now,
			EventType: AuditAgentQuarantined,
			AgentID:   agentID,
			Action:    "circuit_breaker.open",
			Resource:  agentID,
			Outcome:   "quarantined",
			Detail: map[string]any{
				"violation_type": string(vt),
				"count":          len(kept),
				"window_seconds": int(rule.Window.Seconds()),
			},
		})
	}
}

// IsQuarantined reports whether agentID is currently quarantined.
func (cb *CircuitBreaker) IsQuarantined(agentID string) bool {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	_, ok := cb.quarantined[agentID]
	return ok
}

// HasAnyQuarantined reports whether at least one agent is currently
// quarantined. Used by the REST/gRPC middleware to close the
// header-omission bypass (PR #244 review H-01): when a quarantine is
// active, anonymous (empty-X-Agent-ID) calls must be denied so a
// quarantined caller cannot drop their header to slip past
// IsQuarantined and re-enter via the anonymous bucket.
//
// Lock-free atomic load (PR #244 round-2 review L-05). The
// `quarantinedCount` field mirrors len(quarantined) and is updated
// under `cb.mu` at both write sites — see the field's invariant
// comment. Suitable for the request hot path because the steady-state
// answer (no quarantine) is the overwhelmingly common case and must
// not contend on the breaker mutex.
func (cb *CircuitBreaker) HasAnyQuarantined() bool {
	return cb.quarantinedCount.Load() > 0
}

// Unquarantine releases agentID and clears its violation history.
// Returns true when the agent was quarantined; false on no-op.
// `actor` is recorded on the audit event for forensics.
//
// ISSUE-0007: ctx is the inbound request context (typically from the
// operator-driven REST unquarantine handler). Nil ctx falls back to
// [context.Background] — Unquarantine is also called from CLI/admin
// paths that have no ambient request scope.
func (cb *CircuitBreaker) Unquarantine(ctx context.Context, agentID, actor string) bool {
	cb.mu.Lock()
	_, ok := cb.quarantined[agentID]
	if ok {
		delete(cb.quarantined, agentID)
		delete(cb.violations, agentID)
		cb.quarantinedCount.Store(int32(len(cb.quarantined)))
	}
	cb.mu.Unlock()
	if ok {
		cb.emit(ctx, AuditEvent{
			Timestamp: cb.cfg.Now(),
			EventType: AuditAgentUnquarantined,
			AgentID:   agentID,
			Action:    "circuit_breaker.close",
			Resource:  agentID,
			Outcome:   "released",
			Detail: map[string]any{
				"actor": actor,
			},
		})
	}
	return ok
}

// QuarantinedAgents returns the current set of quarantined IDs. Useful
// for the unquarantine REST handler and debugging.
func (cb *CircuitBreaker) QuarantinedAgents() []string {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	ids := make([]string, 0, len(cb.quarantined))
	for id := range cb.quarantined {
		ids = append(ids, id)
	}
	return ids
}

// emit forwards ev to the configured auditor, detaching the parent ctx
// from cancellation via [context.WithoutCancel] (ISSUE-0007). Detaching
// matters because the breaker's `agent.quarantined` /
// `agent.unquarantined` events are security-class and fsync before
// returning; a fast client cancel between the open/close decision and
// the auditor write would otherwise drop the event the breaker just
// recorded. A nil ctx falls back to [context.Background].
func (cb *CircuitBreaker) emit(ctx context.Context, ev AuditEvent) {
	if cb.cfg.Auditor == nil {
		return
	}
	if ctx == nil {
		ctx = context.Background()
	} else {
		ctx = context.WithoutCancel(ctx)
	}
	if err := cb.cfg.Auditor.Emit(ctx, ev); err != nil {
		cb.cfg.Logger.Debug("circuit breaker audit emit failed",
			zap.String("event_type", string(ev.EventType)),
			zap.String("agent_id", ev.AgentID),
			zap.Error(err),
		)
	}
}
