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

### PR 4 (gated on RFC 0041 Phase 1): seed recipes + goldens

The six seed recipes ([§E](0044-eval-set-golden-traces.md#e-seed-eval-sets)) with recorded `.golden.yaml` sidecars, once RFC 0041 emits the typed events they assert on. Until then, recipes carrying only `final_transcript`/`terminal_state` assertions can land and replay against the pre-0041 surface.

## Notes

- **Phase 2 (CI gating) is out of v0.3.11 scope.** Phase 1 ships a runner whose report a human reads; a failed eval does not block merge until Phase 2 wires `.github/workflows/eval.yml`.
- **`evaluators/` lint strictness.** PR 1 folds `evaluators/` into the root `tests/` ruff+mypy invocation for a single config surface. A dedicated stricter mypy profile (matching `agents/` `warn_return_any`) is a candidate follow-up if the harness grows.
