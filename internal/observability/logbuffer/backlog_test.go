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
			backlog, ch, cancel, err := b.SubscribeWithBacklog(tc.id)
			require.NoError(t, err)
			defer cancel()
			assert.Equal(t, tc.want, messages(backlog))
			if tc.id != "" {
				assert.Equal(t, messages(b.Snapshot(tc.id)), messages(backlog), "the backlog is Snapshot's answer")
			}
			assert.Empty(t, ch, "an entry already buffered must not also be on the channel")

			// The channel is live from here on, with Subscribe's routing.
			live := "warm-b"
			if tc.id != "" {
				live = tc.id
			}
			require.Equal(t, DropNone, b.Append(mkEntry(live, "INFO", "live-"+tc.name)))
			require.Equal(t, DropNone, b.Append(mkEntry("warm-a", "INFO", "elsewhere")))
			select {
			case e := <-ch:
				assert.Equal(t, "live-"+tc.name, e.Message)
			case <-time.After(time.Second):
				require.FailNow(t, "the live entry did not arrive")
			}
			if tc.id == "" {
				assert.Equal(t, "elsewhere", (<-ch).Message)
			}
			assert.Empty(t, ch, "an entry for another execution must not arrive")
		})
	}
}

func TestSubscribeWithBacklog_RefusesAsSubscribeDoes(t *testing.T) {
	b := newTestBuffer(t, Config{MaxSubscribers: 1})
	require.Equal(t, DropNone, b.Append(mkEntry("exec-1", "INFO", "m")))

	for _, id := range []string{"../etc", "a/b", "_", strings.Repeat("x", 129)} {
		backlog, ch, cancel, err := b.SubscribeWithBacklog(id)
		require.ErrorIs(t, err, ErrInvalidExecutionID, "id %q", id)
		assert.Nil(t, backlog)
		assert.Nil(t, ch)
		assert.Nil(t, cancel)
	}
	assert.Zero(t, b.SubscriberCount(), "a refused id must not take a slot")

	_, _, cancel, err := b.SubscribeWithBacklog("exec-1")
	require.NoError(t, err)
	backlog, ch, second, err := b.SubscribeWithBacklog("exec-1")
	require.ErrorIs(t, err, ErrSubscriberCapExceeded)
	assert.Nil(t, backlog, "a refused subscriber gets no entries")
	assert.Nil(t, ch)
	assert.Nil(t, second)
	assert.Equal(t, 1, b.SubscriberCount())

	cancel()
	cancel() // idempotent
	assert.Zero(t, b.SubscriberCount(), "cancel frees the slot")
	_, ch, cancel, err = b.SubscribeWithBacklog("exec-1")
	require.NoError(t, err)
	cancel()
	select {
	case _, open := <-ch:
		assert.False(t, open, "cancel closes the channel")
	case <-time.After(time.Second):
		require.FailNow(t, "cancel did not close the channel")
	}
}

// Writers append without a pause while subscribers register. For every
// subscriber and every writer, the writer's entries in the backlog are an
// unbroken run and its first entry on the channel is the very next one:
// nothing is missing and nothing is there twice where the two halves
// meet. Half the subscribers take one execution, half take all of them.
// The test goes on until it has checked enough meetings, so that it
// proves the same on one CPU as on eight.
func TestSubscribeWithBacklog_MeetsTheLiveEntriesExactly(t *testing.T) {
	const (
		executions   = 4
		writersPerEx = 2
		meetings     = 600 // writers to catch with entries on both sides
		tooFew       = 200 // fewer than this proves nothing
	)
	b := newTestBuffer(t, Config{PerExecution: 512, MaxExecutions: executions, MaxSubscribers: 8})

	var stop atomic.Bool
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
		backlog, ch, cancel, err := b.SubscribeWithBacklog(id)
		require.NoError(t, err)

		last := map[string]int{}
		for _, e := range backlog {
			writer, seq := parse(e)
			if prev, seen := last[writer]; seen {
				require.Equal(t, prev+1, seq, "subscription %d: the backlog of %s is not an unbroken run", i, writer)
			}
			last[writer] = seq
		}
		// The first SubscribeBuffer entries a channel takes are the first
		// ones broadcast to it, whether or not it overflows later.
		firstLive := map[string]bool{}
		for range SubscribeBuffer {
			var e Entry
			select {
			case e = <-ch:
			case <-time.After(2 * time.Second):
				require.FailNow(t, "the writers stopped")
			}
			writer, seq := parse(e)
			if firstLive[writer] {
				continue
			}
			firstLive[writer] = true
			prev, seen := last[writer]
			if !seen {
				// The ring had wrapped past this writer's entries, or it
				// had not written yet: nothing to meet.
				continue
			}
			straddled++
			require.Equal(t, prev+1, seq,
				"subscription %d: %s ends at %d in the backlog and starts at %d on the channel", i, writer, prev, seq)
		}
		cancel()
	}
	require.GreaterOrEqual(t, straddled, tooFew, "too few writers were caught on both sides to prove anything")
}
