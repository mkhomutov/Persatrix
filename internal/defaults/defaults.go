// Package defaults provides centralized system default constants for execution
// limits. These replace scattered magic numbers across the orchestrator and
// ensure a single source of truth for the three-level cascade:
// workflow step config → agent config → system defaults.
package defaults

// DefaultMaxLLMCalls is the system default for the maximum number of LLM calls
// per task step. Most v0.1 tasks complete in 1–3 calls; 5 provides headroom
// for tool use without allowing runaway loops.
const DefaultMaxLLMCalls = 5

// DefaultMaxTokens is the system default for the maximum output tokens per LLM
// call. Raised from 4096 to 8192 because observed v0.1 code generation tasks
// routinely truncate at 4096.
const DefaultMaxTokens = 8192

// DefaultTimeoutSeconds is the system default step timeout in seconds.
const DefaultTimeoutSeconds = 60

// DefaultTransportMargin is the additional time (in seconds) added to the step
// deadline to account for gRPC overhead, serialization, and network latency
// when computing the RPC timeout.
const DefaultTransportMargin = 5

// MinRetryBudgetFraction is the minimum fraction of the original budget
// (both time and tokens) that must remain for a retry to be attempted.
// Below this threshold, retries are skipped to avoid wasting compute
// on attempts that cannot complete meaningful work.
const MinRetryBudgetFraction = 0.25

// MaxTimeoutSeconds is the recommended upper bound for per-step timeouts.
// Steps with longer timeouts should be split into multiple steps or use
// streaming. This constant is not currently enforced as a hard limit —
// it is available for future timeout validation and for documentation.
// (PR 5a, S10: added to defaults for discoverability)
const MaxTimeoutSeconds = 3600

// DefaultMaxCascadeDepth is the system default cap on the cooperative-path
// cascade-depth backstop introduced by [RFC 0011 amendment
// "Cascade-depth wire propagation"]
// (docs/rfcs/0011-amendment-cascade-depth-wire-propagation.md). The
// orchestrator drops a fanout when inbound `cascade_depth >= cap`; the
// Python `EventDispatcher` carries the same cap as defense-in-depth
// ([agents/dispatch.py] `max_cascade_depth=5`). Keep the two values
// aligned — splitting them would mean the backstop fires on routine
// cap-bound traffic instead of only on a primary-enforcement regression.
const DefaultMaxCascadeDepth = 5
