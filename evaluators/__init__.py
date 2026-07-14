"""Persatrix golden-trace eval harness (RFC 0044).

Phase 1 ships the eval-set *format* + the deterministic *assertion engine*:

- :mod:`evaluators.assertions` — the closed assertion vocabulary (RFC 0044 §B)
  and the observed-run type ``EvalRun``.
- :mod:`evaluators.eval_set` — the recipe loader (``load_eval_set``, validated
  against ``schemas/eval_set.schema.json``) and ``evaluate`` (recipe × run → report).

PR 2 adds :mod:`evaluators.replay_llm_client` — the recorded-response provider
that makes replay CI-safe (OQ #2). PR 3 adds :mod:`evaluators.runner` (the
recipe → :class:`EvalReport` orchestrator + ``make eval-replay`` / ``eval-record``
/ ``eval-drift`` entry point), :mod:`evaluators.persona_driver` (the persona-runtime
adapter that produces an ``EvalRun``), and :mod:`evaluators.report` (the structured
per-assertion artifact). Those three are imported from their submodules rather than
re-exported here on purpose: the replay client and the driver depend on the
``agents`` runtime, while the two modules above are pure and import nothing
project-internal — keeping ``import evaluators`` light for callers that only need
the assertion engine.

The seed recipes + their ``.golden.yaml`` sidecars, and the CI gate, land in
subsequent PRs (see ``docs/rfcs/0044-pr-plan.md``).
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
