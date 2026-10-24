package channels

// Society-store benchmark, part 3 of 3: the entry point, the pass criteria
// and the report (v0.4.0 row R2). docs/storage-architecture-benchmark.md
// gives the method, the commands and the recorded results.
//
// It runs only when asked, so CI skips it:
//
//	go test ./internal/channels -run '^TestSocietyStoreBenchmark$' -count=1 \
//	    -timeout 0 -v -args -society-bench=gate -society-bench-out=<dir>

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

var (
	societyBench = flag.String("society-bench", "",
		"run the society-store benchmark: smoke, gate or ramp (empty skips it)")
	societyBenchOut = flag.String("society-bench-out", "",
		"directory the JSON results are written to (default: none)")
	societyBenchDir = flag.String("society-bench-dir", "",
		"directory for the store files (default: a test temp dir)")
	societyBenchForms = flag.String("society-bench-forms", "shared,separate",
		"society connection forms to run, comma-separated")
	societyBenchDuration = flag.Duration("society-bench-duration", 0,
		"override each step's duration")
)

// The pass criteria, fixed before the first run. A persona's turn takes
// seconds of model time; a store that adds at most a tenth of a second at the
// 99th percentile, loses nothing and never fails a call is not what limits a
// society.
const (
	benchMaxP99         = 100 * time.Millisecond
	benchMaxPublishWait = time.Second
	benchMinDelivered   = 0.99
)

type benchClass string

const (
	classPublishE2E   benchClass = "publish_e2e"   // scheduled time → committed
	classPublish      benchClass = "publish"       // the publish transaction alone
	classRouterRead   benchClass = "router_read"   // channel, config and members reads
	classHistory      benchClass = "history"       // a recipient's scoped window read
	classRecall       benchClass = "recall"        // full-text recall
	classSocietyRead  benchClass = "society_read"  // authority read, approval-queue page
	classSocietyWrite benchClass = "society_write" // hold, decide, spend, sweep, graph change
)

var benchClasses = []benchClass{classPublishE2E, classPublish, classRouterRead, classHistory,
	classRecall, classSocietyRead, classSocietyWrite}

type benchRecorder struct {
	mu        sync.Mutex
	samples   map[benchClass][]time.Duration
	errs      map[benchClass]int
	errText   map[string]int
	lostRaces int
	cancelled int
	abandoned atomic.Bool // set when the drain deadline cancels the step
}

func newBenchRecorder() *benchRecorder {
	return &benchRecorder{samples: map[benchClass][]time.Duration{},
		errs: map[benchClass]int{}, errText: map[string]int{}}
}

func (r *benchRecorder) record(class benchClass, start time.Time, err error) {
	d := time.Since(start)
	r.mu.Lock()
	defer r.mu.Unlock()
	r.samples[class] = append(r.samples[class], d)
	switch {
	case err == nil:
	case errors.Is(err, errBenchNoRow):
		r.lostRaces++
	case r.abandoned.Load():
		r.cancelled++
	default:
		r.errs[class]++
		key := string(class) + ": " + err.Error()
		if len(r.errText) < 20 || r.errText[key] > 0 {
			r.errText[key]++
		}
	}
}

type benchStats struct {
	Count  int     `json:"count"`
	Errors int     `json:"errors"`
	P50ms  float64 `json:"p50_ms"`
	P95ms  float64 `json:"p95_ms"`
	P99ms  float64 `json:"p99_ms"`
	MaxMs  float64 `json:"max_ms"`
}

func ms(d time.Duration) float64 { return math.Round(float64(d)/1e4) / 100 }

// nearestRank is the p-th percentile by nearest rank on sorted samples.
func nearestRank(sorted []time.Duration, p float64) time.Duration {
	if len(sorted) == 0 {
		return 0
	}
	return sorted[max(0, int(math.Ceil(p*float64(len(sorted))))-1)]
}

type benchStep struct {
	Layout   string        `json:"layout"`
	Form     string        `json:"form"`
	Rate     float64       `json:"rate_msgs_per_s"`
	Duration time.Duration `json:"-"`
}

type benchResult struct {
	benchStep
	Seconds    float64                   `json:"seconds"`
	Offered    int64                     `json:"offered"`
	Published  int64                     `json:"published"`
	Saturated  bool                      `json:"saturated"`
	LostRaces  int                       `json:"lost_races"`
	Cancelled  int                       `json:"cancelled_at_drain"`
	WALPeakMB  float64                   `json:"wal_peak_mb"`
	DBSizeMB   float64                   `json:"db_size_mb"`
	Stats      map[benchClass]benchStats `json:"stats"`
	ErrorTexts map[string]int            `json:"error_texts,omitempty"`
	Pass       bool                      `json:"pass"`
	Failures   []string                  `json:"failures,omitempty"`
	// The society classes also hold the sweep, the queue page and the graph
	// change, which run whatever the posts do, so the posts' own society
	// calls are counted apart.
	PostSocietyReads int64 `json:"post_society_reads"`
	HoldsOpened      int64 `json:"holds_opened"`
}

// judge applies the pass criteria.
func (r *benchResult) judge() {
	if r.Saturated {
		r.Failures = append(r.Failures, "saturated: in-flight work did not drain")
	}
	if r.Offered > 0 && float64(r.Published) < benchMinDelivered*float64(r.Offered) {
		r.Failures = append(r.Failures, fmt.Sprintf("published %d of %d offered", r.Published, r.Offered))
	}
	for _, c := range benchClasses {
		s := r.Stats[c]
		if s.Count == 0 {
			r.Failures = append(r.Failures, fmt.Sprintf("%s: no calls made", c))
		}
		if s.Errors > 0 {
			r.Failures = append(r.Failures, fmt.Sprintf("%s: %d errors", c, s.Errors))
		}
		if s.P99ms > ms(benchMaxP99) {
			r.Failures = append(r.Failures, fmt.Sprintf("%s: p99 %.1f ms", c, s.P99ms))
		}
	}
	if m := r.Stats[classPublishE2E].MaxMs; m > ms(benchMaxPublishWait) {
		r.Failures = append(r.Failures, fmt.Sprintf("publish_e2e: max %.1f ms", m))
	}
	if r.PostSocietyReads == 0 || r.HoldsOpened == 0 {
		r.Failures = append(r.Failures, fmt.Sprintf("posts made %d authority reads and opened %d holds",
			r.PostSocietyReads, r.HoldsOpened))
	}
	// A decide or spend waits at most 11 s on a 15 s expiry, so a healthy
	// store loses no race; one that matches nothing must not pass.
	if r.LostRaces > 0 {
		r.Failures = append(r.Failures, fmt.Sprintf("%d lost races", r.LostRaces))
	}
	r.Pass = len(r.Failures) == 0
}

func benchPlan(plan string, forms []string, override time.Duration) []benchStep {
	var steps []benchStep
	add := func(rate float64, d time.Duration) {
		if override > 0 {
			d = override
		}
		for _, layout := range []string{"room", "society"} {
			for _, form := range forms {
				steps = append(steps, benchStep{Layout: layout, Form: form, Rate: rate, Duration: d})
			}
		}
	}
	switch plan {
	case "smoke":
		add(10, 10*time.Second)
	case "gate":
		add(10, 5*time.Minute)
	case "ramp":
		for _, rate := range []float64{20, 50, 100, 200, 400} {
			add(rate, time.Minute)
		}
	}
	return steps
}

func TestSocietyStoreBenchmark(t *testing.T) {
	if *societyBench == "" {
		t.Skip("society-store benchmark: pass -society-bench=smoke|gate|ramp to run it")
	}
	// Checked before the minutes of seeding, so a typo fails at once.
	forms, err := benchList("-society-bench-forms", *societyBenchForms, "shared", "separate")
	if err != nil {
		t.Fatal(err)
	}
	steps := benchPlan(*societyBench, forms, *societyBenchDuration)
	if len(steps) == 0 {
		t.Fatalf("unknown -society-bench plan %q", *societyBench)
	}
	var outPath string
	if *societyBenchOut != "" {
		if err := os.MkdirAll(*societyBenchOut, 0o755); err != nil {
			t.Fatal(err)
		}
		// Named for what ran, so a narrowed run does not overwrite a wider one.
		outPath = filepath.Join(*societyBenchOut, fmt.Sprintf("society-bench-%s-%s-%s-%s.json",
			*societyBench, strings.Join(forms, "+"), runtime.GOOS, runtime.GOARCH))
	}
	dir := *societyBenchDir
	if dir == "" {
		dir = t.TempDir()
	}
	ctx := context.Background()
	layouts := map[string]benchLayout{"room": roomLayout(), "society": societyLayout()}
	templates := map[string]string{}
	for name, l := range layouts {
		path := filepath.Join(dir, "template-"+name, "channels.db")
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		begin := time.Now()
		if err := seedTemplate(ctx, path, l); err != nil {
			t.Fatalf("seed %s: %v", name, err)
		}
		t.Logf("seeded %s in %s", name, time.Since(begin).Round(time.Second))
		templates[name] = path
	}
	service := map[string]map[string]float64{}
	for name, path := range templates {
		st, err := serviceTimes(ctx, path, filepath.Join(dir, "service.db"), layouts[name])
		if err != nil {
			t.Fatalf("service times %s: %v", name, err)
		}
		service[name] = st
		t.Logf("service times %s: %v", name, st)
	}

	env := benchEnvironment(t, templates["room"])
	failedRamp := map[string]bool{}
	var results []benchResult
	for i, step := range steps {
		key := step.Layout + "/" + step.Form
		if *societyBench == "ramp" && failedRamp[key] {
			continue
		}
		res := runBenchStep(ctx, t, templates[step.Layout],
			filepath.Join(dir, fmt.Sprintf("step-%02d", i)), layouts[step.Layout], step)
		results = append(results, res)
		if !res.Pass {
			failedRamp[key] = true
		}
		s := res.Stats
		t.Logf("%-7s %-8s %5.0f/s pass=%-5v offered=%d published=%d e2e p99=%.1fms max=%.1fms history p99=%.1fms society-w p99=%.1fms wal=%.1fMB %v",
			step.Layout, step.Form, step.Rate, res.Pass, res.Offered, res.Published,
			s[classPublishE2E].P99ms, s[classPublishE2E].MaxMs, s[classHistory].P99ms,
			s[classSocietyWrite].P99ms, res.WALPeakMB, res.Failures)
	}
	if outPath != "" {
		out := map[string]any{"plan": *societyBench, "environment": env,
			"service_times_ms": service, "results": results}
		b, err := json.MarshalIndent(out, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(outPath, b, 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// benchList splits a comma-separated flag value and refuses any name not
// in allowed.
func benchList(flagName, value string, allowed ...string) ([]string, error) {
	var out []string
	for _, v := range strings.Split(value, ",") {
		v = strings.TrimSpace(v)
		if !slices.Contains(allowed, v) {
			return nil, fmt.Errorf("%s: unknown value %q (want %s)", flagName, v, strings.Join(allowed, ", "))
		}
		out = append(out, v)
	}
	return out, nil
}

func runBenchStep(ctx context.Context, t *testing.T, template, dir string, l benchLayout, step benchStep) benchResult {
	t.Helper()
	path := filepath.Join(dir, "channels.db")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := copyBenchFile(template, path); err != nil {
		t.Fatal(err)
	}
	cs, err := NewSQLiteStore(path, SQLiteOptions{MaxChannels: 100})
	if err != nil {
		t.Fatal(err)
	}
	society, closeSociety, err := openSociety(cs, path, step.Form)
	if err != nil {
		t.Fatal(err)
	}
	w := &benchWorld{layout: l, store: cs, society: society, rec: newBenchRecorder(),
		runID: fmt.Sprintf("run-%s-%s-%.0f", step.Layout, step.Form, step.Rate), stop: make(chan struct{})}
	stepCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	begin := time.Now()
	walPeak := w.drive(stepCtx, cancel, step, path+"-wal")
	res := benchResult{benchStep: step, Seconds: time.Since(begin).Seconds(),
		Offered: w.offered.Load(), Published: w.published.Load(), Saturated: w.saturated.Load(),
		WALPeakMB: float64(walPeak) / (1 << 20), Stats: map[benchClass]benchStats{},
		PostSocietyReads: w.postSocietyReads.Load(), HoldsOpened: w.holdsOpened.Load()}
	if err := closeSociety(); err != nil {
		t.Fatal(err)
	}
	if err := cs.Close(); err != nil {
		t.Fatal(err)
	}
	if fi, err := os.Stat(path); err == nil {
		res.DBSizeMB = float64(fi.Size()) / (1 << 20)
	}
	w.rec.mu.Lock()
	for _, c := range benchClasses {
		s := slices.Clone(w.rec.samples[c])
		slices.Sort(s)
		res.Stats[c] = benchStats{Count: len(s), Errors: w.rec.errs[c],
			P50ms: ms(nearestRank(s, .50)), P95ms: ms(nearestRank(s, .95)),
			P99ms: ms(nearestRank(s, .99)), MaxMs: ms(nearestRank(s, 1))}
	}
	res.LostRaces = w.rec.lostRaces
	res.Cancelled = w.rec.cancelled
	if len(w.rec.errText) > 0 {
		res.ErrorTexts = w.rec.errText
	}
	w.rec.mu.Unlock()
	res.judge()
	if err := os.RemoveAll(dir); err != nil {
		t.Logf("keep %s: %v", dir, err)
	}
	return res
}

// benchEnvironment records what the numbers depend on.
func benchEnvironment(t *testing.T, template string) map[string]any {
	t.Helper()
	cs, err := NewSQLiteStore(template, SQLiteOptions{MaxChannels: 100})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cs.Close() }()
	db := cs.(*sqliteStore).db
	var version, journal string
	var synchronous, autockpt int
	for q, dst := range map[string]any{
		`SELECT sqlite_version()`: &version, `PRAGMA journal_mode`: &journal,
		`PRAGMA synchronous`: &synchronous, `PRAGMA wal_autocheckpoint`: &autockpt,
	} {
		if err := db.QueryRow(q).Scan(dst); err != nil {
			t.Fatalf("%s: %v", q, err)
		}
	}
	return map[string]any{"goos": runtime.GOOS, "goarch": runtime.GOARCH, "go": runtime.Version(),
		"cpus": runtime.NumCPU(), "sqlite": version, "journal_mode": journal,
		"synchronous": synchronous, "wal_autocheckpoint": autockpt,
		"date": time.Now().UTC().Format(time.RFC3339)}
}

func copyBenchFile(from, to string) error {
	src, err := os.Open(from)
	if err != nil {
		return err
	}
	defer func() { _ = src.Close() }()
	dst, err := os.Create(to)
	if err != nil {
		return err
	}
	if _, err := io.Copy(dst, src); err != nil {
		_ = dst.Close()
		return err
	}
	return dst.Close()
}
