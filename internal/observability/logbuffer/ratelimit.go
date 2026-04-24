package logbuffer

import (
	"sync"
	"time"
)

// tokenBucket is a simple per-execution rate limiter. Tokens refill at
// `ratePerSec` per second and are capped at `ratePerSec` (1 second of
// burst). A zero rate disables limiting (allow always succeeds).
//
// The implementation uses wall-clock arithmetic on each call rather
// than a background ticker to avoid one goroutine per active execution.
type tokenBucket struct {
	mu         sync.Mutex
	rate       float64 // tokens per second
	capacity   float64
	tokens     float64
	lastRefill time.Time
	now        func() time.Time // injectable for tests
}

func newTokenBucket(ratePerSec int) *tokenBucket {
	if ratePerSec <= 0 {
		return &tokenBucket{rate: 0, capacity: 0, now: time.Now}
	}
	r := float64(ratePerSec)
	return &tokenBucket{
		rate:       r,
		capacity:   r,
		tokens:     r,
		lastRefill: time.Now(),
		now:        time.Now,
	}
}

// allow consumes one token if available and returns true; otherwise
// returns false without blocking.
func (t *tokenBucket) allow() bool {
	// Write-once contract: t.rate is set at construction time
	// (newTokenBucket) and is never mutated, so this lock-free
	// short-circuit is race-free. A future mutator that violates the
	// write-once contract must add a load-acquire here. (PR #172
	// review nice-to-have.)
	if t.rate == 0 {
		return true
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	now := t.now()
	elapsed := now.Sub(t.lastRefill).Seconds()
	if elapsed > 0 {
		t.tokens += elapsed * t.rate
		if t.tokens > t.capacity {
			t.tokens = t.capacity
		}
		t.lastRefill = now
	}
	if t.tokens < 1 {
		return false
	}
	t.tokens--
	return true
}
