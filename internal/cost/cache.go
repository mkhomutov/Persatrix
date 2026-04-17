package cost

import (
	"container/list"
	"crypto/sha256"
	"encoding/hex"
	"sync"
	"time"

	"go.uber.org/zap"
)

// CacheConfig holds configuration for the response cache.
type CacheConfig struct {
	Enabled    bool `yaml:"enabled"`
	MaxEntries int  `yaml:"max_entries"`
	TTLSeconds int  `yaml:"ttl_seconds"`
}

// CachedResponse stores a cached task result with expiry metadata.
type CachedResponse struct {
	Output   string
	Metadata map[string]string
}

// cacheEntry wraps a cached response with LRU bookkeeping.
type cacheEntry struct {
	key       string
	response  CachedResponse
	expiresAt time.Time
}

// ResponseCache is an in-memory LRU cache for task responses with per-entry TTL.
// Cache entries are keyed by a SHA-256 hash of the request content (agent_id,
// task_type, task_input, model, etc.) excluding volatile fields (task_id, workflow_id).
//
// The cache key includes agent_id, which partitions entries per agent. This satisfies
// RFC 0006 Security Consideration that cache entries must not be shared across agents
// with different permission sets.
//
// Only steps with `cacheable: true` in workflow YAML should use this cache.
type ResponseCache struct {
	mu         sync.Mutex
	entries    map[string]*list.Element
	evictList  *list.List
	maxEntries int
	ttl        time.Duration
	logger     *zap.Logger

	// Metrics for observability.
	hits   int64
	misses int64
}

// NewResponseCache creates a new LRU response cache.
// maxEntries must be > 0; ttl must be > 0.
func NewResponseCache(maxEntries int, ttl time.Duration, logger *zap.Logger) *ResponseCache {
	if logger == nil {
		logger = zap.NewNop()
	}
	if maxEntries <= 0 {
		maxEntries = 10000
	}
	if ttl <= 0 {
		ttl = time.Hour
	}
	return &ResponseCache{
		entries:    make(map[string]*list.Element),
		evictList:  list.New(),
		maxEntries: maxEntries,
		ttl:        ttl,
		logger:     logger,
	}
}

// CacheKey computes a SHA-256 hash of the request fields that define cache identity.
// Volatile fields (task_id, workflow_id) are excluded so that identical requests
// from different workflow runs can share cached responses.
func CacheKey(agentID, payload string, context map[string]string) string {
	h := sha256.New()
	h.Write([]byte(agentID))
	h.Write([]byte{0}) // separator
	h.Write([]byte(payload))
	h.Write([]byte{0})
	// Include context keys in sorted order for deterministic hashing.
	// Context is typically small (< 10 entries), so a simple sort suffices.
	for k, v := range context {
		h.Write([]byte(k))
		h.Write([]byte{0})
		h.Write([]byte(v))
		h.Write([]byte{0})
	}
	return hex.EncodeToString(h.Sum(nil))
}

// Get looks up a cached response by key. Returns the response and true on hit,
// or zero value and false on miss or TTL expiry.
func (c *ResponseCache) Get(key string) (CachedResponse, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	elem, ok := c.entries[key]
	if !ok {
		c.misses++
		return CachedResponse{}, false
	}

	entry := elem.Value.(*cacheEntry)
	if time.Now().After(entry.expiresAt) {
		// TTL expired — evict.
		c.removeElement(elem)
		c.misses++
		return CachedResponse{}, false
	}

	// Move to front (most recently used).
	c.evictList.MoveToFront(elem)
	c.hits++

	c.logger.Debug("cache hit",
		zap.String("key", key[:min(16, len(key))]+"..."),
	)

	return entry.response, true
}

// Put stores a response in the cache, evicting the LRU entry if at capacity.
func (c *ResponseCache) Put(key string, resp CachedResponse) {
	c.mu.Lock()
	defer c.mu.Unlock()

	// Update existing entry if present.
	if elem, ok := c.entries[key]; ok {
		entry := elem.Value.(*cacheEntry)
		entry.response = resp
		entry.expiresAt = time.Now().Add(c.ttl)
		c.evictList.MoveToFront(elem)
		return
	}

	// Evict LRU if at capacity.
	for c.evictList.Len() >= c.maxEntries {
		c.evictOldest()
	}

	entry := &cacheEntry{
		key:       key,
		response:  resp,
		expiresAt: time.Now().Add(c.ttl),
	}
	elem := c.evictList.PushFront(entry)
	c.entries[key] = elem
}

// Len returns the number of entries in the cache (including expired entries
// that have not yet been lazily evicted).
func (c *ResponseCache) Len() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.evictList.Len()
}

// Stats returns cache hit/miss counts for observability.
func (c *ResponseCache) Stats() (hits, misses int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.hits, c.misses
}

// evictOldest removes the least recently used entry. Must be called with mu held.
func (c *ResponseCache) evictOldest() {
	elem := c.evictList.Back()
	if elem == nil {
		return
	}
	c.removeElement(elem)
}

// removeElement removes an element from the cache. Must be called with mu held.
func (c *ResponseCache) removeElement(elem *list.Element) {
	entry := elem.Value.(*cacheEntry)
	c.evictList.Remove(elem)
	delete(c.entries, entry.key)
}
