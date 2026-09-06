# Testing Strategy

> **Last updated**: 2026-09-06
> Every test layer in the repository, what it proves, where it runs, and how
> to add to it. Counts are from `git ls-files` on 2026-09-06 and will drift;
> the layers and rules will not.

## The one rule that history paid for

**Every test tree has a named runner, and the runner is listed here.** Three
layers of this repository were lint-clean and type-clean while no job ran them
— the `agents/tests/` tree for 180 days
([#848](https://github.com/mkhomutov/Persatrix/pull/848)), the Rust suite until
a knob shipped with a red lockstep guard
([#813](https://github.com/mkhomutov/Persatrix/pull/813) F-2), and the full
integration tier until a config change broke its close-path tests on `main`
([ISSUE-0076](../issues/ISSUE-0076-full-integration-suite-not-run-in-ci.md)) —
and a fourth, the Go integration tests, was found by the audit that produced
this document and wired into CI in the same series. A
green lint is not a running tree. When a new test directory is created, the
same PR adds its `make` target and its CI step, and adds a row to the table
below.

---

## The layers

| # | Layer | What it proves | Where it lives | Runner | CI |
|---|-------|----------------|----------------|--------|----|
| 1 | Go unit | Orchestrator packages behave in isolation; races (`-race`) | `internal/**/*_test.go` (309 files, 22 packages) | `make test-go` | `go` job |
| 2 | Python unit, root tree | Agent-runtime modules, mirrored per source module | `tests/unit/python/test_<module>.py` (347 files) | `make test-python` (~5.5 min) | `python` job |
| 3 | Python unit, agents tree | Component tests that need agent fixtures; the **only** executable coverage of `observability/tracing.py`, `grpc_logging.py`, `memory/scheduled_wakes.py` | `agents/tests/` (46 files) | `make test-agents` | `python` job (since #848) |
| 4 | Python integration | Assembled pieces: close path, channels, memory scoping, catch-up replay, confidentiality, delegation | `tests/integration/test_*.py` (73 files) + `_*_helpers.py` | `make test-integration` | `python` job (since ISSUE-0076) |
| 5 | Go integration | Scheduler → executor → mock agent over bufconn; rate limiter; audit logger | `tests/integration/*_test.go` (3 files) | `go test ./tests/integration/... -race` | `go` job (since the CI-promotion PR) |
| 6 | Rust | CLI parsing, output, and the CLI↔server **lockstep guards** (knob set and wire types parsed out of the Go sources) | inline `#[cfg(test)]` modules (24 files) | `cd cli && cargo test` | `rust` job (since #813) |
| 7 | Web console | Svelte components and stores under jsdom | `web/src/**/*.test.js` (30 files) | `make ui-test` (Vitest) | `web-console` job |
| 8 | Golden-trace evals | Persona-quality regressions replayed deterministically against recorded goldens ([RFC 0044](../rfcs/0044-eval-set-golden-traces.md)) | `evaluators/eval_sets/*.yaml` + `.golden.yaml` (5 recipes) | `make eval-replay` | none (Phase 2 CI gate slotted v0.3.16, cuttable) |
| 9 | Cost regression | The bored-persona gate: an idle persona spends nothing ([RFC 0024](../rfcs/0024-event-driven-scheduling.md)) | `tests/integration/test_bored_persona_cost.py` | direct pytest | `cost-regression-gate` job, path-filtered on wake-path files |
| 10 | Perf | Personal-tier recall p99/p50 against a committed baseline ([RFC 0029](../rfcs/0029-personal-society-storage-split.md)) | `tests/perf/personal_tier_latency.py` | direct | `python` job, **informational** — no baseline captured yet |
| 11 | Manual tests + live arc | The release gate: behaviour on a real provider, evidenced verbatim | `docs/manual-tests/MT-*.md` (73) + `vX.Y.Z-execution-report.md` (20); drivers in `scripts/manual_tests/` | per release, paid, on host | never |
| 12 | Offline smoke | The whole stack round-trips at $0 on the mock provider | `make demo-autonomous` / `make demo-offline` | Docker | never (release-prep PR 1 and PR 4) |
| 13 | Structural gates | The repository's own invariants: proto sync, import direction, sizes, doc links, generated indexes | `scripts/checks/`, `make *-check` | see [enforcement matrix](enforcement-matrix.md) | mixed |

`make test` runs layers **1–4 only**. Rust, web, evals, and Go integration are
separate commands. Release checklists enumerate every target rather than
leaning on `make test` reading as comprehensive.

---

## Test-driven development

From v0.3.0, new unit-level code follows red-green-refactor: a failing test
first, confirmed failing; the minimum implementation; then refactor
([copilot-instructions §TDD](../../.github/copilot-instructions.md#tdd-from-v030-onward), per-language
rules in `.github/instructions/`). Integration tests (layers 4–5) are exempt
and are written after the unit layer validates the pieces. TDD is a
convention with no automated evidence trail; the review checks for it by
reading the PR's commit order and test-first shape.

Per language:

- **Go** — `_test.go` beside the source, `package foo_test` unless unexported
  access is needed; table-driven; testify `assert`/`require`; no real network.
- **Python** — `tests/unit/python/test_<module>.py` mirrors `agents/<module>.py`;
  mock `LLMClient` at the boundary; `asyncio_mode = "auto"`; `clear_registry()`
  autouse fixture for tool tests.
- **Rust** — inline `#[cfg(test)]`; mock HTTP rather than call a server.
- **Web** — `@testing-library/svelte` under jsdom; the suite pins roles and
  names, not layout.

---

## How the Python trees are wired

- **Two unit trees, both CI-gated.** `tests/unit/python/` is the bulk;
  `agents/tests/` holds component tests that need agent fixtures. Add to
  either; both run. The pyproject comment that once said tests live only in
  the root tree was scaffold boilerplate and was corrected at #848.
- **Root `conftest.py`** puts `agents/generated/` on `sys.path` (protoc stubs
  use bare imports), imports `_test_infra`, and daemonises aiosqlite worker
  threads so a leaked connection cannot hang interpreter shutdown after every
  test has passed. The rationale is in `tests/_test_infra.py`.
- **Integration `conftest.py`** keeps the `summarizer` alias resolvable, since
  the shipped base config leaves it unconfigured on purpose (v0.3.4
  no-default-provider); tests that want an unresolvable model re-patch it.
- **Opt-in markers** (declared in `agents/pyproject.toml`): `requires_compose`
  (observability stack up), `requires_orchestrator` (built binaries),
  `requires_anthropic` (real, billed calls; auto-skip without a key).
- **Long runs**: the root unit tree takes ~5.5 min. Run it in the foreground
  with an explicit ≥10-min timeout and no buffering filter — a killed run
  looks identical to a live one until it ends.

---

## The live arc — manual tests as the release gate

Automated layers prove mechanisms. The **live arc** proves the release's
story on a real model, once, with evidence. Its rules are in
[release-cycle.md](release-cycle.md#pr-1--the-live-arc-and-its-execution-report);
the testing-specific ones:

- **Three-state preflight before spending.** Each leg has a gate that can
  pass, fail, or be *skipped* ("cannot be answered yet" is not a failure).
  A blocking gate stops the arc. `scripts/manual_tests/mt_gate.py`.
- **Vacuity is the enemy.** An absence bar (nothing leaked, no growth) is met
  by an empty read. Every absence leg carries a positive control and reads
  both partitions of whatever it measures.
- **Evidence verbatim.** Tables, triples, counts, per-dispatch spans — pasted
  into the execution report, not summarised.
- **One script paces the arc.** Governance timers (600 s end-vote windows,
  floor-control rounds) expire while a human reads; the driver does not.
- **Run knobs are temporary** (short idle timeouts) and reverted before the
  report commits; the report lists them.
- **Cost is recorded** from the reconciled-charge log line, not the lease
  line (v0.3.15 P-1).
- **Execution reports are frozen** after the tag and exempt from the word cap.

---

## Golden-trace evals

The eval harness ([evaluators-guide.md](../evaluators-guide.md)) records a
conversation once (`make eval-record-offline` against the mock, or
`make eval-record` live) and replays it deterministically
(`make eval-replay`). `make eval-drift` reports live drift and never gates.
Seeds: the dementia test, cross-room carry (shadow and live), the
confidentiality gate, working memory. Replay pins the offline optimization
overlay, so it replays only goldens recorded under that overlay — a known
limit parked in the RFC 0044 PR plan.

---

## Adding a test — checklist

1. Pick the layer from the table. If none fits, you are adding a layer: add
   the row, the `make` target, and the CI step in the same PR.
2. Unit first (red), then implementation (green). Integration after.
3. No real network in unit tests. Mark integration tests that need a stack
   with the right opt-in marker.
4. If the test guards a **class** of regression (a cost leak, a lockstep
   guard, a boundary), say so in a comment where the test lives, naming the
   incident — the CI file does this for every job and it is what lets the next
   maintainer keep the gate instead of deleting it.
5. If the change touches a wake-path file, the cost-regression trigger list
   in `ci.yml` must include it (RFC 0024 §Test Strategy).
6. Run the reviewer's re-run set for the language
   ([review-process.md](review-process.md#what-the-reviewer-re-runs)).

---

## Known gaps in the test system (2026-09-06)

| Gap | State | Owner |
|-----|-------|-------|
| Perf gate never armed — `tests/perf/baselines/` does not exist | `perf-baseline-capture.yml` exists, never dispatched | maintainer; [ISSUE-0058](../issues/ISSUE-0058-perf-gate-runner-variance-tolerance.md) covers the tolerance |
| Eval replay not in CI | RFC 0044 Phase 2, v0.3.16 cuttable | sequencing |
| `cli/tests/` and `internal/testutil/` are prescribed by the instruction files and do not exist | instructions overstate | instruction-file collapse PR in this series |

## Related documentation

- [enforcement-matrix.md](enforcement-matrix.md) — which checks are required, advisory, or local-only
- [automation-catalogue.md](automation-catalogue.md) — every target and script
- [manual-tests/README.md](../manual-tests/README.md) — the MT index and conventions
- [evaluators-guide.md](../evaluators-guide.md) — writing an eval recipe
