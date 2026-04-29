package scheduler

import (
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"sync"
	"unicode/utf8"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/executor/packaging"
	"github.com/mkhomutov/persatrix/internal/planner"
)

// ContextPackageKey is the reserved key under TaskRequest.context where the
// per-step context package JSON payload lives (RFC 0008 Open Question 2 —
// additive; no proto changes in Phase 1).
const ContextPackageKey = "_context_package"

// noCopy may be embedded to surface accidental value-copies under
// `go vet -copylocks` (RFC 0008 PR 6a / N11). It carries no runtime cost —
// the Lock/Unlock no-ops only exist so the vet analyser treats embeddings
// like a sync.Mutex for copy-detection purposes.
type noCopy struct{}

func (*noCopy) Lock()   {}
func (*noCopy) Unlock() {}

// stepWarningKey is the per-run sub-key under which a (step, warning) tuple
// is recorded as already-emitted. Using a struct (rather than a joined
// string) is collision-free regardless of the characters in either field
// (RFC 0008 PR 6a / N10) and lets the sampler key directly on the tuple
// without any sprintf overhead on the warn-emission path.
type stepWarningKey struct {
	stepID  string
	warning string
}

// warningSampler caps each (execution_id, step_id, warning) tuple at one
// structured zap warning per run (RFC 0008 PR 1b / L11). The cost record on
// `StepCostEntry.ContextPackage` carries the unsampled metric for every
// step, so capping log noise here loses no telemetry.
//
// Run state lives in a per-run sub-sampler (`runSampler`) so the scheduler
// can drop the entire bucket when the run terminates (RFC 0008 PR 6a / M11)
// — without that hook the sync.Map grew unbounded across a long-running
// orchestrator.
//
// The embedded `noCopy` sentinel surfaces value-copies under
// `go vet -copylocks`; the sync.Map's zero value is the only safe form.
type warningSampler struct {
	_    noCopy
	runs sync.Map // execID string -> *runSampler
}

// runSampler holds the per-run set of (step, warning) tuples that have
// already emitted once. Pruned in bulk via `warningSampler.pruneRun`.
type runSampler struct {
	mu   sync.Mutex
	seen map[stepWarningKey]struct{}
}

// shouldEmit returns true the first time a given (execID, stepID, warning)
// tuple is observed and false on every subsequent call within the same run.
//
// Implementation notes (RFC 0008 PR 6a):
//   - M12: `Load` first; only fall through to `LoadOrStore` on miss so the
//     hot path (warning already seen) does not allocate a fresh `runSampler`
//     on every call.
//   - L13/L14: the prior `samplerCounter`-with-cap-comparison was over-
//     engineered for a strict once-per-tuple budget; map-presence is the
//     direct expression of "warn once" and removes the unreachable
//     defensive cast that used to swallow type-assertion failures.
func (s *warningSampler) shouldEmit(execID, stepID, warning string) bool {
	var rs *runSampler
	if value, ok := s.runs.Load(execID); ok {
		rs = value.(*runSampler) //nolint:forcetypeassert // M12: only writer is LoadOrStore below — a foreign type cannot appear.
	} else {
		fresh := &runSampler{seen: make(map[stepWarningKey]struct{})}
		actual, _ := s.runs.LoadOrStore(execID, fresh)
		rs = actual.(*runSampler) //nolint:forcetypeassert // see M12 note above.
	}
	rs.mu.Lock()
	defer rs.mu.Unlock()
	key := stepWarningKey{stepID: stepID, warning: warning}
	if _, dup := rs.seen[key]; dup {
		return false
	}
	rs.seen[key] = struct{}{}
	return true
}

// pruneRun drops the per-run sampler bucket. Called from `executeRun`'s
// terminal-status path (RFC 0008 PR 6a / M11) so that long-running
// orchestrators do not accumulate sampler state for completed runs.
// No-op when the run never emitted a sampled warning.
func (s *warningSampler) pruneRun(execID string) {
	s.runs.Delete(execID)
}

// attachContextPackage assembles a packaging.Package for the given step from
// the available step outputs and writes the serialized JSON into outputsCopy
// under ContextPackageKey. The map is mutated in place because the caller
// already passes a per-step copy (see executeStep's outputsCopy snapshot).
//
// Returns the built package so executeStep can plumb its Metrics through to
// the cost recorder (PR 1b: ContextPackageMetrics on StepCostEntry) and the
// remaining-budget calculation (PR 1b: StepState.RemainingContextBudget).
//
// Candidates are derived from the outputs map (each step output becomes a
// candidate, scored by the heuristic scorer). Token estimation uses the
// chars/4 approximation — the orchestrator does not own a tokenizer and a
// rough estimate is sufficient for greedy admission ordering. The agent-side
// cost recorder remains the source of truth for actual consumed tokens.
//
// Review (M3): every entry in pkg.Metrics.Warnings (high_compression_ratio,
// extreme_compression_capped, pinned_overflow) is emitted as a structured zap
// warning so operators have visibility into compression-pressure regressions.
// The PR plan's sizing-risk note designates structured logs as Phase-1's
// substitute for cost-metrics emission; PR 1b also wires the cost record so
// dashboards no longer need to scrape logs for these signals.
//
// Review (L11, PR 1b): warnings are sampled per (execution_id, step_id,
// warning kind) so a noisy step doesn't spam the warn channel; the cost
// record carries the unsampled metric for every step.
//
// Review (M6, PR 6a): output IDs are sorted before candidate construction so
// `Packager.Build`'s "emit admitted in original input order" determinism
// contract holds across retries even when ranging over Go's randomised map
// iteration order.
func (s *WorkflowScheduler) attachContextPackage(
	outputsCopy map[string]string,
	runID string,
	step planner.Step,
	resolvedInput string,
	budgetTokens int,
) (*packaging.Package, error) {
	// M6: deterministic ID ordering is required for cross-retry stability.
	// Without `slices.Sorted`, two attaches of the same outputs map produce
	// candidate slices in different orders, defeating the packager's tie-
	// break-on-input-order contract once a packaging-aware agent (PR 2)
	// starts comparing packages across retries.
	keys := slices.Sorted(maps.Keys(outputsCopy))
	candidates := make([]packaging.Candidate, 0, len(keys))
	for _, key := range keys {
		// Skip the reserved key (defence-in-depth: a leading underscore is
		// already prohibited by `outputKeyRegex` =
		// `^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$` in the planner, so this
		// branch can never fire for a legitimate output. Kept so a future
		// extension to the context map cannot collide).
		if key == ContextPackageKey {
			continue
		}
		content := outputsCopy[key]
		candidates = append(candidates, packaging.Candidate{
			ID:         key,
			Content:    content,
			Tokens:     estimateTokens(content),
			Importance: importanceForCandidate(),
		})
	}

	pkg := s.packager.Build(candidates, packaging.QueryContext{
		StepID:    step.ID,
		Input:     resolvedInput,
		DependsOn: step.DependsOn,
	}, budgetTokens)

	encoded, err := json.Marshal(pkg)
	if err != nil {
		return nil, fmt.Errorf("marshal context package: %w", err)
	}
	outputsCopy[ContextPackageKey] = string(encoded)

	// Surface packager warnings via structured logs (review M3). One log line
	// per warning keeps each event independently filterable in log aggregators;
	// the shared dimensions (compression_ratio, candidates_dropped) make it
	// trivial to correlate the warnings with the workload that produced them.
	for _, w := range pkg.Metrics.Warnings {
		if !s.warningSampler.shouldEmit(runID, step.ID, w) {
			continue
		}
		s.logger.Warn("context_package warning",
			zap.String("warning", w),
			zap.String("execution_id", runID),
			zap.String("step_id", step.ID),
			zap.Float64("compression_ratio", pkg.Metrics.CompressionRatio),
			zap.Int("candidates_dropped", pkg.Metrics.CandidatesDropped),
			zap.Int("tokens_before", pkg.Metrics.TokensBefore),
			zap.Int("tokens_after", pkg.Metrics.TokensAfter),
		)
	}
	return &pkg, nil
}

// estimateTokens is a rough chars/4 approximation suitable for greedy
// admission ordering. The agent-side TokenCounter is the source of truth
// for actual consumed tokens; this estimate only orders candidates.
//
// L7 (PR 6a): rune-count rather than byte-count avoids inflating estimates
// 2–4× for multibyte UTF-8 (CJK, emoji, accented Latin). Acceptable since
// estimates only order candidates by relative density, but the bias should
// be removed at no extra cost.
func estimateTokens(s string) int {
	if s == "" {
		return 0
	}
	t := utf8.RuneCountInString(s) / 4
	if t == 0 {
		return 1
	}
	return t
}

// importanceForCandidate returns the uniform per-candidate importance hint
// the scheduler emits for context packaging.
//
// M9 (PR 6a): previously this returned 0.9 for IDs in `step.DependsOn` and
// 0.5 otherwise, so `HeuristicScorer.Score` then double-counted the
// dependency signal (importance boost AND its own depWeight). The fix is
// "scheduler emits uniform Importance; scorer owns dep-proximity entirely"
// per RFC 0008 PR 6a's M9 disposition. Future embedding-backed scorers
// receive a calibration-free hint and are free to renormalise.
func importanceForCandidate() float64 {
	return 0.5
}
