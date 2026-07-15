# `evaluators/eval_sets/` — golden-trace eval recipes (RFC 0044)

This directory holds the golden-trace eval **recipes** (`<id>.yaml`) and their
recorded **golden** sidecars (`<id>.golden.yaml`, OQ #1) that the runner
(`python -m evaluators.runner`, `make eval-replay`) discovers and replays.

## Landed seeds

| Recipe | Source | Asserts (pre-0041 subset) |
|--------|--------|---------------------------|
| [`EVAL-MEMORY-001`](EVAL-MEMORY-001.yaml) | [MT-MEMORY-005](../../docs/manual-tests/MT-MEMORY-005-dementia-test.md) | Dementia-test recall across five interactions — `final_transcript` + `terminal_state` |
| [`EVAL-WORKING-001`](EVAL-WORKING-001.yaml) | [RFC 0034](../../docs/rfcs/0034-persona-conversational-working-memory.md) | Working memory — the persona references its own prior in-interaction question — `final_transcript` + `terminal_state` |

Both goldens are recorded offline against the mock provider, so they replay
deterministically at $0 with no API key — the seed replay tests
([memory](../../tests/integration/test_eval_seed_replay.py),
[working](../../tests/integration/test_eval_working_seed_replay.py)) run in CI on
every PR, so a replay regression fails the build. (The eval harness's own *tiered*
merge gate — `passed_all` blocking the `stable` tier — is the separate Phase-2
step still deferred.)

`EVAL-MEMORY-001` exercises cross-interaction long-term recall; `EVAL-WORKING-001`
exercises **within-interaction working memory** (RFC 0034) — a distinct runtime
path (the conversation window, not the memory-recall tiers).

### The `setup.channel` seam (RFC 0034 working memory)

The conversation window reconstructs the in-channel transcript from a
`ChannelHistoryFetcher`. A recipe that declares **`setup.channel`** opts into it:
the driver wires an [`InProcessChannelHistory`](../eval_channel_history.py) for
that channel and logs each turn, so the persona's prompt carries its own prior
turns. A recipe with no channel (e.g. `EVAL-MEMORY-001`) stays on the
current-event-only path, byte-identical to before the seam — so `EVAL-WORKING-001`
lit up working memory without disturbing the dementia golden. The
`EVAL-WORKING-001` golden is load-bearing on this: strip the channel and the
replay goes red (turn 2's request loses the window and misses the cassette).

## Still gated on RFC 0041

The event-asserting seed recipes ([RFC 0044 §E][seed]) — `EVAL-ERROR-001` / `002`
(typed chat-error **events**) — assert over the typed event stream that
[RFC 0041][0041] Phase 1 has not yet landed. They follow in a **4b** slice once
RFC 0041 emits those events. The Phase-1 runner reports an empty event stream, so
a recipe that lands now must assert only over `final_transcript` /
`terminal_state`. (`EVAL-RECALL-001` — cross-*session* no-leak — is also still
out: it needs a per-interaction-session recipe extension the single-session format
does not yet express.)

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
