# `evaluators/eval_sets/` — golden-trace eval recipes (RFC 0044)

This directory holds the golden-trace eval **recipes** (`<id>.yaml`) and their
recorded **golden** sidecars (`<id>.golden.yaml`, OQ #1) that the runner
(`python -m evaluators.runner`, `make eval-replay`) discovers and replays.

It is **empty in v0.3.11 (RFC 0044 Phase 1)**. Phase 1 shipped the format, the
assertion engine, the replay LLM client, and the runner — but **not** the seed
recipes/goldens: the six seed evals ([RFC 0044 §E][seed]) assert over the typed
event stream that [RFC 0041][0041] Phase 1 has not yet landed. The runner treats
an empty directory as a clean no-op, so `make eval-replay` runs today and does
nothing.

Seed recipes land in **RFC 0044 PR 4** (recipes carrying only
`final_transcript` / `terminal_state` assertions can land first, replaying against
the pre-0041 surface; event-asserting recipes follow the RFC 0041 events).

See the [evaluators guide](../../docs/evaluators-guide.md) for the recipe format,
the assertion vocabulary, and the record/replay/drift workflow.

[seed]: ../../docs/rfcs/0044-eval-set-golden-traces.md#e-seed-eval-sets
[0041]: ../../docs/rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md
