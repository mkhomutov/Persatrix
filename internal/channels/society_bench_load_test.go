package channels

// Society-store benchmark, part 2 of 3: the layouts, the seeded store and
// the open-loop load one step drives (v0.4.0 row R2).
//
// Each agent posts on a fixed schedule whether or not the store keeps up, so
// a slow store shows as latency measured from the scheduled time, never as a
// lower offered rate. One post costs what the orchestrator spends on it: the
// router's five reads, the publish transaction with its cap prune, an
// authority read on the society tables, and one scoped history read for every
// other member, as each persona's conversation window fetches on delivery.
// One post in ten adds a recall and one in twenty holds an action for
// approval.

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"hash/fnv"
	"math/rand/v2"
	"os"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/mkhomutov/persatrix/internal/society"
)

const (
	benchHistoryLimit     = 50
	benchRecallEvery      = 10
	benchApprovalEvery    = 20
	benchTiePairEvery     = 5
	benchApprovalTTL      = 15 * time.Second
	benchGraphChangeEvery = 60 * time.Second
	benchSweepEvery       = time.Second
	benchQueuePollEvery   = 2 * time.Second
	benchMaxInFlight      = 20_000
	benchDrainTimeout     = 30 * time.Second
	benchSettledApprovals = 2_000
)

// benchLayout is who is in which channel and where each agent posts.
type benchLayout struct {
	Name     string
	Agents   []string
	Channels []string
	Members  map[string][]string // channel → members
	Posts    map[string][]string // agent → channels it posts in, in turn
}

// roomLayout is the storage roadmap's case: ten agents in one channel.
func roomLayout() benchLayout {
	l := benchLayout{Name: "room", Members: map[string][]string{}, Posts: map[string][]string{}}
	ch := "group:room-0"
	l.Channels = []string{ch}
	for i := 0; i < 10; i++ {
		a := fmt.Sprintf("agent-%02d", i)
		l.Agents = append(l.Agents, a)
		l.Members[ch] = append(l.Members[ch], a)
		l.Posts[a] = []string{ch}
	}
	return l
}

// societyLayout is fifty agents in ten channels of ten, each agent in two
// channels and posting in them in turn.
func societyLayout() benchLayout {
	l := benchLayout{Name: "society", Members: map[string][]string{}, Posts: map[string][]string{}}
	for i := 0; i < 50; i++ {
		l.Agents = append(l.Agents, fmt.Sprintf("agent-%02d", i))
	}
	for c := 0; c < 10; c++ {
		ch := fmt.Sprintf("group:team-%d", c)
		l.Channels = append(l.Channels, ch)
		for _, block := range []int{c, (c + 1) % 10} {
			for j := 0; j < 5; j++ {
				a := l.Agents[block*5+j]
				l.Members[ch] = append(l.Members[ch], a)
				l.Posts[a] = append(l.Posts[a], ch)
			}
		}
	}
	return l
}

// The corpus is made-up words whose frequencies fall off with rank as in real
// text (Zipf), led by English function words, so full-text recall matches as
// few or as many messages as it would on real conversation.
var (
	benchStopwords = strings.Fields(`the of and to a in that is for it we on with as
		this be are was at by our will can should have not from or you they their`)
	benchSyllables = strings.Fields(`ka ro mi te su na lo pe vi da ze ku ri mo sa fe
		ti bo gu le ni ha jo we ya ce du pa xi ve`)
	benchVocab = func() []string {
		words := slices.Clone(benchStopwords)
		for n := 0; len(words) < 20_000; n++ {
			var b strings.Builder
			for i, m := 0, n; i < 3; i, m = i+1, m/len(benchSyllables) {
				b.WriteString(benchSyllables[m%len(benchSyllables)])
			}
			words = append(words, b.String())
		}
		return words
	}()
)

// benchContent is a reply-sized message, 200 to 1 200 bytes of words.
func benchContent(rng *rand.Rand) string {
	zipf := rand.NewZipf(rng, 1.07, 1, uint64(len(benchVocab)-1))
	n := 200 + rng.IntN(1000)
	var b strings.Builder
	for b.Len() < n {
		b.WriteString(benchVocab[zipf.Uint64()])
		b.WriteByte(' ')
	}
	return b.String()
}

// benchQuery is a recall of two topic words, neither rare nor common.
func benchQuery(rng *rand.Rand) string {
	return benchVocab[200+rng.IntN(4800)] + " " + benchVocab[200+rng.IntN(4800)]
}

// seedTemplate builds a store as a long-running society leaves it: every
// channel at its 10 000-message cap, so each new post also prunes, and the
// society tables seeded. Seeding skips fsync; the runs do not.
func seedTemplate(ctx context.Context, path string, l benchLayout) error {
	cs, err := NewSQLiteStore(path, SQLiteOptions{MaxChannels: 100})
	if err != nil {
		return err
	}
	s := cs.(*sqliteStore)
	defer func() { _ = cs.Close() }()
	if _, err := s.db.ExecContext(ctx, `PRAGMA synchronous = OFF`); err != nil {
		return err
	}
	for _, ch := range l.Channels {
		members := make([]Member, 0, len(l.Members[ch]))
		for _, m := range l.Members[ch] {
			members = append(members, Member{ParticipantID: m, RespondPolicy: RespondAlways})
		}
		if err := cs.CreateChannelWithMembers(ctx,
			Channel{ID: ch, Name: strings.TrimPrefix(ch, "group:"), Type: ChannelTypeGroup}, members); err != nil {
			return err
		}
	}
	rng := rand.New(rand.NewPCG(1, 2))
	base := time.Now().UTC()
	for i := 0; i < DefaultMaxMessagesPerChannel; i++ {
		for c, ch := range l.Channels {
			members := l.Members[ch]
			if err := cs.PublishMessage(ctx, ChannelMessage{
				ID:        fmt.Sprintf("seed-%d-%d", c, i),
				ChannelID: ch,
				SenderID:  members[i%len(members)],
				Content:   benchContent(rng),
				Timestamp: base.Add(time.Duration(i+1) * time.Microsecond),
			}); err != nil {
				return fmt.Errorf("seed %s #%d: %w", ch, i, err)
			}
		}
	}
	st, err := society.NewSQLiteStore(s)
	if err != nil {
		return err
	}
	if err := seedSociety(ctx, st, l.Agents, benchSettledApprovals); err != nil {
		return err
	}
	// The seeded rows are what a run reads: check them before trusting a run.
	ch := l.Channels[0]
	got, err := cs.GetHistoryScoped(ctx, ch, l.Members[ch][0], benchHistoryLimit, time.Time{})
	if err != nil || len(got) != benchHistoryLimit {
		return fmt.Errorf("seed check: scoped history returned %d rows, err %v", len(got), err)
	}
	_, err = s.db.ExecContext(ctx, `PRAGMA wal_checkpoint(TRUNCATE)`)
	return err
}

// benchWorld is one step's running state.
type benchWorld struct {
	layout  benchLayout
	store   ChannelStore
	society society.Store
	rec     *benchRecorder
	runID   string
	stop    chan struct{}

	offered, published, inflight  atomic.Int64
	postSocietyReads, holdsOpened atomic.Int64
	saturated                     atomic.Bool
	lifecycles                    sync.WaitGroup
}

func (w *benchWorld) timed(class benchClass, f func() error) {
	start := time.Now()
	w.rec.record(class, start, f())
}

// post is one message's whole cost to the store.
func (w *benchWorld) post(ctx context.Context, agent, ch string, seq int, scheduled time.Time) {
	defer w.inflight.Add(-1)
	cs := w.store
	w.timed(classRouterRead, func() error { _, err := cs.GetChannel(ctx, ch); return err })
	w.timed(classRouterRead, func() error { _, _, err := cs.GetChannelConfig(ctx, ch); return err })
	for i := 0; i < 3; i++ {
		w.timed(classRouterRead, func() error { _, err := cs.GetMembers(ctx, ch); return err })
	}
	h := fnv.New64a()
	_, _ = h.Write([]byte(agent))
	rng := rand.New(rand.NewPCG(uint64(seq), h.Sum64()))
	writeStart := time.Now()
	err := cs.PublishMessage(ctx, ChannelMessage{
		ID: fmt.Sprintf("%s-%s-%d", w.runID, agent, seq), ChannelID: ch, SenderID: agent,
		Content: benchContent(rng),
	})
	w.rec.record(classPublish, writeStart, err)
	w.rec.record(classPublishE2E, scheduled, err)
	if err != nil {
		return
	}
	w.published.Add(1)
	w.postSocietyReads.Add(1)
	w.timed(classSocietyRead, func() error { return societyOrgRead(ctx, w.society, agent) })
	if seq%benchRecallEvery == 0 {
		q := benchQuery(rng)
		w.timed(classRecall, func() error {
			_, err := cs.RecallMessages(ctx, RecallParams{
				ParticipantID: agent, Query: q, ActingClassification: ClassificationInternal,
				ActingChannelID: ch, Limit: 10,
			})
			return err
		})
	}
	if seq%benchApprovalEvery == 0 {
		w.lifecycles.Add(1)
		go w.approvalLifecycle(ctx, agent, seq, rng.Uint64())
	}
	for _, m := range w.layout.Members[ch] {
		if m == agent {
			continue
		}
		w.inflight.Add(1)
		go func(member string) {
			defer w.inflight.Add(-1)
			w.timed(classHistory, func() error {
				_, err := cs.GetHistoryScoped(ctx, ch, member, benchHistoryLimit, time.Time{})
				return err
			})
		}(m)
	}
}

// approvalLifecycle holds an action and settles it the way people do: most
// approved and then spent, some denied, some left to expire. One hold in five
// opens a tie pair, whole in one transaction, so approving it withdraws its
// twin.
func (w *benchWorld) approvalLifecycle(ctx context.Context, agent string, seq int, seed uint64) {
	defer w.lifecycles.Done()
	rng := rand.New(rand.NewPCG(seed, 7))
	id := fmt.Sprintf("%s-%s-%d", w.runID, agent, seq)
	w.holdsOpened.Add(1)
	if (seq/benchApprovalEvery)%benchTiePairEvery == 0 {
		w.timed(classSocietyWrite, func() error {
			return societyOpenTieGroup(ctx, w.society, []string{id, id + "-twin"}, agent, "tie-"+id, benchApprovalTTL)
		})
	} else {
		w.timed(classSocietyWrite, func() error {
			return societyOpenApproval(ctx, w.society, id, agent, benchApprovalTTL)
		})
	}
	if !w.wait(time.Second + time.Duration(rng.IntN(9_000))*time.Millisecond) {
		return
	}
	switch r := rng.IntN(10); {
	case r < 6:
		w.timed(classSocietyWrite, func() error { return societyDecide(ctx, w.society, id, true, false) })
		if w.wait(time.Second) {
			w.timed(classSocietyWrite, func() error { return societySpend(ctx, w.society, id) })
		}
	case r < 8:
		w.timed(classSocietyWrite, func() error { return societyDecide(ctx, w.society, id, false, false) })
	}
}

// wait sleeps for d unless the step ends first; it reports whether to go on.
func (w *benchWorld) wait(d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-w.stop:
		return false
	}
}

// every runs f on a period until the step ends.
func (w *benchWorld) every(d time.Duration, f func()) {
	w.lifecycles.Add(1)
	go func() {
		defer w.lifecycles.Done()
		for w.wait(d) {
			f()
		}
	}()
}

// drive offers the step's load for its duration, then drains. Work still in
// flight at the drain deadline is abandoned: cancel stops it, and the errors
// that cancelling causes are not counted as the store's.
func (w *benchWorld) drive(ctx context.Context, cancel context.CancelFunc, step benchStep, walPath string) (walMax int64) {
	start := time.Now().Add(100 * time.Millisecond)
	end := start.Add(step.Duration)
	period := time.Duration(float64(time.Second) * float64(len(w.layout.Agents)) / step.Rate)
	var walPeak atomic.Int64
	w.every(benchSweepEvery, func() {
		w.timed(classSocietyWrite, func() error { return societyExpireSweep(ctx, w.society) })
		if fi, err := os.Stat(walPath); err == nil && fi.Size() > walPeak.Load() {
			walPeak.Store(fi.Size())
		}
	})
	w.every(benchQueuePollEvery, func() {
		w.timed(classSocietyRead, func() error { return societyListPending(ctx, w.society) })
	})
	var graphChanges atomic.Int64
	w.every(benchGraphChangeEvery, func() {
		id := fmt.Sprintf("%s-graph-%d", w.runID, graphChanges.Add(1))
		w.timed(classSocietyWrite, func() error {
			return societyOpenGraphChange(ctx, w.society, id, benchApprovalTTL)
		})
		w.timed(classSocietyWrite, func() error { return societyDecide(ctx, w.society, id, true, true) })
	})

	var schedulers sync.WaitGroup
	phases := rand.New(rand.NewPCG(3, 4))
	for _, agent := range w.layout.Agents {
		phase := time.Duration(phases.Int64N(int64(period)))
		schedulers.Add(1)
		go func(agent string) {
			defer schedulers.Done()
			for k := 0; ; k++ {
				at := start.Add(phase + time.Duration(k)*period)
				if !at.Before(end) {
					return
				}
				time.Sleep(time.Until(at))
				if w.inflight.Load() > benchMaxInFlight {
					w.saturated.Store(true)
					return
				}
				posts := w.layout.Posts[agent]
				w.offered.Add(1)
				w.inflight.Add(1)
				go w.post(ctx, agent, posts[k%len(posts)], k, at)
			}
		}(agent)
	}
	schedulers.Wait()
	close(w.stop)
	deadline := time.Now().Add(benchDrainTimeout)
	for w.inflight.Load() > 0 {
		if time.Now().After(deadline) && !w.rec.abandoned.Load() {
			w.saturated.Store(true)
			w.rec.abandoned.Store(true)
			cancel()
		}
		time.Sleep(10 * time.Millisecond)
	}
	w.lifecycles.Wait()
	return walPeak.Load()
}

// serviceTimes is each call's median time with nothing else running.
func serviceTimes(ctx context.Context, template, scratch string, l benchLayout) (map[string]float64, error) {
	if err := copyBenchFile(template, scratch); err != nil {
		return nil, err
	}
	defer func() { _ = os.Remove(scratch) }()
	cs, err := NewSQLiteStore(scratch, SQLiteOptions{MaxChannels: 100})
	if err != nil {
		return nil, err
	}
	defer func() { _ = cs.Close() }()
	st, err := society.NewSQLiteStore(cs.(*sqliteStore))
	if err != nil {
		return nil, err
	}
	ch := l.Channels[0]
	member := func(i int) string { return l.Members[ch][i%len(l.Members[ch])] }
	rng := rand.New(rand.NewPCG(5, 6))
	calls := []struct {
		name string
		f    func(i int) error
	}{
		{"router_read GetMembers", func(int) error { _, err := cs.GetMembers(ctx, ch); return err }},
		{"history GetHistoryScoped(50)", func(i int) error {
			_, err := cs.GetHistoryScoped(ctx, ch, member(i), benchHistoryLimit, time.Time{})
			return err
		}},
		{"recall RecallMessages", func(i int) error {
			_, err := cs.RecallMessages(ctx, RecallParams{ParticipantID: member(i),
				Query:                benchQuery(rng),
				ActingClassification: ClassificationInternal, ActingChannelID: ch, Limit: 10})
			return err
		}},
		{"publish PublishMessage at cap", func(i int) error {
			return cs.PublishMessage(ctx, ChannelMessage{ID: fmt.Sprintf("svc-%d", i), ChannelID: ch,
				SenderID: member(i), Content: benchContent(rng)})
		}},
		{"society_read org read", func(i int) error { return societyOrgRead(ctx, st, member(i)) }},
		{"society_write hold", func(i int) error {
			return societyOpenApproval(ctx, st, fmt.Sprintf("svc-%d", i), member(i), benchApprovalTTL)
		}},
	}
	out := map[string]float64{}
	for _, c := range calls {
		var d []time.Duration
		for i := 0; i < 25; i++ {
			start := time.Now()
			if err := c.f(i); err != nil {
				return nil, fmt.Errorf("%s: %w", c.name, err)
			}
			d = append(d, time.Since(start))
		}
		slices.Sort(d)
		out[c.name] = ms(d[len(d)/2])
	}
	return out, nil
}

// benchStorePath is the path a step opens its channel store at. The
// immediate form begins every write transaction on both connections as
// IMMEDIATE, under which busy_timeout applies to the write lock (plan S-2).
func benchStorePath(path, form string) string {
	if form == "immediate" {
		return path + "?_txlock=immediate"
	}
	return path
}

// benchConn hands the society store a connection the channel store did not
// make, for the forms that measure a second connection.
type benchConn struct{ db *sql.DB }

func (c benchConn) SocietyConn() *sql.DB { return c.db }

// openSociety returns the society store the society calls use: on the
// channel store's own single connection, or on a second connection to the
// same file, its transactions begun as the channel store's are.
func openSociety(cs ChannelStore, path, form string) (st society.Store, closeFn func() error, err error) {
	switch form {
	case "shared":
		st, err := society.NewSQLiteStore(cs.(*sqliteStore))
		return st, func() error { return nil }, err
	case "separate", "immediate":
		db, err := sql.Open("sqlite", buildDSN(benchStorePath(path, form)))
		if err != nil {
			return nil, nil, err
		}
		db.SetMaxOpenConns(1)
		st, err := society.NewSQLiteStore(benchConn{db})
		if err != nil {
			_ = db.Close()
			return nil, nil, err
		}
		return st, db.Close, nil
	}
	return nil, nil, errors.New("unknown society form " + form)
}
