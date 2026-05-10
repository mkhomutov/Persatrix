// Package packaging implements the per-step context-packaging pipeline
// (RFC 0008 §D). It selects candidate context fragments (typically the
// outputs of dependency steps), scores them via a pluggable RelevanceScorer,
// and admits them under a token budget using a greedy knapsack ordered by
// relevance density (relevance / tokens). Pinned sections are always admitted;
// extractive truncation drops the lowest-density candidates first.
//
// Phase 1 (this PR) is extractive-only — abstractive compression is deferred
// to Phase 1b. The shape and metrics are pre-wired so the abstractive path
// can plug in without schema churn.
package packaging

// PackageVersion is the wire-format version for the JSON payload serialized
// under the reserved `_context_package` key in the gRPC TaskRequest.context map.
// Any new top-level field added after PR 1 merges requires a version bump and
// a separate RFC amendment.
const PackageVersion = 1

// PinnedOverflowKey is the metric/log event emitted when the pinned-section
// token sum alone exceeds the per-step budget. Packaging admits the pinned
// sections anyway (correctness over budget) and surfaces the operator alert
// via this metric.
const PinnedOverflowKey = "pinned_overflow"

// HighCompressionRatioThreshold is the warn-level compression ratio
// (RFC 0008 Open Question 3 — warn at 4:1).
const HighCompressionRatioThreshold = 4.0

// ExtremeCompressionCap is the hard cap on emitted compression ratio
// (RFC 0008 Open Question 3 — cap at 10:1).
const ExtremeCompressionCap = 10.0

// Candidate is a single context fragment (typically a dependency step's
// output) that may be admitted into the package.
type Candidate struct {
	// ID is a stable identifier for deterministic tie-breaking under equal
	// densities. Typically the upstream step's output_key or step ID.
	ID string
	// Content is the raw text of the candidate.
	Content string
	// Tokens is the candidate's token cost. Callers compute this once via
	// their token-counter of choice; the packager never re-tokenises.
	Tokens int
	// Importance is a caller-supplied [0.0, 1.0] hint reflecting workflow-author
	// intent (e.g. a depends_on output is more important than a workflow constant).
	Importance float64
	// Pinned candidates are always admitted, even when the resulting package
	// exceeds B_step. They are excluded from the compression-ratio denominator
	// per RFC 0008 Open Question 3.
	Pinned bool
}

// QueryContext describes the dispatch the package is being assembled for.
// The default heuristic scorer reads it for lexical-overlap and
// dependency-proximity signals.
type QueryContext struct {
	// StepID is the step receiving the package.
	StepID string
	// Input is the resolved step input text.
	Input string
	// DependsOn is the set of upstream output_keys the step explicitly depends on.
	DependsOn []string
}

// AdmittedSection is a candidate that survived knapsack admission and made it
// into the final package. Order in the package is preserved input order
// (deterministic — matches the order callers passed candidates in).
type AdmittedSection struct {
	ID      string `json:"id"`
	Content string `json:"content"`
	Tokens  int    `json:"tokens"`
}

// Metrics is the per-package telemetry surface. The fields prefixed with
// `tokens_` exclude pinned-section weight from the denominator per
// RFC 0008 Open Question 3 so a workload that is mostly pinned does not
// trigger spurious high-compression warnings.
type Metrics struct {
	TokensBefore      int     `json:"tokens_before"`
	TokensAfter       int     `json:"tokens_after"`
	CompressionRatio  float64 `json:"compression_ratio"`
	CandidatesDropped int     `json:"candidates_dropped"`
	// Warnings is the set of emitted warning keys (e.g. "high_compression_ratio",
	// "extreme_compression_capped", "pinned_overflow"). Empty when the package
	// fits the budget cleanly.
	Warnings []string `json:"warnings,omitempty"`
}

// Package is the assembled context-package emitted by the packager and
// forwarded to the agent as JSON under TaskRequest.context["_context_package"].
//
// Field order matches RFC 0008 PR 1 §Key implementation details. The shape is
// frozen as of PR 1 — additive evolution (e.g. abstractive Phase 1b) must
// either fit existing fields or bump PackageVersion.
//
// v1 advisory-only contract: the dispatch carries both raw upstream outputs
// (under their planner output keys, e.g. "out1", "out2", …) AND the same
// content embedded inside StepOutputs[].Content. A packaging-unaware agent
// reading raw outputs bypasses the budget entirely. v1 packaging is therefore
// advisory ordering — actual budget enforcement requires the agent to consume
// StepOutputs in lieu of raw outputs (e.g. via MemoryFacade in RFC 0008 PR 2).
type Package struct {
	Version        int               `json:"version"`
	PinnedSections []AdmittedSection `json:"pinned_sections"`
	StepOutputs    []AdmittedSection `json:"step_outputs"`
	Metrics        Metrics           `json:"metrics"`
	// BudgetMemoryTokens is the orchestrator-side advisory budget for memory
	// retrieval inside the agent (RFC 0008 PR 2 wires the non-zero allocator;
	// PR 1 emits 0 to lock the field into v1 up-front).
	BudgetMemoryTokens int `json:"budget_memory_tokens"`
}
