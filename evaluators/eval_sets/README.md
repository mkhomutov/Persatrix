# `evaluators/eval_sets/` — golden-trace eval recipes (RFC 0044)

This directory holds the golden-trace eval **recipes** (`<id>.yaml`) and their
recorded **golden** sidecars (`<id>.golden.yaml`, OQ #1) that the runner
(`python -m evaluators.runner`, `make eval-replay`) discovers and replays.

## Landed seeds

| Recipe | Source | Asserts (pre-0041 subset) |
|--------|--------|---------------------------|
| [`EVAL-MEMORY-001`](EVAL-MEMORY-001.yaml) | [MT-MEMORY-005](../../docs/manual-tests/MT-MEMORY-005-dementia-test.md) | Dementia-test recall across five interactions — `final_transcript` + `terminal_state` |

`EVAL-MEMORY-001` is the **first pre-0041 seed** (RFC 0044 PR 4). Its golden is
recorded offline against the mock provider, so it replays deterministically at
$0 with no API key — the [seed replay test](../../tests/integration/test_eval_seed_replay.py)
runs it in CI on every PR, so a replay regression fails the build. (The eval
harness's own *tiered* merge gate — `passed_all` blocking the `stable` tier — is
the separate Phase-2 step still deferred.)

## Still gated on RFC 0041

The remaining seed recipes ([RFC 0044 §E][seed]) — `EVAL-ERROR-001` / `002`
(typed chat-error **events**) and the rest — assert over the typed event stream
that [RFC 0041][0041] Phase 1 has not yet landed. They follow in a **4b** slice
once RFC 0041 emits those events. The Phase-1 runner reports an empty event
stream, so a recipe that lands now must assert only over `final_transcript` /
`terminal_state`.

## Re-recording a seed golden

```
make eval-record-offline TARGET=EVAL-MEMORY-001   # deterministic, $0, no key
make eval-replay        TARGET=EVAL-MEMORY-001   # verify it replays green
```

`eval-record-offline` records against the mock (the offline optimization overlay
+ the curated [`offline_responses.eval.yaml`](offline_responses.eval.yaml)); a
live re-record against the real `quality` model rides release-prep. See the
[evaluators guide](../../docs/evaluators-guide.md) for the recipe format, the
assertion vocabulary, and the record/replay/drift workflow.

[seed]: ../../docs/rfcs/0044-eval-set-golden-traces.md#e-seed-eval-sets
[0041]: ../../docs/rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md
