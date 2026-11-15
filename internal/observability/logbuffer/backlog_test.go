package logbuffer

import (
	"fmt"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap/zaptest"
)

func messages(entries []Entry) []string {
	out := make([]string, 0, len(entries))
	for _, e := range entries {
		out = append(out, e.ExecutionID+":"+e.Message)
	}
	sort.Strings(out)
	return out
}

// testHold is a hold no test here fills unless it means to.
const testHold = 1 << 16

// receive takes the next entry from ch, failing the test if none comes.
func receive(t *testing.T, ch <-chan Entry) Entry {
	t.Helper()
	select {
	case e, open := <-ch:
		require.True(t, open, "the channel was closed")
		return e
	case <-time.After(2 * time.Second):
		require.FailNow(t, "no entry arrived on the channel")
		return Entry{}
	}
}

// The backlog is what Snapshot returns for the execution, wherever its
// entries live, and for "" it is every execution's, each entry once.
func TestSubscribeWithBacklog_ReturnsTheBufferedEntries(t *testing.T) {
	// restarted returns a buffer whose first process sealed three
	// executions: it warm-loads the newest two (MaxExecutions) and leaves
	// the oldest on disk only.
	restarted := func(t *testing.T) *Buffer {
		dir := t.TempDir()
		logger := zaptest.NewLogger(t)
		cfg := Config{Dir: dir, PerExecution: 4, MaxExecutions: 2, RatePerExec: 100_000}
		b1, err := New(cfg, logger)
		require.NoError(t, err)
		for i, id := range []string{"on-disk", "warm-a", "warm-b"} {
			require.Equal(t, DropNone, b1.Append(mkEntry(id, "INFO", "sealed")))
			require.NoError(t, b1.Seal(id))
			// Seconds apart, so the on-disk order does not depend on the
			// file system's timestamp resolution.
			stamp := time.Now().Add(time.Duration(i-10) * time.Second)
			require.NoError(t, chTimes(dir+"/"+id, stamp))
		}
		require.NoError(t, b1.Close())

		b, err := New(cfg, logger)
		require.NoError(t, err)
		t.Cleanup(func() { _ = b.Close() })
		// A late entry for a warm-loaded execution: in its ring, not yet on disk.
		require.Equal(t, DropNone, b.Append(mkEntry("warm-b", "INFO", "late")))
		require.Equal(t, 2, b.Stats().ActiveRings, "the oldest execution must be on disk only")
		return b
	}

	cases := []struct {
		name string
		id   string
		want []string
	}{
		{"an execution with a ring in memory", "warm-b", []string{"warm-b:late", "warm-b:sealed"}},
		{"an execution on disk only", "on-disk", []string{"on-disk:sealed"}},
		{"an execution nobody has logged for", "never-seen", []string{}},
		{"every execution, each entry once", "", []string{
			"on-disk:sealed", "warm-a:sealed", "warm-b:late", "warm-b:sealed",
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			b := restarted(t)
			sub, err := b.SubscribeWithBacklog(tc.id, nil, testHold)
			require.NoError(t, err)
			defer sub.Cancel()
			assert.Equal(t, tc.want, messages(sub.Backlog))
			if tc.id != "" {
				assert.Equal(t, messages(b.Snapshot(tc.id)), messages(sub.Backlog), "the backlog is Snapshot's answer")
			}
			assert.Empty(t, sub.Held(), "an entry already buffered must not also be live")
			assert.Empty(t, sub.Live)

			// The channel is live from here on, with Subscribe's routing.
			live := "warm-b"
			if tc.id != "" {
				live = tc.id
			}
			require.Equal(t, DropNone, b.Append(mkEntry(live, "INFO", "live-"+tc.name)))
			require.Equal(t, DropNone, b.Append(mkEntry("warm-a", "INFO", "elsewhere")))
			assert.Equal(t, "live-"+tc.name, receive(t, sub.Live).Message)
			if tc.id == "" {
				assert.Equal(t, "elsewhere", receive(t, sub.Live).Message)
			}
			assert.Empty(t, sub.Live, "an entry for another execution must not arrive")
		})
	}
}

func TestSubscribeWithBacklog_RefusesAsSubscribeDoes(t *testing.T) {
	b := newTestBuffer(t, Config{MaxSubscribers: 1})
	require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "INFO", "m")))

	for _, id := range []string{"../etc", "a/b", "_", strings.Repeat("x", 129)} {
		sub, err := b.SubscribeWithBacklog(id, nil, testHold)
		require.ErrorIs(t, err, ErrInvalidExecutionID, "id %q", id)
		assert.Nil(t, sub)
		sub, err = b.SubscribeFiltered(id, nil)
		require.ErrorIs(t, err, ErrInvalidExecutionID, "id %q", id)
		assert.Nil(t, sub)
	}
	assert.Zero(t, b.SubscriberCount(), "a refused id must not take a slot")

	first, err := b.SubscribeWithBacklog("exec-1", nil, testHold)
	require.NoError(t, err)
	second, err := b.SubscribeWithBacklog("exec-1", nil, testHold)
	require.ErrorIs(t, err, ErrSubscriberCapExceeded)
	assert.Nil(t, second, "a refused subscriber gets no entries")
	second, err = b.SubscribeFiltered("exec-1", nil)
	require.ErrorIs(t, err, ErrSubscriberCapExceeded)
	assert.Nil(t, second)
	assert.Equal(t, 1, b.SubscriberCount())

	first.Cancel()
	first.Cancel() // idempotent
	assert.Zero(t, b.SubscriberCount(), "cancel frees the slot")
	for _, subscribe := range []func() (*Subscription, error){
		func() (*Subscription, error) { return b.SubscribeWithBacklog("exec-1", nil, testHold) },
		func() (*Subscription, error) { return b.SubscribeFiltered("exec-1", nil) },
	} {
		sub, err := subscribe()
		require.NoError(t, err)
		sub.Cancel()
		select {
		case _, open := <-sub.Live:
			assert.False(t, open, "cancel closes the channel")
		case <-time.After(time.Second):
			require.FailNow(t, "cancel did not close the channel")
		}
		assert.Zero(t, b.SubscriberCount())
	}
}

// The live entries of a subscription with a backlog wait in its hold,
// in order, however many the channel could take, until Held has run dry;
// from then on they arrive on the channel.
func TestSubscribeWithBacklog_HoldsTheLiveEntriesUntilHeldRunsDry(t *testing.T) {
	b := newTestBuffer(t, Config{PerExecution: 4096})
	require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "WARN", "buffered")))
	sub, err := b.SubscribeWithBacklog("exec-1", nil, testHold)
	require.NoError(t, err)
	defer sub.Cancel()
	require.Equal(t, []string{"exec-1:buffered"}, messages(sub.Backlog))

	inOrder := func(from, to int) []string {
		var out []string
		for i := from; i < to; i++ {
			out = append(out, fmt.Sprintf("live-%04d", i))
		}
		return out
	}
	text := func(entries []Entry) []string {
		var out []string
		for _, e := range entries {
			out = append(out, e.Message)
		}
		return out
	}
	appendLive := func(from, to int) {
		for _, msg := range inOrder(from, to) {
			require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "WARN", msg)))
		}
	}

	// Three times what the channel holds: all of it waits, none is lost.
	first := 3 * SubscribeBuffer
	appendLive(0, first)
	assert.Empty(t, sub.Live, "a held entry must not also be on the channel")
	assert.Equal(t, inOrder(0, first), text(sub.Held()))

	// More arrived while the caller was sending those: still held.
	appendLive(first, first+5)
	assert.Empty(t, sub.Live)
	assert.Equal(t, inOrder(first, first+5), text(sub.Held()))

	// Nothing waits: the hold ends here, and stays ended.
	assert.Nil(t, sub.Held())
	appendLive(first+5, first+8)
	assert.Nil(t, sub.Held(), "after the hold an entry goes to the channel")
	for _, want := range inOrder(first+5, first+8) {
		assert.Equal(t, want, receive(t, sub.Live).Message)
	}
	assert.Zero(t, sub.Dropped())
	assert.Zero(t, b.Stats().DroppedSubscribers)
}

// The hold takes as many entries as the caller asked for and no more:
// the ones that arrive past it are lost, and counted for the subscriber
// and for the buffer.
func TestSubscribeWithBacklog_TheHoldIsBounded(t *testing.T) {
	const hold = 7
	b := newTestBuffer(t, Config{})
	sub, err := b.SubscribeWithBacklog("exec-1", nil, hold)
	require.NoError(t, err)
	defer sub.Cancel()
	other, err := b.SubscribeWithBacklog("exec-1", nil, testHold)
	require.NoError(t, err)
	defer other.Cancel()

	for i := range hold + 3 {
		require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "WARN", strconv.Itoa(i))))
	}
	held := sub.Held()
	require.Len(t, held, hold)
	assert.Equal(t, "0", held[0].Message)
	assert.Equal(t, strconv.Itoa(hold-1), held[hold-1].Message, "the newest are the ones lost")
	assert.Equal(t, uint64(3), sub.Dropped())
	assert.Zero(t, other.Dropped(), "another subscriber's losses are not this one's")
	assert.Equal(t, uint64(3), b.Stats().DroppedSubscribers)
	assert.Len(t, other.Held(), hold+3)

	// Room again once the caller has taken what waited.
	require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "WARN", "after")))
	held = sub.Held()
	require.Len(t, held, 1)
	assert.Equal(t, "after", held[0].Message)
	assert.Equal(t, uint64(3), sub.Dropped())
}

// keep is asked before an entry takes a place: one it refuses is neither
// queued nor counted as lost, in the hold and on the channel alike.
func TestSubscribe_KeepIsAskedBeforeAnEntryIsQueued(t *testing.T) {
	onlyErrors := func(e Entry) bool { return e.Level == "ERROR" }
	flood := func(t *testing.T, b *Buffer) []string {
		t.Helper()
		var want []string
		for i := range 4 * SubscribeBuffer {
			require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "WARN", "noise")))
			if i%SubscribeBuffer == 0 {
				msg := "error-" + strconv.Itoa(i)
				require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "ERROR", msg)))
				want = append(want, msg)
			}
		}
		return want
	}

	t.Run("in the hold", func(t *testing.T) {
		b := newTestBuffer(t, Config{})
		// A hold with room for the wanted entries only.
		sub, err := b.SubscribeWithBacklog("exec-1", onlyErrors, 4)
		require.NoError(t, err)
		defer sub.Cancel()
		want := flood(t, b)
		var got []string
		for _, e := range sub.Held() {
			got = append(got, e.Message)
		}
		assert.Equal(t, want, got)
		assert.Zero(t, sub.Dropped(), "a refused entry is not a lost one")
		assert.Zero(t, b.Stats().DroppedSubscribers)
	})
	t.Run("on the channel", func(t *testing.T) {
		b := newTestBuffer(t, Config{})
		sub, err := b.SubscribeFiltered("exec-1", onlyErrors)
		require.NoError(t, err)
		defer sub.Cancel()
		assert.Nil(t, sub.Backlog)
		assert.Nil(t, sub.Held(), "a subscription with no backlog never holds")
		want := flood(t, b)
		for _, msg := range want {
			assert.Equal(t, msg, receive(t, sub.Live).Message)
		}
		assert.Empty(t, sub.Live)
		assert.Zero(t, sub.Dropped())
	})
	t.Run("a nil keep keeps every entry, as Subscribe", func(t *testing.T) {
		b := newTestBuffer(t, Config{})
		sub, err := b.SubscribeFiltered("exec-1", nil)
		require.NoError(t, err)
		defer sub.Cancel()
		flood(t, b)
		assert.Len(t, sub.Live, SubscribeBuffer, "the channel is full")
		assert.Positive(t, sub.Dropped(), "and what did not fit is counted")
		assert.Equal(t, sub.Dropped(), b.Stats().DroppedSubscribers)
	})
}

// Writers append without a pause while subscribers register. For every
// subscriber and every writer, the writer's entries in the backlog are an
// unbroken run and its first live entry is the very next one: nothing is
// missing and nothing is there twice where the two halves meet. The live
// entries are read as a caller reads them, the hold first and then the
// channel, and they too are an unbroken run across the two. Half the
// subscribers take one execution, half take all of them.
//
// The test goes on until it has checked enough meetings. That keeps its
// length steady from one CPU to eight; it does not make it equally sharp.
// A broken seam shows only when a writer is stopped between its ring
// write and its fan-out, which on one CPU is up to the scheduler: with
// the seam broken on purpose, in three ways, the test failed in 30 of 36
// runs on one CPU and in every run on two, four and eight.
func TestSubscribeWithBacklog_MeetsTheLiveEntriesExactly(t *testing.T) {
	const (
		executions   = 4
		writersPerEx = 2
		meetings     = 600 // writers to catch with entries on both sides
		tooFew       = 200 // fewer than this proves nothing
	)
	b := newTestBuffer(t, Config{PerExecution: 512, MaxExecutions: executions, MaxSubscribers: 8})

	// While a subscriber empties its hold the writers slow down, so that
	// the hold can run dry; they do not stop, so entries still arrive
	// while it ends and the channel takes over.
	var stop, slow atomic.Bool
	var writers sync.WaitGroup
	for ex := range executions {
		for w := range writersPerEx {
			writers.Add(1)
			go func() {
				defer writers.Done()
				id := "exec-" + strconv.Itoa(ex)
				for seq := 0; !stop.Load(); seq++ {
					// WARN is never rate-limited, so every entry is admitted.
					if b.Append(mkEntry(id, "WARN", fmt.Sprintf("%d/%d", w, seq))) != DropNone {
						panic("an entry was not admitted")
					}
					if slow.Load() {
						time.Sleep(50 * time.Microsecond)
						continue
					}
					// Lets the subscriber run when there is one CPU; with
					// more, the writers still run beside it.
					runtime.Gosched()
				}
			}()
		}
	}
	defer func() {
		stop.Store(true)
		writers.Wait()
	}()

	// parse returns the writer of an entry, as "execution/writer", and
	// its sequence number.
	parse := func(e Entry) (string, int) {
		writer, seq, ok := strings.Cut(e.Message, "/")
		require.True(t, ok, "message %q", e.Message)
		n, err := strconv.Atoi(seq)
		require.NoError(t, err)
		return e.ExecutionID + "/" + writer, n
	}

	var straddled int
	deadline := time.Now().Add(20 * time.Second)
	for i := 0; straddled < meetings && time.Now().Before(deadline); i++ {
		id := ""
		if i%2 == 0 {
			id = "exec-" + strconv.Itoa(i%executions)
		}
		slow.Store(false)
		sub, err := b.SubscribeWithBacklog(id, nil, testHold)
		require.NoError(t, err)
		slow.Store(true)

		last := map[string]int{}
		for _, e := range sub.Backlog {
			writer, seq := parse(e)
			if prev, seen := last[writer]; seen {
				require.Equal(t, prev+1, seq, "subscription %d: the backlog of %s is not an unbroken run", i, writer)
			}
			last[writer] = seq
		}
		// What waited in the hold, until a call finds it empty, and then
		// the first entries of the channel: every one of them was taken,
		// so each writer's entries go on one by one from its backlog.
		live := map[string]bool{}
		follow := func(e Entry, from string) {
			writer, seq := parse(e)
			prev, seen := last[writer]
			last[writer] = seq
			if !seen {
				// The ring had wrapped past this writer's entries, or it
				// had not written yet: nothing to meet.
				live[writer] = true
				return
			}
			if !live[writer] {
				live[writer] = true
				straddled++
			}
			require.Equal(t, prev+1, seq,
				"subscription %d: %s goes from %d to %d %s", i, writer, prev, seq, from)
		}
		for held := sub.Held(); len(held) > 0; held = sub.Held() {
			for _, e := range held {
				follow(e, "in the hold")
			}
		}
		// The channel was empty when the hold ended and takes
		// SubscribeBuffer entries before it can overflow.
		for range SubscribeBuffer {
			follow(receive(t, sub.Live), "on the channel")
		}
		require.Zero(t, sub.Dropped(), "subscription %d", i)
		sub.Cancel()
	}
	require.GreaterOrEqual(t, straddled, tooFew, "too few writers were caught on both sides to prove anything")
}
