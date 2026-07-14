---
id: RFC-0044
title: Eval-Set Shape with Golden Traces
summary: Specify a multi-turn golden-trace eval-set format that asserts a sequence of typed events, terminal state per scope, and final transcript per scenario; seed it with the dementia test and the F-3 recall scenario; gate persona/memory regressions on it.
type: process
status: implementing
author: Maksim Khomutov
created: 2026-05-20
target: v0.3.11 (Phase 1 format + replay) + v0.4.0+ (typed-event goldens)
depends_on:
  - RFC-0008
  - RFC-0020
---

# RFC 0044 — Eval-Set Shape with Golden Traces

**Type**: process
**Status**: 🚧 Implementing (Phase 1 — [PR plan](0044-pr-plan.md); rides v0.3.11 as a cuttable fold-in)
**Author**: Maksim Khomutov
**Date**: 2026-05-20
**Target**: v0.3.11 (Phase 1 format + replay) + v0.4.0+ (typed-event goldens, gated on [RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) Phase 1)
**Depends on**: RFC 0008 (Memory & Context Optimization — the recall/scoring layer goldens are recorded against), RFC 0020 (Interaction Lifecycle — episode/turn boundaries the goldens use)
**Relates to**: RFC 0041 (Typed Event Taxonomy — the events goldens assert sequences of; once 0041 lands, this RFC's surface becomes richer), RFC 0042 (State Namespacing — terminal state-per-scope assertions ride on this scope vocabulary), RFC 0043 (Inbound Agent-Interop Endpoint — external-agent scenarios become a new eval-set category)
**Spawned from**: [agent-runtime-vocabulary-roadmap.md §Eval-set shape as the regression gate](../agent-runtime-vocabulary-roadmap.md#eval-set-shape-as-the-regression-gate); [memory-quality-roadmap.md §Quality bar — the dementia test](../memory-quality-roadmap.md#quality-bar--the-dementia-test)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. Eval-set file shape](#a-eval-set-file-shape)
  - [B. Assertion vocabulary](#b-assertion-vocabulary)
  - [C. Recording vs replay](#c-recording-vs-replay)
  - [D. Stochasticity tolerance](#d-stochasticity-tolerance)
  - [E. Seed eval-sets](#e-seed-eval-sets)
  - [F. CI integration](#f-ci-integration)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

[`evaluators/`](../../evaluators) today has one stub file ([`conversation_scorer.py`](../../evaluators/conversation_scorer.py), `TODO`s only) and no codified format for what a regression assertion looks like. [`docs/manual-tests/`](../manual-tests) holds qualitative test scripts that humans run by eye, including [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) — the dementia test that defines the v0.3.0 persona quality bar. There is no automated bar that fails when a change regresses on memory recall, persona quality, or channel governance.

This RFC specifies a **multi-turn golden-trace eval-set format**: each eval is a recorded conversation plus a set of typed assertions over events, terminal state per scope, and the final transcript. The seed set is the dementia test, the F-3 recall scenario ([RFC 0031](0031-per-session-namespacing-channels.md)), and the ISSUE-0065/0066 error paths. The format is the regression gate the other three umbrella RFCs ([0041](0041-typed-event-taxonomy-lifecycle-callbacks.md), [0042](0042-state-namespacing-by-scope.md), [0043](0043-inbound-agent-interop-endpoint.md)) ship against — which is why this RFC is sequenced first.

## Motivation

### M-1. There is no automated regression bar for persona quality

The dementia test ([MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md)) is a paragraph of prose: "across a five-interaction scenario over 30 minutes covering one named entity, one stated preference, and one explicit commitment, does the persona reference each of those when an appropriate trigger appears later, without keyword overlap to seed the retrieval?" That is a qualitative bar that catches *blatant* regressions when a reviewer runs the test by hand. It does not catch subtle ones, and it does not gate CI.

Every memory/persona RFC since [RFC 0005](0005-persona-agent-memory.md) has been merged on faith that nothing previously working broke. The [memory-quality-roadmap.md](../memory-quality-roadmap.md) calls out this gap explicitly. This RFC closes it.

### M-2. RFC 0041 needs a place to land its assertions

[RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) promises "no silent behavior change" from event-taxonomy migration. The only way to verify that promise is to record golden event streams for representative scenarios *before* migration and replay them *after* migration. The recording format is what this RFC specifies.

### M-3. Manual tests do not scale with RFC velocity

There are 30+ manual tests in [`docs/manual-tests/`](../manual-tests). Each release-prep cycle re-runs them ([v0.3.2 release-prep PRs](../../docs/v0.3.2-release-prep-plan.md)) — a slow, expensive process. The manual tests will not go away (some scenarios are inherently qualitative), but the *deterministic* parts of each manual test — the event sequence, the terminal state, the final transcript — can become goldens that run on every PR.

### M-4. Sequenced first because every other RFC depends on it

The umbrella memo's [recommended sequencing](../agent-runtime-vocabulary-roadmap.md#recommended-sequencing) places this RFC first. The other three RFCs each promise "no silent behavior change" or "regressions caught at CI time." Those promises are vapor unless a regression gate exists. This RFC ships *before* RFCs 0041, 0042, 0043 because otherwise each of them invents its own ad-hoc regression check.

## Goals

1. **One codified eval-set format.** YAML or JSON file per scenario; conventional location under [`evaluators/eval_sets/`](../../evaluators).
2. **Multi-turn coverage.** Each eval spans at least two interactions ([RFC 0020](0020-interaction-lifecycle.md)) so cross-interaction memory behavior is exercised.
3. **Typed assertions.** Event sequences, terminal state per scope, final transcript content, with explicit tolerance rules for stochastic LLM output.
4. **Record + replay tooling.** A `make eval-record` mode generates a golden from a live run; `make eval-replay` checks a recorded golden against the current code.
5. **CI gate.** A subset of evals — the "stable" set — runs on every PR. Failures block merge.
6. **Seed the set.** Ship with the dementia test, F-3 recall, and ISSUE-0065/0066 paths recorded. Future RFCs add their own.

## Non-Goals

- **Replacing manual tests.** Qualitative bars stay manual ([MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) prose narrative will remain even when its deterministic skeleton is a golden). This RFC adds an automated layer underneath the manual layer.
- **LLM-judged scoring.** No model-graded eval pass/fail. Assertions are deterministic — event types, state keys, structural transcript checks. Quality-bar gating uses the persona quality-bar callback ([RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) §C) emitting a flag event the golden asserts on.
- **A general-purpose evaluation framework.** No model comparison, no A/B routing, no statistical aggregation across runs. One eval = one scenario = one expected trace.
- **Reproducibility of LLM token-exact output.** LLM responses are not byte-stable. The assertion vocabulary explicitly tolerates LLM variance ([§D](#d-stochasticity-tolerance)).
- **CI parallelism or perf optimization.** Evals run sequentially in CI; perf work is a follow-up if the suite outgrows its CI budget.

## Design / Implementation

### A. Eval-set file shape

```yaml
# evaluators/eval_sets/dementia_test.yaml
id: EVAL-MEMORY-001
title: Dementia test — five-interaction recall
description: |
  Across five interactions over a simulated 30-minute window covering
  one named entity, one stated preference, and one explicit commitment,
  the persona must reference each when an appropriate trigger appears.
spawned_from: ../manual-tests/MT-MEMORY-005-dementia-test.md
tier: stable                   # stable | experimental | nightly  (Phase 1: named `tier`, not `target_branch`)

setup:
  persona: ember-owl
  user: alice
  channel: dm:alice-ember
  seed_state:
    persona:ember-owl:trust.scores.alice: 0.0
  session_id: EVAL-MEMORY-001-S
  llm_mode: replay              # replay | live (replay uses recorded responses)

interactions:
  - id: i1
    turns:
      - user: "Hi Ember — I'm Alice, I work on the data-platform team."
      - assistant: {match: contains, value: "Alice"}
        events:
          - {type: ModelOutput}
          - {type: StateDelta, scope: persona, key_pattern: "ember-owl:trust.*"}
  - id: i2
    elapsed: 5m
    turns:
      - user: "What do you remember about me?"
      - assistant: {match: must_reference, values: ["Alice", "data-platform"]}
  # … i3 / i4 / i5 …

assertions:
  terminal_state:
    persona:ember-owl:trust.scores.alice: {match: gt, value: 0.0}
  event_count:
    Error: 0
  final_transcript:
    must_reference: ["Alice", "data-platform"]
    must_not_reference: ["[error]", "I don't recall"]
```

The format is intentionally close to the manual-test prose it derives from, so the same author writes both.

### B. Assertion vocabulary

| Assertion | Applies to | Example |
|-----------|------------|---------|
| `match: exact` | event type, state key, transcript line | `{type: Error, kind: wallet_denied}` |
| `match: contains` | transcript content | `{match: contains, value: "Alice"}` |
| `match: must_reference` | transcript content | `{match: must_reference, values: ["Alice", "data-platform"]}` |
| `match: must_not_reference` | transcript content | `{match: must_not_reference, values: ["[error]"]}` |
| `match: regex` | transcript content | `{match: regex, value: "^I (don't|do not) recall"}` |
| `match: gt` / `lt` / `gte` / `lte` / `eq` | numeric state | `{match: gt, value: 0.0}` |
| `event_sequence` | event stream slice | `[ModelOutput, ToolCall, ToolResult, ModelOutput]` |
| `event_count` | event stream | `{Error: 0, ToolCall: 2}` |
| `key_pattern` | state-key glob | `"ember-owl:trust.*"` |
| `terminal_state` | per-scope final state | see file shape |
| `elapsed` | between interactions | `5m`, `2h`, `1d` (simulated) |

The vocabulary is closed: adding a new assertion type is an RFC amendment.

**Co-dependency with RFC 0041.** The assertion grammar above references event-type names (`ModelOutput`, `ToolCall`, `Error`, `StateDelta`, `Control`) that [RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) §A introduces. The sequencing claim "0044 first" applies to the *format spec* — Phase 1 ships the file shape, the assertion grammar, and the replay runner without recording any goldens. Goldens against the seed scenarios in [§E](#e-seed-eval-sets) can only be recorded once [RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) Phase 1 emits the events they assert on. The two RFCs share an event-name vocabulary that lives in 0041; this RFC consumes it.

### C. Recording vs replay

- **Record mode** (`make eval-record TARGET=EVAL-MEMORY-001`): runs the scenario with `llm_mode: live`, captures the event stream, terminal state, and final transcript, and writes them as a sidecar `<eval_id>.golden.yaml`. The eval-set file becomes the *recipe*; the golden file is the *expected output*.
- **Replay mode** (`make eval-replay TARGET=EVAL-MEMORY-001`): runs the scenario with `llm_mode: replay` (LLM client returns recorded responses from the golden), executes all assertions, and reports pass/fail per assertion.
- **Drift detection** (`make eval-drift`): runs the scenario with `llm_mode: live` and compares the new run against the golden. Drift is reported but not fatal — it surfaces "the model changed" or "the prompt changed" without auto-updating the golden.

A golden is regenerated explicitly by the author (`make eval-record`); CI never overwrites a golden.

### D. Stochasticity tolerance

LLM output is not byte-stable. The assertion vocabulary ([§B](#b-assertion-vocabulary)) is designed around this:

- `match: exact` is never used for `assistant:` content; only for event types, state values, and tool-call arguments.
- `match: contains` / `must_reference` / `must_not_reference` / `regex` are for `assistant:` content. They assert *structural* properties (mentions Alice, does not say "I don't recall"), not token-level identity.
- LLM-mode `replay` is the default for CI. Recorded responses are deterministic; the LLM's role-as-mock is byte-stable.
- `live` mode (for `eval-drift`) runs against a real provider and accepts more variance — its purpose is to detect that the recorded scenario no longer matches reality, not to gate merges.

### E. Seed eval-sets

The initial set, all under [`evaluators/eval_sets/`](../../evaluators). IDs follow a fixed shape `EVAL-<DOMAIN>-<NNN>` with `<DOMAIN>` drawn from a flat closed list (`MEMORY`, `RECALL`, `ERROR`, `WORKING`, `FACTS`); source references live in the table column, not encoded into the ID:

| ID | Source | Asserts |
|----|--------|---------|
| `EVAL-MEMORY-001` | [MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) | Dementia-test recall across five interactions |
| `EVAL-RECALL-001` | [RFC 0031](0031-per-session-namespacing-channels.md) Phase 2 (F-3) | Cross-session memory does not leak into recall |
| `EVAL-ERROR-001` | [ISSUE-0065](../issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md) | Wallet denial publishes a typed chat-error on the channel |
| `EVAL-ERROR-002` | [ISSUE-0066](../issues/ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md) | Lease-cap / rate-limit / `RESOURCE_EXHAUSTED` publish typed chat-errors |
| `EVAL-WORKING-001` | [RFC 0034](0034-persona-conversational-working-memory.md) | Persona references its own prior question in the same interaction |
| `EVAL-FACTS-001` | [RFC 0026](0026-declarative-facts-tier.md) | Declarative facts surface in subsequent interactions without keyword overlap |

`EVAL-ERROR-001` and `EVAL-ERROR-002` are the lever the channel-error work has been missing — once they exist, future "synthesized chat-error" regressions fail CI instead of leaking into release prep.

Adding a new `<DOMAIN>` is an RFC amendment; sequential `<NNN>` within a domain is a routine addition.

### F. CI integration

- The eval suite has three tiers: `stable` (runs on every PR), `experimental` (runs on `main` post-merge), `nightly` (runs on a schedule).
- Initial classification: the six seed evals start in `stable` after their goldens are recorded and verified.
- A failed `stable` eval blocks merge. A failed `experimental` opens an issue but does not block.
- Eval failures emit a structured artifact (per-assertion pass/fail, diff against golden) attached to the CI run.

## Phased Implementation Plan

### Phase 1 — format + replay-only

Ship the eval-set file shape, the assertion vocabulary, and the replay runner. No CI gating yet. The runner produces a report; humans interpret it.

Seed-eval golden recording is staged: the eval-set *recipes* (scenario YAMLs in [§E](#e-seed-eval-sets)) land in this phase, but their `.golden.yaml` sidecars can only be recorded once [RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) Phase 1 emits the events the assertions reference. Until then, the runner replays recipes against the pre-0041 surface using the subset of assertions that do not require typed events (`final_transcript`, `terminal_state`).

### Phase 2 — CI gating

`make eval-replay` runs in CI on every PR for the `stable` tier. Failures block merge. Drift detection runs nightly on `main`.

### Phase 3 — drift workflow

Drift reports become actionable: a CI job opens an issue when drift exceeds threshold, with a structured diff that points at which assertions changed. Operators decide to re-record (`make eval-record`) or investigate.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Evaluators | `evaluators/runner.py` (new), `evaluators/replay_llm_client.py` (new), `evaluators/assertions.py` (new) | Runner, mock LLM, assertion engine |
| Evaluators | `evaluators/eval_sets/*.yaml`, `evaluators/eval_sets/*.golden.yaml` (new) | Seed evals + their goldens |
| Schemas | `schemas/eval_set.schema.json`, `schemas/eval_golden.schema.json` (new) | File shape validation |
| CI | `.github/workflows/eval.yml` (new) | CI gate (Phase 2) |
| Makefile | `Makefile` | `eval-record`, `eval-replay`, `eval-drift` targets |
| Docs | `docs/evaluators-guide.md` (new) | Author guide |

## Test Strategy

- **Unit tests**: assertion-engine semantics for every assertion type; replay-LLM-client determinism; golden-file parser round-trip.
- **Integration tests**: each seed eval runs in replay mode and passes; flipping one assertion to a wrong value fails as expected.
- **E2E**: the eval runner under CI produces a structured artifact that matches the documented schema.
- **Manual tests**: the seed evals' source manual tests ([MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md) and others) remain runnable by hand; the goldens are *additive*, not a replacement.
- **Self-test**: an "intentional regression" PR (e.g., disabling the F-3 recall filter) must fail `EVAL-RECALL-001` and be caught at CI before merge.

## Open Questions

1. **Where do goldens live in the repo?** Same directory as the eval-set, sidecar `.golden.yaml`. Alternative: separate `evaluators/eval_sets/goldens/` tree. Lean: sidecar — easier to review the pair together.
2. **How are LLM-replay responses recorded?** ✅ Resolved (PR 2). A cassette is `{request_hash: response_payload}`. The key is a `hashlib.sha256` over a canonicalized request — the six `create_message` inputs dumped as sorted, compact JSON, with volatile keys (`cache_control` prompt-cache markers, the opaque provider round-trip `signature`, `timestamp`/`idempotency_key`/`request_id`) stripped at any depth so an incidental difference does not cause a replay miss. `hashlib` (not the salted builtin `hash`) keeps the digest stable across processes, so a golden recorded once is portable to CI. Record and replay share the one canonicalization ([`evaluators/replay_llm_client.py`](../../evaluators/replay_llm_client.py)), so a recorded golden is guaranteed replayable; a miss is a fatal `ReplayCassetteMissError`, never a silent pass.
3. **Per-provider goldens?** A scenario recorded against Anthropic Sonnet may not replay byte-identically against OpenAI GPT — but with `match: contains` etc., it might pass anyway. Decision: one golden per scenario, recorded against the active default model ([RFC 0033](0033-model-alias-layer.md) `quality` alias); drift mode catches when model changes invalidate the golden.
4. **What is the `stable` tier admission criterion?** Initial: an eval enters `stable` when its golden has been re-verified across two independent runs and the maintainer marks it stable. Document the bar more rigorously when Phase 2 lands.
5. **Eval scenarios involving wall-clock time.** Some evals assert behavior across simulated days ([RFC 0021](0021-persona-temporal-awareness.md)). The `elapsed` field uses simulated time, not real time — the runner injects the elapsed delta into the persona temporal-awareness layer. Confirm the seam exists ([RFC 0021](0021-persona-temporal-awareness.md) §Phase 1).
6. **External-agent eval scenarios.** Once [RFC 0043](0043-inbound-agent-interop-endpoint.md) ships, an external-agent participant is a new eval-set fixture type. Defer adding the fixture shape until RFC 0043 lands.

## Decision / Next Steps

🚧 Implementing (Phase 1). Sequenced by the [RFC 0044 PR plan](0044-pr-plan.md) as the cuttable [v0.3.11 fold-in](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28). Phase 1 is the prerequisite for [RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) Phase 1 — the event-stream goldens RFC 0041 promises to verify "no silent behavior change" against do not exist until this RFC's format does.

The blocking open questions are resolved in the PR plan: **OQ #1** (goldens) → sidecar `<id>.golden.yaml`; **OQ #2** (replay cassette) → the replay-client PR (**landed** — canonicalization spelled out above); **OQ #5** (`elapsed`) → the runner PR (the schema already types the field). PR 1 landed the deterministic core — the eval-set format (`schemas/eval_set.schema.json`), the [§B](#b-assertion-vocabulary) assertion engine, and `load_eval_set` / `evaluate` — built test-first, with no dependency on the unlanded RFC 0041 taxonomy. PR 2 landed the replay client ([`evaluators/replay_llm_client.py`](../../evaluators/replay_llm_client.py)): the recorded-response `LLMProvider` + its record half, the mock-as-LLM that makes replay CI-safe. The runner and seed goldens follow in PRs 3–4.

## Related Documentation

- [RFC 0044 PR Implementation Plan](0044-pr-plan.md) — the Phase 1 PR sequence (format → replay client → runner → seed goldens)
- [Agent Runtime Vocabulary — Discussion Notes](../agent-runtime-vocabulary-roadmap.md) — the umbrella memo
- [Memory Quality Roadmap](../memory-quality-roadmap.md) — defines the dementia-test quality bar this RFC operationalizes
- [RFC 0041 — Typed Event Taxonomy and Lifecycle Callbacks](0041-typed-event-taxonomy-lifecycle-callbacks.md)
- [RFC 0042 — State Namespacing by Scope Prefix](0042-state-namespacing-by-scope.md)
- [RFC 0043 — Inbound Agent-Interop Endpoint](0043-inbound-agent-interop-endpoint.md)
- [MT-MEMORY-005 — Dementia Test](../manual-tests/MT-MEMORY-005-dementia-test.md)
- [RFC 0031 — Per-Session Namespacing](0031-per-session-namespacing-channels.md)
- [RFC 0034 — Persona Conversational Working Memory](0034-persona-conversational-working-memory.md)
- [RFC 0026 — Declarative Facts Tier](0026-declarative-facts-tier.md)
- [RFC 0021 — Persona Temporal Awareness](0021-persona-temporal-awareness.md)
- [RFC 0033 — Provider-Agnostic Model Alias Layer](0033-model-alias-layer.md)
- [ISSUE-0065](../issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md) / [ISSUE-0066](../issues/ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md)
