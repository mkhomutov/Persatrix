package scheduler

import (
	"encoding/json"
	"fmt"
	"sync"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/executor/packaging"
	"github.com/mkhomutov/persatrix/internal/planner"
)

// ContextPackageKey is the reserved key under TaskRequest.context where the
// per-step context package JSON payload lives (RFC 0008 Open Question 2 —
// additive; no proto changes in Phase 1).
const ContextPackageKey = "_context_package"

// warningSampleCap bounds how many times a given (execution_id, step_id,
// warning) tuple emits a structured zap.Warn before being silently sampled
// out for the rest of the run. RFC 0008 PR 1b finding L11: a step that fans
// out the same warning on every retry would otherwise spam the log pipeline
// at multiplicative cost (steps × retries × warning kinds). One emission per
// tuple is sufficient for triage — the same metrics stay visible via the
// cost record (`StepCostEntry.ContextPackage`).
const warningSampleCap = 1

// warningSampler tracks per-(execution_id, step_id, warning) emission counts
// so the L11 sampler can short-circuit duplicate warns. Stored as a sync.Map
// keyed by the joined tuple string; values are *atomic-incremented int64
// counters. Memory grows with concurrent runs; entries are not pruned today
// because warning-eligible workloads are rare and the counters reset on
// scheduler restart, which matches the existing CostReporter contract.
type warningSampler struct {
	counts sync.Map // key: "exec|step|warning" -> *int64 (held under sync.Map RW)
}

// shouldEmit returns true the first warningSampleCap times the (run, step,
// warning) tuple is observed and false afterwards. Implementation deliberately
// uses the simple sync.Map LoadOrStore + lock-protected counter pattern rather
// than atomic/int64 ptr juggling — this code path runs at most a handful of
// times per step on the warn-emission path, so contention is irrelevant.
func (s *warningSampler) shouldEmit(execID, stepID, warning string) bool {
	key := execID + "|" + stepID + "|" + warning
	value, _ := s.counts.LoadOrStore(key, new(samplerCounter))
	c, ok := value.(*samplerCounter)
	if !ok {
		// Defensive: a mistyped value can never appear because LoadOrStore is
		// the only writer, but never trust a sync.Map cast at face value.
		return true
	}
	return c.takeOne()
}

// samplerCounter is a tiny mutex-guarded counter used as the sync.Map value
// to keep the L11 emission limit deterministic without spinning on atomics.
type samplerCounter struct {
	mu  sync.Mutex
	hit int
}

// takeOne returns true on the first warningSampleCap calls, false after.
func (c *samplerCounter) takeOne() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.hit >= warningSampleCap {
		return false
	}
	c.hit++
	return true
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
func (s *WorkflowScheduler) attachContextPackage(
	outputsCopy map[string]string,
	runID string,
	step planner.Step,
	resolvedInput string,
	budgetTokens int,
) (*packaging.Package, error) {
	candidates := make([]packaging.Candidate, 0, len(outputsCopy))
	for key, content := range outputsCopy {
		// Skip any reserved keys (defence-in-depth: should never happen since
		// outputs come from output_key which is regex-validated, but a future
		// extension to the context map could collide).
		if key == ContextPackageKey {
			continue
		}
		candidates = append(candidates, packaging.Candidate{
			ID:         key,
			Content:    content,
			Tokens:     estimateTokens(content),
			Importance: importanceForCandidate(key, step.DependsOn),
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
func estimateTokens(s string) int {
	if s == "" {
		return 0
	}
	t := len(s) / 4
	if t == 0 {
		return 1
	}
	return t
}

// importanceForCandidate gives explicit-dependency outputs a higher
// importance hint than other workflow outputs. Combined with the heuristic
// scorer's dependency-proximity boost this means depends_on candidates win
// admission ties without the scorer needing to inspect outputs separately.
func importanceForCandidate(id string, dependsOn []string) float64 {
	for _, d := range dependsOn {
		if d == id {
			return 0.9
		}
	}
	return 0.5
}
