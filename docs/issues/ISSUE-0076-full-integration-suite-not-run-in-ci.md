---
# Allowed values are documented in README.md. Comments above fields
# (not inline) so that the front-matter parser does not pick them up.
id: ISSUE-0076
# summary: one-line description, surfaced as the Summary column in INDEX.md
summary: "CI never runs the full tests/integration/ suite, so the v0.3.4 no-default-provider change broke the close-path integration tests undetected"
# status: open | in_progress | resolved
status: resolved
# severity: low | medium | high | critical
severity: high
# area: internal/ package or agent subsystem (cost, persona, memory, grpc, ...)
area: ci
# created: YYYY-MM-DD when the finding was first captured (validated)
created: 2026-05-27
# closed: YYYY-MM-DD — set only when status == resolved (validated)
closed: 2026-05-27
# closed_pr: closing PR number (no leading "#") — rendered as #NNN link in INDEX
closed_pr: 443
refs:
  - .github/workflows/ci.yml
  - docs/v0.3.4-plan-amendment-2026-05-27.md
  - docs/v0.3.4-release-prep-plan.md
  - tests/integration/conftest.py
  - agents/persona_runtime/summarize_close.py
---

## Summary

CI runs only the Python unit suite and the single `test_bored_persona_cost.py`
integration gate — it never runs the full `tests/integration/` suite. As a
result the v0.3.4 "no default provider" change shipped the base config's
`summarizer` alias as `provider: unconfigured`, which broke every close-path
integration test, and the breakage reached `main` undetected.

## Context

Found during v0.3.4 release-prep PR 4 (final pre-tag verification), re-running
the full gate sweep on the post-bump tip `cf1742b`.

[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) has exactly two
Python `pytest` invocations: `tests/unit/python/` (the `python` job) and
`tests/integration/test_bored_persona_cost.py` (the conditional
`cost-regression-gate` job). The rest of `tests/integration/` is run only by
`make test-integration` locally and during manual-test execution — never in CI.

The knob-free provider-selection refactor
([#440](https://github.com/mkhomutov/Persatrix/pull/440),
[amendment 2026-05-27](../v0.3.4-plan-amendment-2026-05-27.md)) made the shipped
`config/optimization.yaml` ship `quality` / `fast` / `summarizer` as
`provider: unconfigured`. On the summarisation close path
([`agents/persona_runtime/summarize_close.py`](../../agents/persona_runtime/summarize_close.py)),
`resolve_model("summarizer")` now raises `SystemExit`, which
`summarize_closed_interaction` catches and degrades to
`SUMMARY_UNAVAILABLE_TEXT` (a deliberate, correct graceful degradation on a
background task). PR 2 added an autouse `_resolvable_summarization_model`
fixture to pin a resolvable model — but only in `tests/unit/python/conftest.py`.
The close-path **integration** tests
(`test_summarize_on_close`, `test_summarize_on_close_phases`,
`test_facts_extractor_close`, `test_facts_extractor_message_content`,
`test_interaction_multi_turn`, `test_interaction_multi_turn_followups`) use the
same path and had no such fixture, so they collapse to the fallback and fail the
LLM-summary assertion (one also hangs). PR 2's own commit noted it "broke 8
tests in the full CI suite (missed by the targeted local slice run)" — but the
"full CI suite" there is the unit suite; the integration tier is not gated.

## Impact

- `make test` is **not green on `main`**: the full `tests/integration/` suite
  has 9 failing tests plus one hang (`212 passed` only after the fix).
- No runtime/product impact — the production summarisation path degrades
  correctly when no provider is configured. The defect is test coverage + a CI
  detection gap.
- The wider risk is the gap itself: an entire integration tier runs in no CI
  job, so this class of regression is invisible until someone runs the suite by
  hand. v0.3.4's release verification is the first time it surfaced.

## Proposed fix / investigation path

Two parts, both in this PR:

1. Add `tests/integration/conftest.py` with the same autouse
   `_resolvable_summarization_model` fixture as the unit conftest (patches the
   `summarization_model` name bound inside `summarize_close`; tests that want
   the unresolvable-fallback path re-monkeypatch in-body and still win).
2. Add a CI step that runs the full `tests/integration/` suite in the `python`
   job, so the integration tier is gated going forward.

## Notes

> 2026-05-27 — captured during v0.3.4 release-prep PR 4. Fix verified locally:
> the full suite goes from 9 failures + 1 hang to `212 passed, 11 skipped`.
