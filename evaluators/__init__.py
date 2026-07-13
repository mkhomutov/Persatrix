"""Persatrix golden-trace eval harness (RFC 0044).

Phase 1 ships the eval-set *format* + the deterministic *assertion engine*:

- :mod:`evaluators.assertions` — the closed assertion vocabulary (RFC 0044 §B)
  and the observed-run type ``EvalRun``.
- :mod:`evaluators.eval_set` — the recipe loader (``load_eval_set``, validated
  against ``schemas/eval_set.json``) and ``evaluate`` (recipe × run → report).

The replay LLM client, the runner, the seed recipes, and the Makefile / CI
wiring land in subsequent PRs (see ``docs/rfcs/0044-pr-plan.md``).
"""

from evaluators.assertions import (
    AssertionResult,
    EvalRun,
    MatchOp,
    match_content,
    match_event_count,
    match_event_sequence,
    match_exact,
    match_numeric,
)
from evaluators.eval_set import (
    Assertions,
    ContentAssertion,
    EvalReport,
    EvalSet,
    EventAssertion,
    Interaction,
    Setup,
    StateMatcher,
    Turn,
    evaluate,
    load_eval_set,
)

__all__ = [
    "Assertions",
    "AssertionResult",
    "ContentAssertion",
    "EvalReport",
    "EvalRun",
    "EvalSet",
    "EventAssertion",
    "Interaction",
    "MatchOp",
    "Setup",
    "StateMatcher",
    "Turn",
    "evaluate",
    "load_eval_set",
    "match_content",
    "match_event_count",
    "match_event_sequence",
    "match_exact",
    "match_numeric",
]
