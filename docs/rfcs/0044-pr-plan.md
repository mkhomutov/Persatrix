# RFC 0044 — PR Implementation Plan (Phase 1 — v0.3.11 fold-in)

> Owning RFC: [0044-eval-set-golden-traces.md](0044-eval-set-golden-traces.md) · Version plan: [v0.3.11-plan.md](../v0.3.11-plan.md) (rides as the [cuttable safety-net fold-in](../v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28), the way RFC 0045 rode v0.3.10).

## Overview

RFC 0044 Phase 1 is **format + replay-only**: the eval-set file shape, the closed assertion grammar, and the replay runner — *no* CI gating, *no* recorded goldens (the seed goldens in [RFC 0044 §E](0044-eval-set-golden-traces.md#e-seed-eval-sets) can only be recorded once [RFC 0041](0041-typed-event-taxonomy-lifecycle-callbacks.md) Phase 1 emits the typed events their assertions reference). This plan slices Phase 1 into small, independently-reviewable PRs. The two v0.3.11 headline RFCs (0052/0053) are already Implemented; this fold-in is the last implementation work before release-prep and is **droppable without touching the autonomous-channel headline**.

Because autonomous channels (RFC 0052) run with **no human to catch a bad conversation**, an automated regression bar matters more here than anywhere prior — but it carries no competing user story, so it lands as infrastructure.

### Open-question resolutions locked at plan-authoring time

Per [RFC 0044 §Decision](0044-eval-set-golden-traces.md#decision--next-steps), OQ 1/2/5 must be resolved before Phase 1 begins:

- **OQ #1 (golden location) → sidecar `<id>.golden.yaml`** next to the recipe. Easier to review the recipe/golden pair together; a recipe is the reproducible input, the sidecar is the recorded expected output ([§C](0044-eval-set-golden-traces.md#c-recording-vs-replay)). Landed as a schema/docs decision in PR 1.
- **OQ #2 (replay-cassette shape) → PR 2** — the recorded-response format `{request_hash: response_payload}` with a volatile-field-stripping canonicalization is the replay LLM client's concern, not the format's; resolved when that client lands.
- **OQ #5 (simulated `elapsed`) → PR 3** — the runner injects the `elapsed` delta into the RFC 0021 persona temporal-awareness seam; the schema already types the field (`^[0-9]+(s|m|h|d)$`), the runner wires it.
- **Field rename `target_branch` → `tier`.** The draft RFC §A labelled the eval-tier field `target_branch`, which reads misleadingly as a git branch. The schema names it `tier` (values `stable | experimental | nightly` per [§F](0044-eval-set-golden-traces.md#f-ci-integration)); the RFC §A example is updated to match.

### File-size constraints (cap = 500 per [`file_size.py --strict`](../../scripts/checks/file_size.py))

| File | Lines (PR 1) | Headroom | Routing |
|------|-------------|----------|---------|
| [`evaluators/assertions.py`](../../evaluators/assertions.py) | ~210 | ample | The pure matcher vocabulary — no loader / runner coupling. |
| [`evaluators/eval_set.py`](../../evaluators/eval_set.py) | ~290 | ample | Recipe dataclasses + loader + `evaluate`. If the runner PR grows this, split the loader from `evaluate`. |
| [`schemas/eval_set.json`](../../schemas/eval_set.json) | ~156 | — | JSON, not code-gated; the recipe file shape. |

## Dependency Graph

```
RFC 0052 + 0053 Implemented (v0.3.11 headline)
   │
   └── PR 1 (this PR): eval-set format + assertion engine   ← no external deps
         │
         ├── PR 2: replay LLM client (recorded-response cassette, OQ #2)
         │     │
         │     └── PR 3: eval runner (recipe → EvalRun) + `make eval-replay`/`eval-record`/`eval-drift`
         │                + evaluators-guide + `elapsed` temporal seam (OQ #5)
         │
         └── PR 4 (gated on RFC 0041 P1): seed recipes + recorded `.golden.yaml` sidecars
                  (EVAL-MEMORY-001, EVAL-RECALL-001, EVAL-ERROR-001/002, EVAL-WORKING-001, EVAL-FACTS-001)

   Phase 2 (separate): CI gate (`stable` tier blocks merge) — deferred past v0.3.11.
```

Only PR 4 has a hard cross-RFC edge (RFC 0041 typed events). PRs 1–3 build the harness against the pre-0041 surface using the assertion subset that does not require typed events (`final_transcript`, `terminal_state`).

## PR Sequence

### PR 1: `feature/v0311-rfc0044-eval-format` — the format + the assertion engine ✅ (this PR)

The deterministic, dependency-free core, built test-first.

#### Scope

| File | Change |
|------|--------|
| New [`evaluators/assertions.py`](../../evaluators/assertions.py) | The closed assertion vocabulary ([§B](0044-eval-set-golden-traces.md#b-assertion-vocabulary)): `contains` / `must_reference` / `must_not_reference` / `regex` / `exact` content matchers, `gt`/`lt`/`gte`/`lte`/`eq` numeric matchers, `event_count`, `event_sequence` (contiguous slice). Plus `EvalRun` (the observed-outcome type the runner produces) and `AssertionResult`. Events are opaque `{"type": …}` maps — **no dependency on the unlanded RFC 0041 taxonomy**. |
| New [`evaluators/eval_set.py`](../../evaluators/eval_set.py) | Recipe dataclasses (`EvalSet`/`Setup`/`Interaction`/`Turn`/`Assertions`/…), `load_eval_set` (schema-validated, raising `ValueError` on any malformed recipe), and `evaluate(eval_set, run) → EvalReport`. Load-time guards: the [§D](0044-eval-set-golden-traces.md#d-stochasticity-tolerance) `match: exact`-on-content ban, **operand presence** (a content operator missing/mis-keying its `value`/`values` would otherwise coalesce to a vacuously-passing assertion), and **regex compilability** (a bad pattern fails loudly at load, not mid-`evaluate`). |
| New [`evaluators/__init__.py`](../../evaluators/__init__.py) | Public API re-export. |
| New [`schemas/eval_set.json`](../../schemas/eval_set.json) | Draft-07 recipe file shape (`additionalProperties: false`, `EVAL-<DOMAIN>-<NNN>` id pattern, `tier` enum). |
| [`Makefile`](../../Makefile) | `lint-python` runs root ruff + mypy over the new `evaluators/` tree (a repo-root package, like `tests/`). |
| RFC 0044 front-matter + bold header | `status: draft → implementing`; Phase 1 target → v0.3.11; §A `target_branch → tier`. |
| [`ROADMAP.md`](../../ROADMAP.md) + [`v0.3.11-plan.md`](../v0.3.11-plan.md) | RFC 0044 Master-Index row → 🚧 Implementing (target v0.3.11); Master-Progress row 3 → 🔄 In progress; `Last updated` refresh (kept concise). |

#### Tests

- [`tests/unit/python/test_eval_assertions.py`](../../tests/unit/python/test_eval_assertions.py) — every matcher on a passing **and** a failing input; numeric boundary (`gte`/`lte` vs `gt`/`lt`); non-numeric-actual graceful failure; event count/sequence.
- [`tests/unit/python/test_eval_set_loader.py`](../../tests/unit/python/test_eval_set_loader.py) — valid recipe round-trip; schema rejection (missing/ malformed `id`, unknown `llm_mode`, unknown top-level key); the §D `exact`-on-content rejection (and `exact`-on-state allowance); `evaluate` all-pass; the [RFC Test-Strategy self-test](0044-eval-set-golden-traces.md#test-strategy) (flip one observed value → exactly one assertion fails).

#### PR checklist

- [x] Test-first (red → green); `make test-python` green for the two new suites.
- [x] `ruff` + `mypy` clean over `evaluators/` and the new tests.
- [x] No new runtime dependency (`jsonschema` + `pyyaml` already in the closure).
- [x] ROADMAP + RFC status hygiene.

### PR 2: `feature/v0311-rfc0044-replay-client` — replay LLM client

A recorded-response `LLMProvider` (the [`llm_types.LLMProvider`](../../agents/llm_types.py) Protocol) that returns cassette responses keyed by a canonicalized request hash (OQ #2). Deterministic, byte-stable — the mock-as-LLM that makes replay CI-safe ([§D](0044-eval-set-golden-traces.md#d-stochasticity-tolerance)).

### PR 3: `feature/v0311-rfc0044-runner` — the runner + Makefile targets + guide

`evaluators/runner.py` drives a recipe's interactions/turns against the replay client to produce an `EvalRun`, then calls `evaluate`. `make eval-replay` / `eval-record` / `eval-drift` ([§C](0044-eval-set-golden-traces.md#c-recording-vs-replay)); `docs/evaluators-guide.md`; the `elapsed` → RFC 0021 temporal seam (OQ #5). A structured per-assertion report artifact.

### PR 4 (gated on RFC 0041 Phase 1): seed recipes + goldens

The six seed recipes ([§E](0044-eval-set-golden-traces.md#e-seed-eval-sets)) with recorded `.golden.yaml` sidecars, once RFC 0041 emits the typed events they assert on. Until then, recipes carrying only `final_transcript`/`terminal_state` assertions can land and replay against the pre-0041 surface.

## Notes

- **Phase 2 (CI gating) is out of v0.3.11 scope.** Phase 1 ships a runner whose report a human reads; a failed eval does not block merge until Phase 2 wires `.github/workflows/eval.yml`.
- **`evaluators/` lint strictness.** PR 1 folds `evaluators/` into the root `tests/` ruff+mypy invocation for a single config surface. A dedicated stricter mypy profile (matching `agents/` `warn_return_any`) is a candidate follow-up if the harness grows.
