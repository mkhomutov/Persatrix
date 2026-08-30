package channels

import (
	"context"
)

// router_fanout_drain.go holds the detached-fanout drain surface — the bounded
// shutdown drain and the unbounded test barrier over the goroutines
// [ChannelRouter.PublishAsync] spawns. Split out of router_publish_async.go
// when the ISSUE-0114 per-channel cascade-depth resolve (v0.3.13) pushed that
// file past the 500-line review cap; the spawn side (PublishAsync, the
// in-flight ceiling, recoverFanout) stays there.

// DrainPendingFanout blocks until every detached fanout goroutine has completed
// OR `ctx` is done, returning true if it fully drained and false if the context
// expired first. This is the BOUNDED drain a graceful shutdown uses: a fanout
// wedged on a silent agent (under floor control, up to M×turnTimeout) must not
// hang process exit past the shutdown budget. The unbounded
// [ChannelRouter.WaitForPendingFanout] remains for tests that need a hard
// barrier. When `ctx` expires, the internal waiter goroutine outlives this call
// until the fanout eventually finishes — benign at shutdown, where the process
// exits immediately after.
//
// PR #718 review finding 1, ordering settled by the second follow-up review's
// DRAINING GATE. Waiting fanoutWG before the sweep (the first revision) closed
// the sweep-outrun race but opened the symmetric one: a synthesis timer firing
// during that first Wait ran fanoutWG.Add(1) (onSynthesisTimeout → boundedClose
// → notifyInteractionClose) from a goroutine holding NO fanoutWG count — an
// Add-from-zero concurrent with an in-progress Wait, the documented
// sync.WaitGroup misuse. The gate closes BOTH races without a fanoutWG wait
// before the sweep:
//
//  1. `draining` is set under interactionMu — the lock the arm CAS runs under
//     — so [ChannelRouter.maybeArmSynthesisClose] refuses every arm serialized
//     after it and degrades to the immediate close (deterministic termination
//     preserved; the arming fanout goroutine holds its own fanoutWG count, so
//     its close-notification Adds are legal).
//  2. The sweep is therefore FINAL: every synthesisWG.Add is under the same
//     lock and either preceded the sweep's critical section (its count is
//     registered before the Wait below starts) or finds its arm already swept
//     and never Adds. No Add-from-zero can race synthesisWG.Wait.
//  3. synthesisWG.Wait: swept timers released their counts at the sweep;
//     a timer caught mid-fire (Stop()==false) self-releases via its deferred
//     Done — deferred FIRST in onSynthesisTimeout, so its close work's
//     fanoutWG.Add(1)s happen-before that Done, which happens-before this
//     Wait returns.
//  4. One fanoutWG.Wait: by (3) every timer-originated Add is already held
//     when it starts, and every other Add came from a goroutine registered at
//     spawn (PublishAsync), holding a count (the notification fan), or holding
//     the arm's TRANSFERRED synthesisWG count (the commit-path reply claim —
//     [ChannelRouter.closeOnSynthesisReply] releases it only after its close's
//     Adds, so (3)'s happens-before covers that path too) — no Add-from-zero
//     can race it either.
//
// The flag clears on return (deferred), so a router reused after a bounded
// (ctx-expired) drain resumes arming — at real shutdown the process exits
// right after, and a straggler arm past an ABANDONED drain is the same
// accepted exposure as the outlived waiter goroutine above.
func (r *ChannelRouter) DrainPendingFanout(ctx context.Context) bool {
	r.interactionMu.Lock()
	r.draining = true
	r.interactionMu.Unlock()
	defer func() {
		r.interactionMu.Lock()
		r.draining = false
		r.interactionMu.Unlock()
	}()
	done := make(chan struct{})
	go func() {
		r.disarmAllPendingSyntheses()
		r.synthesisWG.Wait()
		r.fanoutWG.Wait()
		close(done)
	}()
	select {
	case <-done:
		return true
	case <-ctx.Done():
		return false
	}
}

// WaitForPendingFanout blocks until every fanout goroutine spawned by
// [ChannelRouter.PublishAsync] has completed. Intended for tests that need a
// deterministic point to assert on dispatched recipients — several call it
// MID-ARM and expect the pending synthesis close to survive
// (synthesis_close.go), so it deliberately takes no draining gate and runs no
// disarm sweep. A no-op when no async fanout is in flight.
//
// KNOWN EXPOSURE (accepted, test-facing): a synthesis timer that fires
// CONCURRENTLY with this wait runs its close-notification fanoutWG.Add(1)s on
// a goroutine holding no fanout count — the Add-from-zero WaitGroup misuse
// the drain's gate exists to close. Graceful shutdown must use
// [ChannelRouter.DrainPendingFanout]; a test that deliberately lets the
// timeout net fire polls the dispatcher instead of calling this
// (TestSynthesisClose_ReplyTimeoutFallsBackToImmediateClose).
func (r *ChannelRouter) WaitForPendingFanout() {
	r.fanoutWG.Wait()
}
