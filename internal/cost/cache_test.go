package cost

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// --- CacheKey ---

func TestCacheKey_SameInputs_SameKey(t *testing.T) {
	ctx := map[string]string{"k": "v"}
	k1 := CacheKey("agent-a", "payload", ctx)
	k2 := CacheKey("agent-a", "payload", ctx)
	assert.Equal(t, k1, k2)
}

func TestCacheKey_DifferentAgent_DifferentKey(t *testing.T) {
	ctx := map[string]string{"k": "v"}
	k1 := CacheKey("agent-a", "payload", ctx)
	k2 := CacheKey("agent-b", "payload", ctx)
	assert.NotEqual(t, k1, k2)
}

func TestCacheKey_DifferentPayload_DifferentKey(t *testing.T) {
	k1 := CacheKey("agent-a", "payload-1", nil)
	k2 := CacheKey("agent-a", "payload-2", nil)
	assert.NotEqual(t, k1, k2)
}

func TestCacheKey_DifferentContext_DifferentKey(t *testing.T) {
	k1 := CacheKey("agent-a", "payload", map[string]string{"k": "v1"})
	k2 := CacheKey("agent-a", "payload", map[string]string{"k": "v2"})
	assert.NotEqual(t, k1, k2)
}

func TestCacheKey_NilContext(t *testing.T) {
	k1 := CacheKey("agent-a", "payload", nil)
	k2 := CacheKey("agent-a", "payload", nil)
	assert.Equal(t, k1, k2)
}

// TestCacheKey_MultiKeyContext_Deterministic verifies that CacheKey produces the
// same hash regardless of Go map iteration order when context has multiple keys.
// This is a regression test for a bug where context keys were iterated without
// sorting, producing non-deterministic hashes that caused cache misses.
// (PR #91 review: pre-existing CacheKey non-determinism from PR #88)
func TestCacheKey_MultiKeyContext_Deterministic(t *testing.T) {
	// Use multiple keys to increase the chance of non-deterministic iteration.
	ctx := map[string]string{
		"model":     "gpt-4",
		"task_type": "code_review",
		"language":  "go",
		"priority":  "high",
	}
	// Run many iterations — with unsorted iteration, different runs would
	// produce different hashes with high probability for 4+ keys.
	first := CacheKey("agent-a", "payload", ctx)
	for i := 0; i < 100; i++ {
		assert.Equal(t, first, CacheKey("agent-a", "payload", ctx),
			"CacheKey must be deterministic across calls (iteration %d)", i)
	}
}

// --- ResponseCache Get/Put ---

func TestResponseCache_PutAndGet(t *testing.T) {
	cache := NewResponseCache(100, time.Hour, zap.NewNop())

	resp := CachedResponse{Output: "result-1", Metadata: map[string]string{"k": "v"}}
	cache.Put("key-1", resp)

	got, ok := cache.Get("key-1")
	require.True(t, ok)
	assert.Equal(t, "result-1", got.Output)
	assert.Equal(t, "v", got.Metadata["k"])
}

func TestResponseCache_Miss(t *testing.T) {
	cache := NewResponseCache(100, time.Hour, zap.NewNop())

	_, ok := cache.Get("nonexistent")
	assert.False(t, ok)
}

func TestResponseCache_TTLExpiry(t *testing.T) {
	// Use a very short TTL.
	cache := NewResponseCache(100, time.Millisecond, zap.NewNop())

	cache.Put("key-1", CachedResponse{Output: "result"})
	time.Sleep(5 * time.Millisecond)

	_, ok := cache.Get("key-1")
	assert.False(t, ok, "expected cache miss after TTL expiry")
}

func TestResponseCache_LRUEviction(t *testing.T) {
	cache := NewResponseCache(2, time.Hour, zap.NewNop())

	cache.Put("key-1", CachedResponse{Output: "result-1"})
	cache.Put("key-2", CachedResponse{Output: "result-2"})
	cache.Put("key-3", CachedResponse{Output: "result-3"}) // should evict key-1

	_, ok := cache.Get("key-1")
	assert.False(t, ok, "key-1 should have been evicted")

	got, ok := cache.Get("key-2")
	require.True(t, ok)
	assert.Equal(t, "result-2", got.Output)

	got, ok = cache.Get("key-3")
	require.True(t, ok)
	assert.Equal(t, "result-3", got.Output)
}

func TestResponseCache_LRU_AccessRefreshes(t *testing.T) {
	cache := NewResponseCache(2, time.Hour, zap.NewNop())

	cache.Put("key-1", CachedResponse{Output: "result-1"})
	cache.Put("key-2", CachedResponse{Output: "result-2"})

	// Access key-1 to make it recently used.
	cache.Get("key-1")

	// Adding key-3 should evict key-2 (now the LRU), not key-1.
	cache.Put("key-3", CachedResponse{Output: "result-3"})

	_, ok := cache.Get("key-1")
	assert.True(t, ok, "key-1 should still be in cache after access")

	_, ok = cache.Get("key-2")
	assert.False(t, ok, "key-2 should have been evicted")
}

func TestResponseCache_UpdateExisting(t *testing.T) {
	cache := NewResponseCache(100, time.Hour, zap.NewNop())

	cache.Put("key-1", CachedResponse{Output: "old"})
	cache.Put("key-1", CachedResponse{Output: "new"})

	got, ok := cache.Get("key-1")
	require.True(t, ok)
	assert.Equal(t, "new", got.Output)
	assert.Equal(t, 1, cache.Len())
}

func TestResponseCache_Len(t *testing.T) {
	cache := NewResponseCache(100, time.Hour, zap.NewNop())
	assert.Equal(t, 0, cache.Len())

	cache.Put("key-1", CachedResponse{Output: "r1"})
	assert.Equal(t, 1, cache.Len())

	cache.Put("key-2", CachedResponse{Output: "r2"})
	assert.Equal(t, 2, cache.Len())
}

func TestResponseCache_Stats(t *testing.T) {
	cache := NewResponseCache(100, time.Hour, zap.NewNop())

	cache.Put("key-1", CachedResponse{Output: "r1"})

	cache.Get("key-1")       // hit
	cache.Get("key-1")       // hit
	cache.Get("nonexistent") // miss

	hits, misses := cache.Stats()
	assert.Equal(t, int64(2), hits)
	assert.Equal(t, int64(1), misses)
}

func TestResponseCache_DefaultValues(t *testing.T) {
	// Zero/negative maxEntries and TTL should be clamped to defaults.
	cache := NewResponseCache(0, 0, nil)
	assert.NotNil(t, cache)
	assert.Equal(t, 0, cache.Len())
}

func TestResponseCache_ConcurrentAccess(t *testing.T) {
	cache := NewResponseCache(1000, time.Hour, zap.NewNop())

	done := make(chan struct{})
	for i := range 10 {
		go func(id int) {
			defer func() { done <- struct{}{} }()
			for j := range 100 {
				key := CacheKey("agent", "payload-"+string(rune('0'+id))+"-"+string(rune('0'+j)), nil)
				cache.Put(key, CachedResponse{Output: "result"})
				cache.Get(key)
			}
		}(i)
	}
	for range 10 {
		<-done
	}
	// Just verify no panic under -race.
}
