# Golden-Trace Eval Harness — Author Guide (RFC 0044)

## What this is

The golden-trace eval harness is Persatrix's **automated regression bar for
persona quality**. You write a *recipe* — a short, reproducible conversation with
a persona and a set of checks on how it responds — and the harness runs it and
tells you, check by check, whether the persona still behaves the way it should.

It matters most for [autonomous channels](rfcs/0052-autonomous-agent-channels.md):
there is no human watching a machine-only conversation, so an automated bar is the
only thing that catches a persona that quietly started forgetting names or leaking
another session's memory.

This guide covers **Phase 1** (v0.3.11): the recipe format, the assertion
vocabulary, and the replay/record/drift workflow. Phase 1 produces a report a
human reads — a failed eval does **not** block merge yet (that is Phase 2). See
[RFC 0044](rfcs/0044-eval-set-golden-traces.md) for the full design.

## How replay stays deterministic

An LLM's output is not byte-stable, so you cannot assert on exact text and you
cannot re-run a live model in CI and expect the same answer twice. The harness
solves this by **recording once and replaying**:

- **Record** runs the scenario against a real model and saves every model
  response, keyed by a hash of the request, into a `<id>.golden.yaml` cassette.
- **Replay** re-runs the scenario with a [recorded-response
  client](../evaluators/replay_llm_client.py) that returns those saved responses
  instead of calling a model. The run is byte-stable and free, so it is safe to
  run on every change.

Because assertions check *structure* (mentions "Alice", never says "I don't
recall") rather than exact tokens, one golden keeps passing across small,
legitimate wording changes — and drift mode (below) catches when the model has
changed enough that the golden no longer reflects reality.

## Quick start

```bash
make eval-replay                      # replay every recipe in evaluators/eval_sets/
make eval-replay TARGET=EVAL-MEMORY-001   # just one
make eval-replay REPORT=out.json      # also write the structured JSON artifact
make eval-record TARGET=EVAL-MEMORY-001   # (authors) record/refresh its golden
make eval-drift                       # live run; report drift, never gate
```

`evaluators/eval_sets/` is empty in v0.3.11 — the seed recipes land in
[RFC 0044 PR 4](rfcs/0044-pr-plan.md), gated on
[RFC 0041](rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md) typed events —
so `make eval-replay` is a clean no-op today.

## Recipe anatomy

A recipe is one YAML file, validated against
[`schemas/eval_set.schema.json`](../schemas/eval_set.schema.json). Its id follows
`EVAL-<DOMAIN>-<NNN>` where `<DOMAIN>` is one of `MEMORY`, `RECALL`, `ERROR`,
`WORKING`, `FACTS`.

```yaml
id: EVAL-MEMORY-001
title: Recall across five interactions
tier: stable                     # stable | experimental | nightly (Phase 2 gates `stable`)

setup:
  persona: ember-owl             # resolved against config/agents.yaml
  user: alice                    # the speaker the turns come from
  seed_state:                    # scope-prefixed state seeded before the run
    persona:ember-owl:trust.scores.alice: 0.0
  session_id: EVAL-MEMORY-001-S
  llm_mode: replay               # replay (default, CI-safe) | live

interactions:
  - id: i1
    turns:
      - user: "Hi Ember — I'm Alice, I work on the data-platform team."
      - assistant: {match: contains, value: "Alice"}
  - id: i2
    elapsed: 5m                   # simulated time since i1 (never real wall-clock)
    turns:
      - user: "What do you remember about me?"
      - assistant: {match: must_reference, values: ["Alice", "data-platform"]}

assertions:
  terminal_state:
    persona:ember-owl:trust.scores.alice: {match: gt, value: 0.0}
  final_transcript:
    must_reference: ["Alice", "data-platform"]
    must_not_reference: ["I don't recall"]
```

Each interaction is a `user` turn followed by the `assistant` turn that carries
your expectation for the reply. The runner drives each user turn through the real
persona runtime and lines the assistant expectations up with the replies, in
order.

## Assertion vocabulary

The vocabulary is **closed** — adding an operator is an RFC amendment, not a
routine change ([RFC 0044 §B](rfcs/0044-eval-set-golden-traces.md#b-assertion-vocabulary)).

| Where | Operators |
|-------|-----------|
| Assistant turn / `final_transcript` (text) | `contains`, `must_reference` (all-of), `must_not_reference` (none-of), `regex` |
| `terminal_state` (per key) | `exact`, `gt`, `lt`, `gte`, `lte`, `eq` |
| Event stream (top-level) | `event_count`, `event_sequence` |

`match: exact` is **rejected on assistant/transcript text** at load time — LLM
output is not byte-stable, so an exact-text check would be a flaky regression bar
(RFC 0044 §D). Use it only for state values. Every operator must carry its
operand (`contains` needs `value`, `must_reference` needs `values`) — an empty
operand is rejected so a check can never vacuously pass.

## State: `seed_state` and `terminal_state`

State keys are **scope-prefixed**: `persona:<id>:<path>`. Phase 1 supports the
**trust** family, `persona:<id>:trust.scores.<peer>`, which maps to the persona's
relationship trust for that peer:

- **`seed_state`** sets the starting value before the run (seeded as an absolute
  trust row).
- **`terminal_state`** asserts on the value after the run.

Other state families do not have a runtime seam yet; a recipe that seeds one gets
a logged warning and the key is skipped, so it is visibly unsupported rather than
silently ignored. More families are wired as recipes come to need them.

## `elapsed` — simulated time

`elapsed` (e.g. `5m`, `2h`, `1d`) is **simulated** time between interactions, never
real wall-clock. The runner advances a frozen clock by that delta before the next
interaction, so the persona's temporal awareness
([RFC 0021](rfcs/0021-persona-temporal-awareness.md)) sees "5 minutes later"
deterministically. This is also what keeps a golden's time-anchored prompt stable
enough to replay byte-for-byte.

## Record → replay → drift

- **`make eval-record TARGET=<id>`** — runs the scenario against a live model and
  writes/overwrites the `<id>.golden.yaml` sidecar. Authors run this deliberately;
  **CI never overwrites a golden.**
- **`make eval-record-offline TARGET=<id>`** — the Phase-1 seed path: records the
  golden against the **mock** provider deterministically, at $0 with no API key
  (the offline optimization overlay points `quality` at the mock, and the curated
  [`offline_responses.eval.yaml`](../evaluators/eval_sets/offline_responses.eval.yaml)
  feeds the replies). This is how the landed seed goldens are produced; a live
  re-record (`eval-record`) against the real model rides release-prep.
- **`make eval-replay`** — the CI-safe path: replays the golden and reports each
  assertion pass/fail. A request with no recorded response is a hard failure
  (`ReplayCassetteMissError`), never a silent pass — it means the recipe drifted
  from the golden, so re-record.
- **`make eval-drift`** — runs live and surfaces where the new run diverges from
  the golden. Drift is informational: it tells you "the model or the prompt
  changed", it does not gate merge or auto-update the golden.

> **Offline goldens replay under the offline overlay.** `make eval-replay` pins
> `PERSATRIX_OPTIMIZATION_CONFIG` to the offline overlay. The action loop hashes
> the raw model *alias* (`quality`), but the RFC 0020 close-summary and RFC 0051
> critic paths hash the *resolved physical* model — so a golden recorded offline
> must resolve the same aliases at replay, or those requests miss the cassette.
>
> Because the target pins the offline overlay for *every* recipe, it replays only
> goldens recorded under that overlay. The release-prep live re-record bakes in the
> real physical models and would miss the cassette here until `make eval-replay`
> resolves the overlay per recipe — a follow-up parked in the
> [PR plan](rfcs/0044-pr-plan.md) (§Notes).

## The report artifact

The runner emits a structured, per-assertion JSON artifact (RFC 0044 §F) — the
shape Phase 2 will attach to the CI run and gate the `stable` tier on:

```json
{
  "evals": [
    {
      "eval_id": "EVAL-MEMORY-001",
      "tier": "stable",
      "mode": "replay",
      "passed": false,
      "summary": {"total": 4, "passed": 3, "failed": 1},
      "assertions": [
        {"name": "turn[0].contains", "passed": true, "detail": ""},
        {"name": "final_transcript.must_reference", "passed": true, "detail": ""},
        {"name": "terminal_state.persona:ember-owl:trust.scores.alice",
         "passed": false, "detail": "expected gt 0.0, got 0.0"}
      ]
    }
  ],
  "summary": {"evals": 1, "passed": 0, "failed": 1, "passed_all": false}
}
```

`passed_all` is the single merge-gate signal Phase 2 reads.

## Phase 1 limits

- **No event assertions yet.** `event_count` / `event_sequence` parse and load,
  but the runtime has no capturable typed-event stream until
  [RFC 0041](rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md) lands, so the
  event stream is empty and those assertions run against nothing. Phase 1 recipes
  assert on `final_transcript` and `terminal_state` only.
- **No harness merge gate.** The eval harness's `passed_all` / tier gate does not
  block merge until Phase 2. (A committed seed's own integration test still fails
  CI on a replay regression — that is an ordinary test, not the harness gate.)

## Seed recipes

The first pre-0041 seed has landed:
[`EVAL-MEMORY-001`](../evaluators/eval_sets/EVAL-MEMORY-001.yaml) — the dementia
test ([MT-MEMORY-005](manual-tests/MT-MEMORY-005-dementia-test.md)) as a
five-interaction recall recipe, asserting only over `final_transcript` /
`terminal_state`. Its offline golden replays green on every PR via
[`test_eval_seed_replay.py`](../tests/integration/test_eval_seed_replay.py).

Because its golden is mock-recorded, the recorded replies are curated stand-ins,
not a live model's — so the assertions on reply *content* verify the golden is
well-formed, while the load-bearing regression signal is the **request hashes**:
a change in the memory-recall → prompt-assembly path shifts a request, misses the
cassette, and fails replay. The genuine-recall (live-model) bar rides the
release-prep re-record. The remaining seeds — the typed-event `EVAL-ERROR-*` and
the rest of [§E](rfcs/0044-eval-set-golden-traces.md#e-seed-eval-sets) — land in a
**4b** slice gated on RFC 0041.

## Related

- [RFC 0044 — Eval-Set Shape with Golden Traces](rfcs/0044-eval-set-golden-traces.md)
- [RFC 0044 PR Plan](rfcs/0044-pr-plan.md)
- [Recipe schema](../schemas/eval_set.schema.json)
