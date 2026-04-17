"""
Persatrix Python-side execution limit defaults.

These constants define the conservative defaults for task agent execution
limits as specified in RFC 0006 Section B. They replace the inline magic
numbers previously scattered across base.py.

Persona-specific constants (_MAX_SUB_AGENT_TOKENS, etc.) remain in
persona_runtime.py per RFC 0006 Open Question 4 — they are not migrated
here until RFC 0010 integrates sub-agent spawning with budget enforcement.

Go-side equivalents live in internal/defaults/defaults.go. Both sides must
stay conceptually aligned: changes to either should be reviewed together.
"""

# Maximum number of LLM calls a task agent may make per task execution.
# Lowered from 10 to 5: most v0.1 tasks complete in 1–3 calls; 5 provides
# headroom for tool use without allowing runaway loops (RFC 0006 §B).
DEFAULT_MAX_LLM_CALLS: int = 5

# Maximum output tokens per LLM call for task agents.
# Raised from 4096 to 8192: 4096 was too low for code generation tasks
# that include context. 8192 covers typical code review and generation
# (RFC 0006 §B).
DEFAULT_MAX_TOKENS: int = 8192

# Default per-task wall-clock timeout in seconds.
# Agents that do not receive an explicit timeout from TaskConfig fall back
# to this value. Orchestrator-side default is defined in
# internal/defaults/defaults.go as DefaultTimeoutSeconds (60).
# Used by executor deadline derivation (PR 2); Python-side timeout wiring
# deferred to RFC 0008.
DEFAULT_TIMEOUT_SECONDS: int = 60
