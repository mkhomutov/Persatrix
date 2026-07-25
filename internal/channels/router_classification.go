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
// Coherence: `channels.classification` changes through exactly one write path
// today — [ChannelStore.SetChannelClassification], whose only caller is the
// boot-time reconcile adoption step ([ChannelRouter.ReconcileConfig]), which
// runs before any dispatch and refreshes this cache via [refresh]. A future
// runtime reclassification surface (RFC 0037 §Security) MUST route through
// the router and call [refresh] — a store-direct write would serve the stale
// level here until restart. Creation paths need no hook: a channel's first
// dispatch read-through-fills after the row exists.
type classificationCache struct {
	mu     sync.RWMutex
	levels map[string]string // channel id → §A level, as stored
}

func (c *classificationCache) get(channelID string) (string, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	level, ok := c.levels[channelID]
	return level, ok
}

func (c *classificationCache) refresh(channelID, level string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.levels == nil {
		c.levels = make(map[string]string)
	}
	c.levels[channelID] = level
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
	if level, ok := r.classifications.get(channelID); ok {
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
	r.classifications.refresh(channelID, string(ch.Classification))
	return string(ch.Classification)
}
