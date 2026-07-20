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
| [`schemas/eval_set.schema.json`](../../schemas/eval_set.schema.json) | ~156 | — | JSON, not code-gated; the recipe file shape. |

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
         ├── PR 4a: first pre-0041 seed — EVAL-MEMORY-001 recipe + offline golden
         │         (final_transcript / terminal_state only) ← no RFC 0041 dep
         │
         ├── PR 4c: driver conversation-window fetcher + EVAL-WORKING-001
         │         (RFC 0034 within-interaction working-memory seed, pre-0041) ← no RFC 0041 dep
         │
         └── PR 4b (gated on RFC 0041 P1): event-asserting seeds
                  (EVAL-ERROR-001/002 typed chat-error events);
                  + EVAL-RECALL-001 (cross-session, needs a per-interaction-session
                  recipe extension) + EVAL-FACTS-001

   Phase 2 (separate): CI gate (`stable` tier blocks merge) — deferred past v0.3.11.
```

PR 4 splits at the RFC 0041 edge: **4a** lands the seed recipes whose assertions
the pre-0041 surface already supports (`final_transcript` / `terminal_state`);
**4b** lands the event-asserting seeds once RFC 0041 emits the typed events they
reference. PRs 1–3 build the harness against the pre-0041 surface. A pre-0041 seed
whose *runtime path* the eval driver does not yet exercise lands when that seam is
built — **4c** adds the driver conversation-window fetcher so the RFC 0034
working-memory seed (`EVAL-WORKING-001`) becomes a genuine bar without RFC 0041
(the plan first grouped `WORKING` under 4b, but its assertions are transcript-only;
what it actually needed was the driver seam, not the typed-event stream). What
remains truly 4b-gated is the typed-event `EVAL-ERROR-*`; `EVAL-RECALL-001`
(cross-*session* no-leak) is deferred separately on a recipe-format extension.

## PR Sequence

### PR 1: `feature/v0311-rfc0044-eval-format` — the format + the assertion engine ✅ (this PR)

The deterministic, dependency-free core, built test-first.

#### Scope

| File | Change |
|------|--------|
| New [`evaluators/assertions.py`](../../evaluators/assertions.py) | The closed assertion vocabulary ([§B](0044-eval-set-golden-traces.md#b-assertion-vocabulary)): `contains` / `must_reference` / `must_not_reference` / `regex` / `exact` content matchers, `gt`/`lt`/`gte`/`lte`/`eq` numeric matchers, `event_count`, `event_sequence` (contiguous slice). Plus `EvalRun` (the observed-outcome type the runner produces) and `AssertionResult`. Events are opaque `{"type": …}` maps — **no dependency on the unlanded RFC 0041 taxonomy**. |
| New [`evaluators/eval_set.py`](../../evaluators/eval_set.py) | Recipe dataclasses (`EvalSet`/`Setup`/`Interaction`/`Turn`/`Assertions`/…), `load_eval_set` (schema-validated, raising `ValueError` on any malformed recipe), and `evaluate(eval_set, run) → EvalReport`. Load-time guards: the [§D](0044-eval-set-golden-traces.md#d-stochasticity-tolerance) `match: exact`-on-content ban, **operand presence** (a content operator missing/mis-keying its `value`/`values` would otherwise coalesce to a vacuously-passing assertion), and **regex compilability** (a bad pattern fails loudly at load, not mid-`evaluate`). |
| New [`evaluators/__init__.py`](../../evaluators/__init__.py) | Public API re-export. |
| New [`schemas/eval_set.schema.json`](../../schemas/eval_set.schema.json) | Draft-07 recipe file shape (`additionalProperties: false`, `EVAL-<DOMAIN>-<NNN>` id pattern, `tier` enum). |
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

### PR 2: `feature/v0311-rfc0044-replay-client` — replay LLM client ✅

A recorded-response `LLMProvider` (the [`llm_types.LLMProvider`](../../agents/llm_types.py) Protocol) that returns cassette responses keyed by a canonicalized request hash (OQ #2). Deterministic, byte-stable — the mock-as-LLM that makes replay CI-safe ([§D](0044-eval-set-golden-traces.md#d-stochasticity-tolerance)).

#### Scope

| File | Change |
|------|--------|
| New [`evaluators/replay_llm_client.py`](../../evaluators/replay_llm_client.py) | The **canonicalize → hash → look-up** core (OQ #2): `canonicalize_request` (sorted, compact, UTF-8 JSON) + `hash_request` (`hashlib.sha256`, so the digest is stable across processes — a cassette recorded once is portable to CI). Volatile keys (`DEFAULT_VOLATILE_KEYS`: `cache_control` prompt-cache markers, the opaque provider `signature`, `timestamp`/`idempotency_key`/`request_id`) are stripped at any depth before hashing, overridable via `drop_keys`. `ReplayProvider` (fail-loud `ReplayCassetteMissError` on a miss) and a `RecordingProvider` wrapper that captures the cassette with the *same* hash — so a recording is guaranteed replayable. `response_to_payload`/`payload_to_response` (YAML/JSON-safe: `stop_reason`→str, `signature`→base64) and `dump_cassette`/`load_cassette` (`.golden.yaml`, OQ #1). |
| [`evaluators/__init__.py`](../../evaluators/__init__.py) | Docstring only — the replay client is imported from its submodule, *not* re-exported here, so `import evaluators` (the pure assertion core) does not drag in the `agents` runtime that `agents.llm_types` transitively loads. |
| RFC 0044 §H OQ #2 + this plan | OQ #2 → resolved (canonicalization spelled out). |
| [`ROADMAP.md`](../../ROADMAP.md) + [`v0.3.11-plan.md`](../v0.3.11-plan.md) | RFC 0044 rows note PR 2 landed; `Last updated` refresh (concise). |

#### Tests

- [`tests/unit/python/test_eval_replay_client.py`](../../tests/unit/python/test_eval_replay_client.py) — hash stability across dict-key order; every semantic field perturbs the hash; each default volatile key does *not*; `drop_keys` override *replaces* the default set; payload round-trip incl. `signature` bytes + `stop_reason`; `ReplayProvider` hit (byte-stable across calls) / miss (`ReplayCassetteMissError` with actionable detail); Protocol conformance; cassette file dump/load + `from_file`; and the two symmetry tests — record→replay reproduces responses, incl. a multi-round tool loop whose follow-up request (built by `append_tool_round`) must re-hash identically.

#### Review fold-in

- **Provider-agnostic tool hash (symmetry fix).** The runtime pipes `format_tool_definitions()` output into `create_message(tools=…)`, and `tools` is one of the six hashed inputs. The vendor formatters rewrite the shape structurally (`parameters`→`input_schema` for Anthropic, a `{type: function}` wrapper for OpenAI), so keying the cassette on the vendor-native shape at record time while [`ReplayProvider`](../../evaluators/replay_llm_client.py) (no wrapped provider) keys on the raw shape would miss for *every tool-bearing eval* — i.e. every real persona recording, since personas always carry memory tools. Fix: both providers' `format_tool_definitions` pass tools through raw (the cassette key is provider-agnostic); `RecordingProvider.create_message` applies the inner provider's native formatting for the live call only. A regression test (transforming-fake inner) pins it red→green. `append_tool_round` stays on the shared canonical (Anthropic-block) shape for the same symmetry reason — documented, with non-Anthropic multi-round record noted out of Phase-1 scope.

#### PR checklist

- [x] Test-first (red → green); the new suite green under `make test-python`.
- [x] `ruff` + `mypy` clean over `evaluators/` and the new test.
- [x] No new runtime dependency (`pyyaml` already in the closure).
- [x] ROADMAP + RFC/plan status hygiene.
- [x] Adversarial multi-agent review; confirmed findings folded in (tool-hash symmetry + `load_cassette` edge tests).

### PR 3: `feature/v0311-rfc0044-runner` — the runner + Makefile targets + guide ✅

The orchestration half of Phase 1, built test-first: load a recipe → build the mode's provider → drive the **real** persona runtime through the replay client → `evaluate` → a structured artifact. Resolves OQ #5 (`elapsed`).

#### Scope

| File | Change |
|------|--------|
| New [`evaluators/runner.py`](../../evaluators/runner.py) | The orchestrator: `parse_elapsed` (OQ #5 — no codebase helper parses `5m`/`2h`/`1d`; the reverse `format_duration` is seconds→prose), the `PersonaDriver` Protocol seam, `run_eval` (recipe × provider × driver → `EvalReport`), `build_provider` (replay from golden / record wrapping a live provider / live drift), recipe discovery (`discover_recipes` / `golden_path_for` sidecar, OQ #1), `run_suite`, and the `python -m evaluators.runner` CLI behind the three make targets. Kept free of a module-level `agents` import (the driver + live-provider factory are lazy) so `import evaluators.runner` and the orchestration stay light and fake-driver-testable. |
| New [`evaluators/persona_driver.py`](../../evaluators/persona_driver.py) | `PersonaRuntimeDriver` — the real-runtime adapter that produces an `EvalRun`. Builds a persona agent around the injected `LLMProvider` + a `FrozenClock` (RFC 0021 seam), drives each user turn through `agent.on_event` → `extract_chat_reply`, injects each interaction's `elapsed` via `clock.advance` (OQ #5), and snapshots terminal state into the `persona:<id>:trust.scores.<peer>` key space `evaluate` compares against (seed via the config `relationships` tier). Imports the `agents` runtime, so — like the replay client — it is **not** re-exported from `evaluators/__init__`. `default_config_resolver` maps `setup.persona` → a `config/agents.yaml` entry. Events are empty pre-0041 (documented seam). |
| New [`evaluators/report.py`](../../evaluators/report.py) | Pure per-assertion → JSON artifact: `report_to_dict` (eval_id / tier / mode / per-assertion rows / roll-up), `suite_report` (`passed_all` = the Phase-2 merge-gate signal), `write_report`. |
| New [`evaluators/eval_sets/`](../../evaluators/eval_sets/) | The recipe scan home (a `README.md` documenting the empty-until-PR-4 gate). The runner treats an empty/absent dir as a clean no-op. |
| New [`docs/evaluators-guide.md`](../../docs/evaluators-guide.md) | Author guide: recipe anatomy, assertion vocabulary, `seed_state`/`terminal_state`, `elapsed`, the record/replay/drift workflow, the report-artifact shape. |
| [`Makefile`](../../Makefile) | `eval-replay` / `eval-record` / `eval-drift` targets (+ `.PHONY`), `TARGET=<id>` / `REPORT=<path>` knobs. |
| [`evaluators/__init__.py`](../../evaluators/__init__.py) | Docstring only — PR 3 modules noted as submodule-imported, not re-exported. |
| RFC 0044 §H OQ #5 + this plan | OQ #5 → resolved (the runner wires the `elapsed` delta into the `FrozenClock`). |
| [`ROADMAP.md`](../../ROADMAP.md) + [`v0.3.11-plan.md`](../v0.3.11-plan.md) + [`FILEMAP.md`](../../FILEMAP.md) | RFC 0044 rows note PR 3 landed; `Last updated` refresh (concise); FILEMAP tree + counts. |

#### Tests

- [`tests/unit/python/test_eval_runner.py`](../../tests/unit/python/test_eval_runner.py) — `parse_elapsed` units + malformed rejection; `run_eval` orchestration via a deterministic fake `PersonaDriver` (pass + fail propagation); the report artifact shape + failure counts + suite aggregation + JSON write; recipe discovery (golden-sidecar + non-yaml exclusion, `target` filter, missing-dir → `[]`); provider building (replay from a golden / missing-golden → `FileNotFoundError`); the CLI no-recipes no-op (exit 0).
- [`tests/unit/python/test_eval_persona_driver.py`](../../tests/unit/python/test_eval_persona_driver.py) — the **real** runtime end-to-end against an in-memory persona config (no disk / network / API key): ordered `turn_outputs`, seeded-trust snapshot round-trip, the `elapsed`→clock advance observed directly, events empty pre-0041, and **record → replay symmetry through the runtime** (a clean replay with no `ReplayCassetteMissError` is the proof that record and replay canonicalize the same requests through the full prompt-assembly path). Plus the config resolver (by-name + unknown → `KeyError`).

#### PR checklist

- [x] Test-first (red → green); both new suites green under `make test-python` (full python suite 4016 green).
- [x] `ruff` + `mypy` clean over `evaluators/` and the new tests; `file_size.py --strict` clean.
- [x] No new runtime dependency (`pyyaml` already in the closure; the driver rides the existing `agents` runtime).
- [x] `import evaluators` stays runtime-free (driver + runner are submodule-only).
- [x] ROADMAP + RFC/plan + FILEMAP status hygiene.

### PR 4a: `feature/v0311-rfc0044-seed-memory` — the first pre-0041 seed ✅

The dementia test ([MT-MEMORY-005](../manual-tests/MT-MEMORY-005-dementia-test.md))
as [`EVAL-MEMORY-001`](../../evaluators/eval_sets/EVAL-MEMORY-001.yaml): a
five-interaction recall recipe with an offline-recorded `.golden.yaml` sidecar
that replays green against the pre-0041 surface. Assertions are restricted to the
subset that does not need typed events (`final_transcript` / `terminal_state`).

#### Scope

| File | Change |
|------|--------|
| New [`evaluators/eval_sets/EVAL-MEMORY-001.yaml`](../../evaluators/eval_sets/EVAL-MEMORY-001.yaml) + `.golden.yaml` | The recipe + its committed golden (recorded offline against the mock). |
| New [`evaluators/eval_sets/offline_responses.eval.yaml`](../../evaluators/eval_sets/offline_responses.eval.yaml) | Curated mock replies the offline record feeds — kept separate from the demo's `config/offline_responses.yaml`. |
| [`evaluators/persona_driver.py`](../../evaluators/persona_driver.py) | **Fix:** the driver now forces an isolated `:memory:` DB (it claimed this in its docstring but used the config's file `db_path`) — required so the golden is portable (a persona's file DB carries ambient rows that shift the recalled prompt → a cassette miss) and so an eval never pollutes production memory. |
| [`Makefile`](../../Makefile) | `eval-record-offline` (deterministic mock record, $0); `eval-replay` pins the offline optimization overlay so the alias resolution matches the record (the close-summary / critic paths hash the resolved physical model). |
| [`docs/evaluators-guide.md`](../evaluators-guide.md), [`evaluators/eval_sets/README.md`](../../evaluators/eval_sets/README.md) | Seed section + the offline record/replay workflow + the offline-overlay-pins-replay caveat. |
| RFC 0044 §Decision + this plan | PR 4 split into 4a (this PR) / 4b; first seed noted landed. |

#### Tests

- [`tests/integration/test_eval_seed_replay.py`](../../tests/integration/test_eval_seed_replay.py) — the committed recipe + golden replays all-pass through the real runtime (no key/network); the golden is load-bearing (blanking a reply fails `must_reference`); a missing golden fails loud; the recipe is confirmed pre-0041 (no event assertions).
- [`tests/unit/python/test_eval_persona_driver.py`](../../tests/unit/python/test_eval_persona_driver.py) — the driver forces `:memory:` and never touches the config's file `db_path`.

#### PR checklist

- [x] Test-first (red → green): the seed-replay + driver-isolation tests fail before the seed/fix, pass after.
- [x] Golden recorded via `make eval-record-offline` (deterministic, $0, no key); replays green ×N.
- [x] `ruff` + `mypy` + `file_size.py --strict` clean; no cross-suite fixture leakage (seed test runs alongside the close-path suites).
- [x] ROADMAP + RFC/plan status hygiene.

### PR 4c: `feature/v0311-rfc0044-seed-working` — driver conversation-window fetcher + EVAL-WORKING-001 ✅ ([#748](https://github.com/mkhomutov/Persatrix/pull/748))

The second pre-0041 seed, and the driver seam it needed. `EVAL-WORKING-001` asserts
RFC 0034 **within-interaction working memory** (the ISSUE-0052 defect: the persona
references its own prior clarifying question) — orthogonal to the dementia seed's
cross-interaction recall. Empirically, the PR-3 driver drove channel-less events
with no history fetcher, so the RFC 0034 conversation window degraded to
current-event-only: a `WORKING` seed would have been a **vacuous** bar (a regression
in working memory would shift no request hash). So this PR builds the missing seam
first, then lands the seed against it — a genuine bar, all pre-0041 (working memory
is RFC 0034, already shipped; no typed events involved).

#### Scope

| File | Change |
|------|--------|
| New [`evaluators/eval_channel_history.py`](../../evaluators/eval_channel_history.py) | `InProcessChannelHistory` — a pure (no-`agents`) in-memory `ChannelHistoryFetcher`: the driver appends each delivered turn, the persona runtime fetches the window during prompt assembly (newest-first rows in the `id`/`sender_id`/`content` shape the window reads). |
| [`evaluators/persona_driver.py`](../../evaluators/persona_driver.py) | Working memory **opt-in per recipe via `setup.channel`**: with a channel the driver wires the fetcher (`set_history_fetcher`), sets `channel_id`/a deterministic `message_id` on each event, and logs the inbound turn (before dispatch, as the ordering anchor) + the persona's reply (after). No channel → the pre-window current-event-only path, **byte-identical** to before — so `EVAL-MEMORY-001`'s committed golden is untouched (verified). |
| New [`evaluators/eval_sets/EVAL-WORKING-001.yaml`](../../evaluators/eval_sets/EVAL-WORKING-001.yaml) + `.golden.yaml` | The recipe (declares `setup.channel`) + its offline-recorded golden. |
| [`evaluators/eval_sets/offline_responses.eval.yaml`](../../evaluators/eval_sets/offline_responses.eval.yaml) | Curated `ember-owl` replies for the two turns — keywords disjoint from the `EVAL-MEMORY-001` entries so neither seed shadows the other (the mock keys on the latest user message only). |
| [`docs/evaluators-guide.md`](../evaluators-guide.md), [`evaluators/eval_sets/README.md`](../../evaluators/eval_sets/README.md) | The `setup.channel` working-memory seam + the second seed. |
| RFC 0044 pr-plan (this file) | PR 4 re-split: `WORKING` was never event-gated — it needed the driver seam, not RFC 0041 (4c); 4b narrows to `EVAL-ERROR-*`; `EVAL-RECALL-001` deferred on a recipe-format extension. |

#### Tests

- [`tests/unit/python/test_eval_channel_history.py`](../../tests/unit/python/test_eval_channel_history.py) — the fetcher contract: newest-first, `limit` cap, channel isolation, `[]` (never `None`) on empty, `as_participant` accepted-and-ignored, Protocol conformance.
- [`tests/unit/python/test_eval_persona_driver.py`](../../tests/unit/python/test_eval_persona_driver.py) — with `setup.channel`, turn 2's `messages` carries turn 1's user message **and the persona's own reply** (working memory engaged); without a channel, every turn is current-event-only (the gate that keeps `EVAL-MEMORY-001` unchanged).
- [`tests/integration/test_eval_working_seed_replay.py`](../../tests/integration/test_eval_working_seed_replay.py) — the committed recipe + golden replays all-pass; pre-0041 subset; assertion load-bearing; and **working-memory load-bearing** — replaying the committed golden against a channel-stripped recipe goes red (turn 2 loses the window → cassette miss).

#### PR checklist

- [x] Test-first (red → green); the fetcher / driver-engagement / seed-replay suites fail before the seam + seed, pass after.
- [x] `EVAL-MEMORY-001` golden verified byte-identical (channel-less path unchanged); offline re-record of the new seed byte-deterministic.
- [x] Golden recorded via `make eval-record-offline` (deterministic, $0, no key); replays green ×N.
- [x] `ruff` + `mypy` + `file_size.py --strict` clean over `evaluators/` + the new tests.
- [x] `import evaluators` stays runtime-free (the fetcher is pure; the driver is submodule-only).
- [x] ROADMAP + RFC/plan + FILEMAP status hygiene (RFC 0044 rides as infrastructure — no per-PR CHANGELOG entry, per PRs 1–4a; the release CHANGELOG carries one consolidated fold-in entry, added at v0.3.11 curation the way RFC 0045's was in v0.3.10).

### PR 4b (gated on RFC 0041 Phase 1): event-asserting seeds + remaining recipes

The event-asserting seeds ([§E](0044-eval-set-golden-traces.md#e-seed-eval-sets)) —
`EVAL-ERROR-001`/`002` (typed chat-error events) — with recorded `.golden.yaml`
sidecars, once RFC 0041 emits the typed events they assert on. `EVAL-FACTS-001`
(declarative-facts recall) rides here too. `EVAL-RECALL-001` (cross-*session*
no-leak) is a separate deferral: its assertions are transcript-only (not
event-gated), but the single-session recipe format cannot yet express two sessions
— it lands once a per-interaction-session extension does. (`EVAL-WORKING-001`
landed pre-0041 in PR 4c — see above.)

## Notes

- **Phase 2 (CI gating) is out of v0.3.11 scope.** Phase 1 ships a runner whose report a human reads; a failed eval does not block merge until Phase 2 wires `.github/workflows/eval.yml`.
- **Per-recipe replay overlay (follow-up, untracked elsewhere).** `make eval-replay` pins the offline optimization overlay for *every* target (PR 4a), so it replays only goldens recorded under that overlay. The release-prep live re-record of `EVAL-MEMORY-001` produces a golden that bakes in the real physical models (the RFC 0020 close-summary / RFC 0051 critic paths hash the *resolved physical* model, not the env-independent alias), so replaying it needs the target to resolve the overlay per recipe rather than pinning offline. Parked here until a live golden lands — it is neither a 4b (RFC-0041 event seeds) nor a Phase-2 (CI gate) item.
- **`evaluators/` lint strictness.** PR 1 folds `evaluators/` into the root `tests/` ruff+mypy invocation for a single config surface. A dedicated stricter mypy profile (matching `agents/` `warn_return_any`) is a candidate follow-up if the harness grows.
