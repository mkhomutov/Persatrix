package cost

import "github.com/mkhomutov/persatrix/internal/executor/packaging"

// ContextPackageMetrics captures the per-step packaging metrics emitted by
// the scheduler's `_context_package` builder (RFC 0008 PR 1) so they can be
// surfaced through the cost endpoint alongside token usage.
//
// Wired through `StepCostEntry.ContextPackage` during RFC 0008 PR 1b (the
// sizing-risk follow-on to PR 1). The PR 1 packager already produces these
// numbers via `packaging.Metrics`; PR 1b only adds the cost-side persistence
// path so dashboards can correlate compression pressure with model spend.
//
// `CandidatesAdmitted` is intentionally tracked here in the cost record even
// though the wire-shape `_context_package.metrics` block does not expose it
// (consumers compute admitted == len(step_outputs) per the PR plan §Key
// implementation details). Cost dashboards typically show admit/drop side by
// side, so duplicating the derived value here avoids cross-record joins.
type ContextPackageMetrics struct {
	TokensBefore       int     `json:"tokens_before"`
	TokensAfter        int     `json:"tokens_after"`
	CompressionRatio   float64 `json:"compression_ratio"`
	CandidatesAdmitted int     `json:"candidates_admitted"`
	CandidatesDropped  int     `json:"candidates_dropped"`
}

// NewContextPackageMetrics constructs a ContextPackageMetrics from the Phase-1
// `packaging.Package`, deriving admitted-count from `len(pkg.StepOutputs)`.
// Returns nil when pkg is nil so callers can use the helper unconditionally
// on the post-build path.
//
// N9 (RFC 0008 PR 6a): the prior signature took `*packaging.Metrics` plus
// a separate admitted int — easy to miswire (caller must remember to pass
// `len(pkg.StepOutputs)` and not e.g. `len(pkg.Memories)`). Taking the
// package directly removes the foot-gun.
func NewContextPackageMetrics(pkg *packaging.Package) *ContextPackageMetrics {
	if pkg == nil {
		return nil
	}
	return &ContextPackageMetrics{
		TokensBefore:       pkg.Metrics.TokensBefore,
		TokensAfter:        pkg.Metrics.TokensAfter,
		CompressionRatio:   pkg.Metrics.CompressionRatio,
		CandidatesAdmitted: len(pkg.StepOutputs),
		CandidatesDropped:  pkg.Metrics.CandidatesDropped,
	}
}
