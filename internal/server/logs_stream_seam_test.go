// Tests for a log stream whose reader is behind (ISSUE-0223): the live
// entries that arrive while the buffered ones are still being sent wait
// in a bounded hold, only entries that pass the filters take a place in
// a stream's queue, and a stream that loses entries says so in the
// orchestrator's log.
package server

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// A client asks for buffered entries and is slow to take them. The live
// entries that arrive meanwhile, more than a subscriber's channel holds,
// all reach it afterwards, in order, and nothing is reported as lost.
func TestSSE_LiveEntriesThatArriveWhileTheBufferedOnesAreSentAllArrive(t *testing.T) {
	const live = 400
	require.Greater(t, live, logbuffer.SubscribeBuffer, "the case must overflow a subscriber's channel")
	srv, buf := testServerWithRing(t, 4096)
	logs := observed(srv)

	appendWarn(t, buf, "exec-1", "WARN", "buffered")
	st := openStalledStream(t, srv, "exec-1", "?since=5m")
	st.waitForAWrite()

	want := []string{"buffered"}
	for i := range live {
		msg := fmt.Sprintf("live-%03d", i)
		appendWarn(t, buf, "exec-1", "WARN", msg)
		want = append(want, msg)
	}
	appendWarn(t, buf, "exec-1", "WARN", "end")
	st.w.open()

	got := st.messagesUntil("end")
	require.Len(t, got, len(want)+1)
	assert.Equal(t, append(want, "end"), got)
	st.end()
	assert.Empty(t, droppedWarnings(logs), "nothing was lost, so nothing is reported")
	assert.Zero(t, buf.Stats().DroppedSubscribers)
}

// Only entries that pass the stream's filters take a place in its
// queue, so a flood of other entries cannot push out the ones the client
// asked for. With `since` (the hold) and without it (the channel).
func TestSSE_EntriesTheFilterRefusesTakeNoPlaceInTheQueue(t *testing.T) {
	const (
		noise  = 1000
		errors = 10
	)
	for _, tc := range []struct {
		name  string
		query string
	}{
		{"with since", "?since=5m&level=ERROR"},
		{"without since", "?level=ERROR"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv, buf := testServerWithRing(t, 4096)
			logs := observed(srv)

			// The first frame, buffered with `since` and live without it,
			// is the one the client is slow to take.
			var st *stalledStream
			if strings.Contains(tc.query, "since") {
				appendWarn(t, buf, "exec-1", "ERROR", "first")
				st = openStalledStream(t, srv, "exec-1", tc.query)
			} else {
				st = openStalledStream(t, srv, "exec-1", tc.query)
				require.Eventually(t, func() bool { return buf.SubscriberCount() == 1 }, gateLimit, time.Millisecond)
				appendWarn(t, buf, "exec-1", "ERROR", "first")
			}
			st.waitForAWrite()

			want := []string{"first"}
			for i := range noise {
				appendWarn(t, buf, "exec-1", "WARN", fmt.Sprintf("noise-%04d", i))
				if i%(noise/errors) == 0 {
					msg := fmt.Sprintf("error-%04d", i)
					appendWarn(t, buf, "exec-1", "ERROR", msg)
					want = append(want, msg)
				}
			}
			appendWarn(t, buf, "exec-1", "ERROR", "end")
			st.w.open()

			assert.Equal(t, append(want, "end"), st.messagesUntil("end"))
			st.end()
			assert.Empty(t, droppedWarnings(logs))
			assert.Zero(t, buf.Stats().DroppedSubscribers, "a refused entry is not a dropped one")
		})
	}
}

// The hold has a bound. A stream with `since` whose reader stays away
// for longer than the bound covers gets the entries that fitted, loses
// the newest, is live again afterwards, and the orchestrator says how
// many were lost and on which stream.
func TestSSE_TheHoldIsBoundedAndALossIsLogged(t *testing.T) {
	const lost = 7
	srv, buf := testServerWithRing(t, 4096)
	logs := observed(srv)

	appendWarn(t, buf, "exec-1", "WARN", "buffered")
	st := openStalledStream(t, srv, "exec-1", "?since=5m")
	st.waitForAWrite()

	want := []string{"buffered"}
	for i := range sseHeldEntries + lost {
		msg := fmt.Sprintf("live-%05d", i)
		appendWarn(t, buf, "exec-1", "WARN", msg)
		if i < sseHeldEntries {
			want = append(want, msg)
		}
	}
	st.w.open()
	require.Eventually(t, func() bool { return len(st.w.messages(t)) == len(want) }, gateLimit, 5*time.Millisecond,
		"the buffered entry and the %d held ones", sseHeldEntries)
	require.Eventually(t, func() bool { return len(droppedWarnings(logs)) > 0 }, gateLimit, time.Millisecond,
		"the loss is logged when the hold has been sent, not when the stream ends")

	appendWarn(t, buf, "exec-1", "WARN", "end")
	got := st.messagesUntil("end")
	require.Len(t, got, len(want)+1)
	assert.Equal(t, append(want, "end"), got)

	st.end()
	assert.Equal(t, []string{"exec-1 7/7"}, droppedWarnings(logs), "one line, with the stream and the count")
	assert.Equal(t, uint64(lost), buf.Stats().DroppedSubscribers)
}

// A stream without `since` has the channel's 256 places and no hold, as
// before. What it loses is now logged too, when the stream ends at the
// latest, and the merged view is named by its `_`.
func TestSSE_ALossOnAStreamWithoutSinceIsLogged(t *testing.T) {
	const lost = 9
	srv, buf := testServerWithRing(t, 4096)
	logs := observed(srv)

	st := openStalledStream(t, srv, "_", "")
	require.Eventually(t, func() bool { return buf.SubscriberCount() == 1 }, gateLimit, time.Millisecond)
	appendWarn(t, buf, "exec-1", "WARN", "first")
	st.waitForAWrite()

	// The handler holds "first"; the channel takes 256 more.
	want := []string{"first"}
	for i := range logbuffer.SubscribeBuffer + lost {
		msg := fmt.Sprintf("live-%03d", i)
		appendWarn(t, buf, "exec-2", "WARN", msg)
		if i < logbuffer.SubscribeBuffer {
			want = append(want, msg)
		}
	}
	st.w.open()
	assert.Equal(t, want, st.messagesUntil(want[len(want)-1]))
	assert.Empty(t, droppedWarnings(logs), "no heartbeat was due: nothing is logged yet")
	st.end()
	assert.Equal(t, []string{"_ 9/9"}, droppedWarnings(logs))
}

// A stream that stays open says what it lost at its next heartbeat, and
// each line counts the entries lost since the line before it.
func TestSSE_AnOpenStreamReportsItsLossesAtTheHeartbeat(t *testing.T) {
	srv, buf := testServerWithRing(t, 4096)
	srv.sseHeartbeat = 20 * time.Millisecond
	logs := observed(srv)
	fill := func(lost int) {
		for range logbuffer.SubscribeBuffer + lost {
			appendWarn(t, buf, "exec-1", "WARN", "live")
		}
	}

	st := openStalledStream(t, srv, "exec-1", "")
	require.Eventually(t, func() bool { return buf.SubscriberCount() == 1 }, gateLimit, time.Millisecond)
	appendWarn(t, buf, "exec-1", "WARN", "first")
	st.waitForAWrite()
	fill(3)
	// The client reads everything, then stops at the next entry.
	taken := 1 + logbuffer.SubscribeBuffer
	st.w.shutAfter(taken + 1)
	st.w.open()
	require.Eventually(t, func() bool { return len(droppedWarnings(logs)) == 1 }, gateLimit, time.Millisecond,
		"the first loss is logged while the stream is open")

	appendWarn(t, buf, "exec-1", "WARN", "taken")
	st.waitStoppedAt(taken + 1)
	appendWarn(t, buf, "exec-1", "WARN", "the handler waits to write this one")
	st.waitForAHeldWrite()
	fill(4)
	st.w.open()
	require.Eventually(t, func() bool { return len(droppedWarnings(logs)) == 2 }, gateLimit, time.Millisecond,
		"the second loss is logged at a later heartbeat")
	st.end()
	assert.Equal(t, []string{"exec-1 3/3", "exec-1 4/7"}, droppedWarnings(logs))
}

// A reader that does not catch up stays behind a hold that is full. Its
// losses are logged at the heartbeat's pace while it is still there, not
// only when the hold runs dry or the stream ends.
func TestSSE_AReaderStillBehindTheHoldIsReportedAtTheHeartbeat(t *testing.T) {
	srv, buf := testServerWithRing(t, 4096)
	srv.sseHeartbeat = 20 * time.Millisecond
	logs := observed(srv)

	appendWarn(t, buf, "exec-1", "WARN", "buffered")
	st := openStalledStream(t, srv, "exec-1", "?since=5m")
	st.waitForAWrite()
	for range sseHeldEntries + 3 {
		appendWarn(t, buf, "exec-1", "WARN", "held")
	}
	// The client takes the buffered entry and ten held ones and stops,
	// with the rest of that batch still to come. Two more entries wait in
	// the hold meanwhile, and it stops again after the first of them.
	firstStop := 1 + 10
	secondStop := 1 + sseHeldEntries + 1
	st.w.shutAfter(firstStop)
	st.w.shutAfter(secondStop)
	st.w.open()
	st.waitStoppedAt(firstStop)
	appendWarn(t, buf, "exec-1", "WARN", "more-1")
	appendWarn(t, buf, "exec-1", "WARN", "more-2")
	time.Sleep(3 * srv.sseHeartbeat) // a heartbeat falls due
	st.w.open()
	st.waitStoppedAt(secondStop)

	// The handler is in the middle of the second batch: the hold has not
	// run dry, and the loss is already in the log.
	require.Eventually(t, func() bool { return len(droppedWarnings(logs)) == 1 }, gateLimit, time.Millisecond)
	st.w.open()
	assert.Equal(t, "more-2", tail(st.messagesUntil("more-2"), 1)[0])
	st.end()
	assert.Equal(t, []string{"exec-1 3/3"}, droppedWarnings(logs))
}
