package channels

// live_delivery.go — the per-recipient live-delivery failure ledger for one
// floor-path fanout (PR #718 review). The floor-path bounded close stamps its
// notification `close_notification_redelivery=true` because the bounding
// stimulus was already delivered live inside the round — but delivery is
// PER-RECIPIENT (a per-recipient dispatch timeout/error is warn-only,
// fire-and-forget by contract), while the marker was per-close: a member whose
// live dispatch failed then skipped the notification ingest too
// (close_notification.py's redelivery skip), and the closing turn vanished
// from its record entirely — pre-4b-ii the notification re-ingest doubled as
// that member's delivery repair, at the cost of a duplicate turn for everyone
// else. The round collects its failures here and the close fan downgrades
// exactly those members' notifications to sole delivery (redelivery=false),
// so the ingest-skip only ever applies to a member the live round actually
// reached. The concurrent path never needs one: its close-before-dispatch
// ordering makes the notification the sole delivery for every member.

import "sync"

// liveDeliveryFailures collects the member ids whose live dispatch of the
// bounding stimulus failed during one floor-path fanout. Concurrency-safe —
// the non-responder ingestion deliveries run on [ChannelRouter.dispatchConcurrent]
// worker goroutines. All methods are nil-tolerant so the concurrent fanout
// path can pass nil.
type liveDeliveryFailures struct {
	mu  sync.Mutex
	ids map[string]struct{}
}

// record notes one member whose live dispatch of the stimulus failed.
func (f *liveDeliveryFailures) record(participantID string) {
	if f == nil {
		return
	}
	f.mu.Lock()
	if f.ids == nil {
		f.ids = make(map[string]struct{}, 1)
	}
	f.ids[participantID] = struct{}{}
	f.mu.Unlock()
}

// snapshot returns the failed-member set, nil when every dispatch landed.
// Called once at the round's end — after every recording dispatch has
// returned (dispatchConcurrent blocks on its workers; the floor turns are
// serial) — so the returned map is never written again and is safe to hand
// to the detached close-notification fan.
func (f *liveDeliveryFailures) snapshot() map[string]struct{} {
	if f == nil {
		return nil
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.ids) == 0 {
		return nil
	}
	return f.ids
}
