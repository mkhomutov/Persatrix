package security

import "time"

// withTickerSeam is an unexported test-only [AuditLoggerOption] that
// replaces the real `time.NewTicker(batchInterval).C` channel with a
// caller-supplied tick chan and signals each ticker-driven flush on
// flushed.
//
// Lives in a *_test.go file so production builds cannot accidentally
// reach the seam. Used by [TestBatchFlush_TickerInjected_FlushesOnTick]
// and friends (PR #233 review Should-Fix #1) to make ticker behaviour
// deterministic without depending on wall-clock progress or
// platform-specific timer resolution (~15 ms on Windows).
func withTickerSeam(tick <-chan time.Time, flushed chan<- struct{}) AuditLoggerOption {
	return func(l *fileAuditLogger) {
		l.testTickC = tick
		l.testFlushSignal = flushed
	}
}
