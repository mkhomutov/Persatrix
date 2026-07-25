// router_classification.go — the router's dispatch-time classification
// resolve (RFC 0037 §B, v0.3.12 PR 2). Own file per the router_reasoning.go /
// router_salience.go carve-out pattern.
package channels

import (
	"context"
	"sync"

	"go.uber.org/zap"
)

// classificationCache is the router's read-through cache behind
// [ChannelRouter.classificationFor]. The dispatch path was I/O-free after
// commit before RFC 0037 — a per-dispatch `channels`-row read on the store's
// single pinned connection measurably perturbs the fanout→continuation timing
// (the ISSUE-0110 seam's tests catch it), so the row is read once per channel
// and served from memory after that.
//
// Coherence: a cached channel id's effective level changes through exactly two
// paths, and both have a hook.
//
//  1. [ChannelStore.SetChannelClassification] — today only the boot-time
//     reconcile adoption step ([ChannelRouter.ReconcileConfig]), which runs
//     before any dispatch and refreshes this cache via [refresh]. A future
//     runtime reclassification surface (RFC 0037 §Security) MUST route through
//     the router and call [refresh] — a store-direct write would serve the
//     stale level here until restart.
//  2. DELETE + re-create of the same channel id (`DELETE /api/v1/channels/{id}`
//     is store-direct, so the router never sees the row go), which resets the
//     row to the create-path stamp while this map still holds the deleted
//     channel's level. Cached `public` outliving a re-created `internal` row is
//     the one way this cache can UNDER-classify a dispatch — the direction that
//     over-injects once the PR 4 gate arms — so the delete handler calls
//     [ChannelRouter.ForgetChannelClassification], the [PurgeChannelInteraction]
//     precedent for router state that outlives a deleted channel. The eviction
//     also keeps the map from growing one entry per deleted channel forever.
//
// Plain creation needs no hook: a channel's first dispatch read-through-fills
// after the row exists.
type classificationCache struct {
	mu     sync.RWMutex
	levels map[string]string // channel id → §A level, as stored
	// gen guards the read-through refill against the forget TOCTOU: a
	// dispatch that read the row BEFORE a delete could land its [fill] AFTER
	// the delete handler's [forget], re-planting the deleted channel's level
	// for a future re-create — the same stale-under-classify ordering the
	// forget hook exists to close, just through a µs-wide side door. [get]
	// snapshots the generation under the same lock as the miss, [forget]
	// bumps it, and a [fill] whose snapshot is stale is discarded (the next
	// dispatch simply re-reads). Global rather than per-channel on purpose:
	// per-channel would need tombstone entries to survive the delete
	// (defeating the leak eviction), and the cost of a global bump is one
	// extra row read on an unrelated channel's next dispatch, paid only when
	// a delete races a resolve. [refresh] (the reconcile adoption's
	// authoritative write-through, value fresh from its own
	// SetChannelClassification) stays unguarded.
	gen uint64
}

// get returns the cached level and, for the miss path, the generation the
// caller must hand back to [fill] — snapshotted under the same lock as the
// miss so a forget between the miss and the fill is always visible as a gen
// change.
func (c *classificationCache) get(channelID string) (level string, ok bool, gen uint64) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	level, ok = c.levels[channelID]
	return level, ok, c.gen
}

// fill is the read-through half of [ChannelRouter.classificationFor]: it
// stores a row-read result only if no [forget] intervened since the [get]
// that missed. A discarded fill is benign — the resolved value still rides
// the caller's envelope; only the caching is skipped.
func (c *classificationCache) fill(channelID, level string, gen uint64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.gen != gen {
		return
	}
	if c.levels == nil {
		c.levels = make(map[string]string)
	}
	c.levels[channelID] = level
}

// refresh is the authoritative write-through for router-side classification
// WRITES (today: the reconcile adoption, immediately after its own
// [ChannelStore.SetChannelClassification] succeeded) — unconditional by
// design; the gen guard protects only read-through refills of possibly-stale
// row reads.
func (c *classificationCache) refresh(channelID, level string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.levels == nil {
		c.levels = make(map[string]string)
	}
	c.levels[channelID] = level
}

func (c *classificationCache) forget(channelID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.gen++
	delete(c.levels, channelID)
}

// ForgetChannelClassification drops channelID's cached §A level so the next
// dispatch resolves the row again (or, for a channel that stays deleted, stops
// holding an entry for it at all).
//
// Exported for the channel-delete HTTP handler, alongside
// [ChannelRouter.PurgeChannelInteraction] — deleting a channel is the one event
// that invalidates this cache without going through
// [ChannelStore.SetChannelClassification]: the id can be re-created at a
// different level (the REST create path always stamps `internal`), and a cached
// `public` would then ride every dispatch of a row that is no longer `public`.
// Nil-tolerant / idempotent: forgetting an unknown or never-cached channel is a
// no-op, and a later dispatch simply read-through-fills.
func (r *ChannelRouter) ForgetChannelClassification(channelID string) {
	if r == nil {
		return
	}
	r.classifications.forget(channelID)
}

// classificationFor resolves the §A confidentiality level [dispatchTo] stamps
// onto every [DispatchEnvelope] — the "populated from the `channels` row when
// the event is dispatched" leg of RFC 0037 §B. Cache hit on every dispatch
// after a channel's first (see [classificationCache]); the miss path is a PK
// point read. Resolving here, inside dispatchTo, keeps every one of its
// callers — ordinary fanout, the floor turn, and the five orchestrator-
// authored control dispatches — covered without widening seven call sites.
//
// Fail-closed by omission: a failed row read returns "" (uncached, so a
// transient store error does not pin an unclassified wire value) with a WARN,
// and the receiver resolves an empty wire field to the `public` acting floor
// (§A rule (b) — `agents/persona_runtime/classification.py::acting_rank`),
// never `internal`. No stamp-side default is applied here: rule (a)'s
// `internal` is the STAMPING default for labeling data at rest, and stamping
// it onto a live acting-level signal would let a store hiccup widen injection
// instead of narrowing it (the PR 1 one-resolver-per-rule discipline — this
// caller resolves no rule at all, it carries the row's value or nothing).
func (r *ChannelRouter) classificationFor(ctx context.Context, channelID string) string {
	level, ok, gen := r.classifications.get(channelID)
	if ok {
		return level
	}
	ch, err := r.store.GetChannel(ctx, channelID)
	if err != nil {
		r.logger.Warn("channels: classification resolve failed at dispatch; sending unclassified (receiver floors to public)",
			zap.String("channel_id", channelID),
			zap.Error(err),
		)
		return ""
	}
	// Gen-guarded: a forget (channel delete) between the miss above and this
	// fill discards the write — see [classificationCache].
	r.classifications.fill(channelID, string(ch.Classification), gen)
	return string(ch.Classification)
}
