package packaging

import (
	"sort"
)

// Packager assembles a per-step Package from a list of candidates under a
// token budget. It is stateless and safe for concurrent use.
type Packager struct {
	scorer RelevanceScorer
}

// NewPackager constructs a Packager. A nil scorer falls back to the default
// HeuristicScorer so callers that don't need to swap backends can pass nil.
func NewPackager(scorer RelevanceScorer) *Packager {
	if scorer == nil {
		scorer = NewHeuristicScorer()
	}
	return &Packager{scorer: scorer}
}

// Build assembles a Package for the given candidates under budgetTokens.
// Pinned candidates are always admitted (even when over budget); non-pinned
// candidates compete on density (score / tokens) and are admitted greedily
// until the budget is exhausted. Tied densities break on candidate ID
// (lexicographic) so identical workloads produce identical packages.
//
// Returns the package; never returns an error — over-budget pinned admission
// is signalled via Metrics.Warnings ("pinned_overflow") rather than failing.
func (p *Packager) Build(candidates []Candidate, query QueryContext, budgetTokens int) Package {
	pinned := make([]AdmittedSection, 0)
	pinnedTokens := 0
	competing := make([]scoredCandidate, 0, len(candidates))
	totalNonPinnedTokens := 0

	// Phase 1: separate pinned (always admitted) from competing candidates,
	// scoring the latter once.
	for _, c := range candidates {
		if c.Pinned {
			pinned = append(pinned, AdmittedSection{ID: c.ID, Content: c.Content, Tokens: c.Tokens})
			pinnedTokens += c.Tokens
			continue
		}
		totalNonPinnedTokens += c.Tokens
		score := p.scorer.Score(c, query)
		density := 0.0
		if c.Tokens > 0 {
			density = score / float64(c.Tokens)
		}
		competing = append(competing, scoredCandidate{cand: c, score: score, density: density})
	}

	// Phase 2: deterministic sort by density desc, ID asc on ties.
	sort.SliceStable(competing, func(i, j int) bool {
		if competing[i].density != competing[j].density {
			return competing[i].density > competing[j].density
		}
		return competing[i].cand.ID < competing[j].cand.ID
	})

	// Phase 3: greedy knapsack — track admitted set so we can preserve
	// input order in the emitted slice.
	remaining := budgetTokens - pinnedTokens
	if remaining < 0 {
		remaining = 0
	}
	admittedIDs := make(map[string]struct{}, len(competing))
	dropped := 0
	for _, sc := range competing {
		if sc.cand.Tokens <= remaining {
			admittedIDs[sc.cand.ID] = struct{}{}
			remaining -= sc.cand.Tokens
		} else {
			dropped++
		}
	}

	// Phase 4: emit admitted in original input order (deterministic, matches
	// what reviewers expect when reading the package alongside the workflow).
	stepOutputs := make([]AdmittedSection, 0, len(admittedIDs))
	admittedTokens := 0
	for _, c := range candidates {
		if c.Pinned {
			continue
		}
		if _, ok := admittedIDs[c.ID]; !ok {
			continue
		}
		stepOutputs = append(stepOutputs, AdmittedSection{ID: c.ID, Content: c.Content, Tokens: c.Tokens})
		admittedTokens += c.Tokens
	}

	// Metrics: compression ratio is computed against non-pinned weight only
	// per RFC 0008 Open Question 3 so heavily-pinned workloads do not trip
	// spurious high-compression warnings.
	metrics := Metrics{
		TokensBefore:      totalNonPinnedTokens,
		TokensAfter:       admittedTokens,
		CandidatesDropped: dropped,
	}
	switch {
	case admittedTokens == 0 && totalNonPinnedTokens == 0:
		metrics.CompressionRatio = 1.0
	case admittedTokens == 0:
		// All non-pinned candidates dropped — treat as cap (10:1) and warn.
		metrics.CompressionRatio = ExtremeCompressionCap
		metrics.Warnings = append(metrics.Warnings, "extreme_compression_capped")
	default:
		ratio := float64(totalNonPinnedTokens) / float64(admittedTokens)
		if ratio > ExtremeCompressionCap {
			metrics.CompressionRatio = ExtremeCompressionCap
			metrics.Warnings = append(metrics.Warnings, "extreme_compression_capped")
		} else {
			metrics.CompressionRatio = ratio
			if ratio >= HighCompressionRatioThreshold {
				metrics.Warnings = append(metrics.Warnings, "high_compression_ratio")
			}
		}
	}

	if pinnedTokens > budgetTokens {
		metrics.Warnings = append(metrics.Warnings, PinnedOverflowKey)
	}

	return Package{
		Version:        PackageVersion,
		PinnedSections: pinned,
		StepOutputs:    stepOutputs,
		Metrics:        metrics,
		// PR 1 emits 0; PR 2 wires the orchestrator-side allocator.
		BudgetMemoryTokens: 0,
	}
}

// scoredCandidate is the internal sort tuple. Kept private so future scorer
// backends don't accidentally depend on the density formula.
type scoredCandidate struct {
	cand    Candidate
	score   float64
	density float64
}
