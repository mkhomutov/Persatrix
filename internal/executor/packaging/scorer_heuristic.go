package packaging

import (
	"strings"
	"unicode"
)

// RelevanceScorer scores a candidate against the dispatch's QueryContext.
// The default heuristic backend lives in scorer_heuristic.go. RFC 0008
// Open Question 1 commits this surface so a future embedding-backed
// backend can swap in via DI without touching the packager.
type RelevanceScorer interface {
	// Score returns a non-negative relevance value. Higher is more relevant.
	// The packager uses score / tokens as the knapsack density key.
	Score(candidate Candidate, query QueryContext) float64
}

// HeuristicScorer is the default Phase-1 scorer combining:
//   - dependency proximity: candidate ID listed in QueryContext.DependsOn → boost
//   - lexical overlap: token-set Jaccard between candidate and step input
//   - importance: caller-supplied [0, 1] weight
//
// Recency is implicit (callers pass candidates in dependency order; ties
// break on input order in the packager) so it is not re-applied here.
type HeuristicScorer struct{}

// NewHeuristicScorer returns the default Phase-1 scorer.
func NewHeuristicScorer() HeuristicScorer { return HeuristicScorer{} }

// Score implements RelevanceScorer.
//
// The formula is intentionally simple and tunable in one place:
//
//	score = importance_weight * importance
//	      + dep_weight        * 1{candidate.ID ∈ query.DependsOn}
//	      + overlap_weight    * jaccard(tokens(candidate), tokens(query.Input))
//
// Weights sum to 1.0 so the output stays in [0, 1] regardless of which
// signals fire — this keeps the density (score / tokens) interpretable and
// avoids an embedding-tier scorer needing to renormalise to compete.
func (h HeuristicScorer) Score(c Candidate, q QueryContext) float64 {
	const (
		importanceWeight = 0.4
		depWeight        = 0.4
		overlapWeight    = 0.2
	)

	score := importanceWeight * clamp01(c.Importance)

	if isDependency(c.ID, q.DependsOn) {
		score += depWeight
	}

	if overlap := jaccard(tokenize(c.Content), tokenize(q.Input)); overlap > 0 {
		score += overlapWeight * overlap
	}

	return score
}

func clamp01(v float64) float64 {
	switch {
	case v < 0:
		return 0
	case v > 1:
		return 1
	default:
		return v
	}
}

func isDependency(id string, deps []string) bool {
	for _, d := range deps {
		if d == id {
			return true
		}
	}
	return false
}

// tokenize is a deliberately simple lowercase-word splitter — anything more
// sophisticated belongs in an embedding-backend scorer.
func tokenize(s string) map[string]struct{} {
	out := make(map[string]struct{})
	var b strings.Builder
	flush := func() {
		if b.Len() == 0 {
			return
		}
		out[strings.ToLower(b.String())] = struct{}{}
		b.Reset()
	}
	for _, r := range s {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			b.WriteRune(r)
			continue
		}
		flush()
	}
	flush()
	return out
}

func jaccard(a, b map[string]struct{}) float64 {
	if len(a) == 0 || len(b) == 0 {
		return 0
	}
	var inter int
	// Iterate the smaller map for a tiny constant-factor speedup.
	if len(a) > len(b) {
		a, b = b, a
	}
	for k := range a {
		if _, ok := b[k]; ok {
			inter++
		}
	}
	union := len(a) + len(b) - inter
	if union == 0 {
		return 0
	}
	return float64(inter) / float64(union)
}
