# Persatrix Roadmap

> **Last updated**: 2026-09-06 (v0.3.15 release prep — the live arc passed and closed six issues; docs verified and the [checklist](docs/v0.3.15-release-checklist.md) landed. Version bump next.)
> **Current phase**: **v0.3.15** *Who said what* — 🚧 **release prep** ([milestone plan](docs/v0.3.15-plan.md) · [release-prep plan](docs/v0.3.15-release-prep-plan.md) · [baseline](docs/v0.3.15-release-baseline.md) · [checklist](docs/v0.3.15-release-checklist.md)); all three workstreams merged — A at [PR 4b](https://github.com/mkhomutov/Persatrix/pull/852), B at [#850](https://github.com/mkhomutov/Persatrix/pull/850)/[#851](https://github.com/mkhomutov/Persatrix/pull/851), C at [#842](https://github.com/mkhomutov/Persatrix/pull/842) — the live arc ran on 2026-09-02 and landed as release-prep [PR 1](https://github.com/mkhomutov/Persatrix/pull/855), leaving the version bump and the tag; scope ratified by the [sequencing Amendment 2026-08-19](docs/v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train): the three residuals the v0.3.14 changelog assigns to it — [ISSUE-0082](docs/issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) **R-1** (per-speaker interaction scope, an RFC 0020 §G change) and **R-2** (agent cascades), plus [ISSUE-0130](docs/issues/ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md) shape **(b)** (persist `principal_id` on `messages`, channel store v11 → v12, repaired by v13) — joined by [ISSUE-0131](docs/issues/ISSUE-0131-derived-memory-has-no-speaker-attribution.md) (the speaker axis — same record-shape decision, its own memory-store migration 17 → 18) and [ISSUE-0125](docs/issues/ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md) (fleet re-registration, cuttable), so interaction functionality is complete before v0.4.0 organizations build on it. Designs + PR plan: [#822](https://github.com/mkhomutov/Persatrix/pull/822) (merged 2026-08-23); the milestone plan owns the two workstreams it does not — ISSUE-0130(b) and ISSUE-0125. Then **v0.3.16** *The persona knows who is listening* ([ISSUE-0132](docs/issues/ISSUE-0132-memory-egress-gate-blind-to-room-audience.md) audience-scoped egress, the operator-observability pair, RFC 0044 Phase 2), then **v0.4.0** *Agent Organizations* (RFC 0012 + RFC 0028). Prior phase: v0.3.14 *One persona, many people* — ✅ [Released 2026-08-19](https://github.com/mkhomutov/Persatrix/releases/tag/v0.3.14).
> **Current milestone**: **v0.3.15** *Who said what* — 🚧 Release prep ([plan](docs/v0.3.15-plan.md) · [checklist](docs/v0.3.15-release-checklist.md); opened 2026-08-23, all eight implementation PRs merged as of 2026-09-01, the live arc **passed** — [execution report](docs/manual-tests/v0.3.15-execution-report.md)). The story: **memory the persona derives from a shared room names the person it came from — and survives the trip through the orchestrator.** v0.3.14 made the verified principal a per-request axis and that boundary holds for every turn's own write; it does not hold for the three writes that are not turns — the **derived** write ([ISSUE-0082](docs/issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) R-1: one close record per ROOM, summarised and fact-extracted under whichever principal closed it), the **relayed** write (R-2: a persona's reply re-enters unauthenticated, so the cascade below it loses the tenant), and the **replayed** write ([ISSUE-0130](docs/issues/ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md) shape (b): `messages` carries no principal, so catch-up derives nothing attributable). Underneath them, the axis none supplies — derived memory records what was said and where, never **who said it** ([ISSUE-0131](docs/issues/ISSUE-0131-derived-memory-has-no-speaker-attribution.md)); the [#822](https://github.com/mkhomutov/Persatrix/pull/822) Phase 0 gate keyed the interaction record `(principal, speaker, scope)` for both axes at once. Cuttable: [ISSUE-0125](docs/issues/ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md) (fleet re-registration) — which nonetheless lands **first**, because every restart-bearing leg of the live gate is a leg that today leaves the fleet mute. Four store migrations across two databases: channel store v11 → v12 and v12 → v13 (Go), and persona-memory 17 → 18 plus 19 (Python) — the last two added by the B2 review: an index on a predicate the live close path was already scanning, and a repair for stores that took v12's original backfill. Prior milestone: v0.3.14 *One persona, many people* — ✅ [Released 2026-08-19](https://github.com/mkhomutov/Persatrix/releases/tag/v0.3.14) ([plan](docs/v0.3.14-plan.md) · [release-prep plan](docs/v0.3.14-release-prep-plan.md) · [checklist](docs/v0.3.14-release-checklist.md); the live two-account MT **passed** — [execution report](docs/manual-tests/v0.3.14-execution-report.md), both emitted principals recorded off storage). **Next milestone: v0.3.16** *The persona knows who is listening* — then **v0.4.0** Agent Organizations, per the [sequencing Amendment 2026-08-19](docs/v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train).

This document tracks development progress across all versions. Update it when merging PRs or completing milestones.

---

## Version Map

A version is ready when a developer can do something meaningful they could not do before. Versions are defined by what a user **can do** — not by which RFCs are internally complete.

| Version | What a user can do | Status |
|---------|-------------------|--------|
| **v0.1.0** | Submit YAML workflows, orchestrate task agents via gRPC, poll status via REST | ✅ Complete — internal baseline |
| **v0.2.0** ⭐ | Run persistent AI agents with personalities, memory, and evolving relationships from a terminal | ✅ Complete — first public release |
| **v0.2.1** | Talk to a persona agent from your terminal — the agent remembers you and responds in character | ✅ Complete — released |
| **v0.2.2** | Bounded, predictable per-event memory injection for persona agents — structural fix unblocking RFC 0008 | ✅ Complete — released |
| **v0.2.3** | Observability Foundation — logs + traces + metrics + correlation shipped together: structured JSON logs across Go/Python/CLI on a versioned schema, working `persatrix logs` CLI (with `--follow` and server-side filters), end-to-end OpenTelemetry traces from REST handler to LLM call (with OTEL Gen-AI semantic conventions), OTLP metrics with exemplars, W3C Baggage propagation, and a tail-sampling Collector pipeline. Combined deliverable of RFCs 0018 + 0019. | ✅ Complete — released |
| **v0.3.0** | Give agents a shared channel and watch them talk, negotiate, and form opinions over time | ✅ Complete — released |
| **v0.3.1** | The persona remembers stated facts about you across interactions and follows the conversation it is currently having | ✅ Complete — released |
| **v0.3.2** | Every LLM call in the system passes through a wallet lease before it is issued — cost is a structural gate, not a post-hoc accountant — and the memory facade is frozen as the single path to agent memory ahead of the v0.4.0 Postgres split | ✅ Complete — released |
| **v0.3.3** | A persona with no scheduled work and no inbound traffic costs nothing — no SQLite recall query, no `_inject_memory_context`, no provider activity, no wallet lease requested. The persona autonomy loop is structurally event-driven (inbound RPC / salience-triggered memory write / scheduled timer), not polled. | ✅ Complete — released |
| **v0.3.4** | Reference models by logical alias (`quality` / `fast` / `summarizer`) instead of vendor-specific IDs — a vendor deprecation or provider swap becomes a one-line edit, not a config sweep — and run the whole society on any provider the same way: Anthropic, OpenAI, a free local model (Ollama), or a $0 offline mock | ✅ Complete — released |
| **v0.3.5** | Persona-memory recall is session-scoped — a run reads only its own session (plus the always-visible `legacy` rows), so concurrent conversations don't bleed and a run is isolated by giving it a fresh session instead of `make reset` (closes the F-3 cross-run *recall* bleed; auto-isolating a same-named rerun is the `epoch` axis, also shipped) | ✅ Released |
| **v0.3.6** | Open a URL and talk to a persona — chat and watch a channel from the browser with zero CLI knowledge, via an embedded same-origin web console behind `--enable-ui` (RFC 0048 Slice 1: Interactions; later slices add memory/cost/control-plane panels) | ✅ Released |
| **v0.3.7** | Watch several personas hold a group conversation that reads like colleagues, not bots — they see and build on the transcript (RFC 0034 P2), the right people speak, and a question to one isn't answered by everyone (RFC 0030 relevance gate Tier A + peer-voice prompt) | ✅ Released |
| **v0.3.8** | Give a group of personas a problem and watch the brainstorm **converge, terminate, and produce a readable synthesized outcome** — no pile-on, bounded cost, a real result (RFC 0030 relevance Tier B salience + `chair` disposition + natural-language addressing + reply budget + cost ceiling + end-of-interaction + interaction-summary surface), plus per-channel governance knobs you can edit live from the CLI or web console (RFC 0050) | ✅ Released |
| **v0.3.9** | Personas quote each other accurately and you can search the verbatim record of what was said, scoped to the channels each was present for (RFC 0036 + RFC 0035) | ✅ Released |
| **v0.3.10** | Watch personas **think before they speak** — they stay silent (with a reason) when they have nothing to add (semantic silence, on by default once a channel is governed) and — one opt-in rung up, per channel — compose considered, plan-threaded posts instead of reflexive ones (RFC 0051 Phases 1–3 + 5) | ✅ Released |
| **v0.3.11 — Conversations that run themselves** | Give a roster of personas a topic and watch them discuss it, converge, and produce a readable synthesis **with no human in the loop** — the human-out-of-the-loop capstone of the realism arc (RFC 0052 autonomous agent-only channels; brainstorm-level, decision-reaching deferred to v0.4.x) — and watch the flagship demo run it **across four vendors at once** (Anthropic + OpenAI + Gemini + watsonx.ai, via RFC 0053 provider additions). Anchored at [plan opening 2026-06-28](docs/v0.3.11-plan.md#scope-decisions-locked-at-plan-authoring-time-2026-06-28); the *safe remote console* (RFC 0039) becomes the v0.3.12 candidate. | ✅ Released |
| **v0.3.12 — Memory that travels** | Tell a persona something in one room and it knows it in every other room it belongs to — a project fact taught in a DM is known in the standup, raw experience from other rooms is available (room-first-ranked) — **without leaking what it learned in a confidential room** (RFC 0037 classification + protection levels + deterministic egress gate; RFC 0049 Phases 0–1 widenings, shadow-gated). Plus verified accounts: with `auth.mode: enabled` the web console/REST surface is safe beyond localhost (RFC 0039 Phases 1–2, bundled/cuttable). | ✅ Released |
| **v0.3.13 — Deferred calls closed** | The v0.3.12 promise holds everywhere it was made: a persona's own memory-tool recalls obey the same epoch/session isolation as injected memory ([ISSUE-0118](docs/issues/ISSUE-0118-tool-recall-bypasses-epoch-session-scopes.md)), the person-identity half of cross-room memory is verified live ([ISSUE-0121](docs/issues/ISSUE-0121-crossroom-person-identity-legs-never-run-live.md) — [MT-MEMORY-CROSSROOM-001](docs/manual-tests/MT-MEMORY-CROSSROOM-001.md) legs 1b/2b), and one channel's discussion length is tunable without retuning the fleet ([ISSUE-0114](docs/issues/ISSUE-0114-per-channel-cascade-depth-override.md) per-channel cascade-depth override). Fold-in (cuttable): [ISSUE-0116](docs/issues/ISSUE-0116-fact-subject-renders-unquarantined.md). Sequenced by the [v0.3.x-sequencing Amendment 2026-08-02](docs/v0.3.x-sequencing.md#amendment-2026-08-02--v0313--v0314-the-two-release-tail-to-v040); scope locked at the [plan opening](docs/v0.3.13-plan.md) 2026-08-03; all three fixes merged and **verified live** at release-prep PR 1 ([execution report](docs/manual-tests/v0.3.13-execution-report.md)); the fold-in was **taken, not cut**. | ✅ Released |
| **v0.3.14 — One persona, many people** | Two authenticated people talk to the same persona concurrently — beyond localhost under `auth.mode: enabled` — without one person's memory bleeding into the other's: the orchestrator emits a per-request **principal**, the half of the [ISSUE-0081](docs/issues/ISSUE-0081-session-id-process-global-not-task-local.md) rail still unfed now that auth ships a verified claim ([ISSUE-0082](docs/issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) Part 2 — its session half landed 2026-05-29, so today every caller shares the one `local` principal), plus [RFC 0040](docs/rfcs/0040-agent-orchestrator-transport-unification.md) Phase 1 transport hygiene (cuttable) so the v0.4.0 train starts on the designed agent→orchestrator seam. Scope locked at the [plan opening](docs/v0.3.14-plan.md) 2026-08-05: derivation source = the RFC 0039 §F verified `participant_id`, emission `auth.mode: enabled`-only, the `delete_by_subject` erasure residual rides while the agent-global capacity sweeps cut to v0.4.0, and RFC 0040 Phase 1 is decoupled from that RFC's Accepted flip. **Verified live at release prep** ([execution report](docs/manual-tests/v0.3.14-execution-report.md)): the whole `MT-MEMORY-MULTIUSER-001` arc ran on a real provider with two accounts and both emitted principals read off storage — the release gate's own criterion. RFC 0040 Phase 1 was **taken, not cut**. | ✅ [Released 2026-08-19](https://github.com/mkhomutov/Persatrix/releases/tag/v0.3.14) |
| **v0.3.15 — Who said what** | Memory the persona derives from a shared room names the person it came from — and survives the trip through the orchestrator: the interaction close stops writing one multi-speaker aggregate under whichever principal happened to close it ([ISSUE-0082](docs/issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) **R-1**), an agent cascade no longer drops the originating tenant at the orchestrator hop (**R-2**), the emitting principal is persisted with the message so a restart replay cannot re-derive it under the shared tenant ([ISSUE-0130](docs/issues/ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md) shape **(b)**, channel store v11 → v12), and derived rows carry the speaker — not just the subject and the room — so hearsay is distinguishable from testimony ([ISSUE-0131](docs/issues/ISSUE-0131-derived-memory-has-no-speaker-attribution.md) — the persona-memory store's own migration 17 → 18, bound to R-1 by the shared record-shape decision rather than by a shared schema). Plus the fleet self-heals after an orchestrator restart instead of going silently mute ([ISSUE-0125](docs/issues/ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md), cuttable). Designs + PR plan: [#822](https://github.com/mkhomutov/Persatrix/pull/822); milestone [plan](docs/v0.3.15-plan.md) opened 2026-08-23, locking the `(principal, speaker, scope)` record shape, ISSUE-0125 riding in its connectivity-driven shape ahead of the live arc, and one extended live gate ([MT-MEMORY-GROUP-TENANT-001](docs/manual-tests/MT-MEMORY-GROUP-TENANT-001.md)). Sequenced by the [v0.3.x-sequencing Amendment 2026-08-19](docs/v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train). PR C1 merged first — re-registration **taken, not cut**, on the connectivity-state shape, with both [ISSUE-0126](docs/issues/ISSUE-0126-mt-orchestrator-restart-registry-note-missing.md) MT warnings retired. Workstream A is complete: PR A1 landed the causal-attribution store dormant, PR A2 made R-2 behavioural — a persona's reply is re-stamped with the principal that caused it, so the cascade below it stays in that person's tenant instead of collapsing into the shared `local` bucket — and [PR A3](https://github.com/mkhomutov/Persatrix/pull/846) re-keys the interaction record itself `(principal, speaker, scope)`, so a group room no longer closes one multi-speaker aggregate under one tenant, with persona-memory migration 18 landing the `speaker_id` column dormant; [PR A4](https://github.com/mkhomutov/Persatrix/pull/849) then writes it — the close path projects the record key's speaker half onto the episode row and every extracted fact, with the RFC 0020 §G room-close turn excluded from the derivation input. [PR A4b](https://github.com/mkhomutov/Persatrix/pull/852) pays the re-key's bill: the RFC 0052 close-path budget reserve was sized for one summary per persona and a room now closes one per persona per `(principal, speaker)` pair, so it is re-sized to that count — and because the half-cap clamp consequently bites in ordinary configs, the under-funded close it produces stops being silent. Workstream B is complete: [PR B1](https://github.com/mkhomutov/Persatrix/pull/850) persists the emitting principal on `messages`, and [PR B2](https://github.com/mkhomutov/Persatrix/pull/851) makes catch-up replay derive in that tenant, exactly once across restarts. **Every implementation PR is merged as of 2026-09-01**, so the cycle is in release prep ([plan](docs/v0.3.15-release-prep-plan.md) · [baseline](docs/v0.3.15-release-baseline.md)) and the only work left before the tag is the live [MT-MEMORY-GROUP-TENANT-001](docs/manual-tests/MT-MEMORY-GROUP-TENANT-001.md) arc — ten legs, run once, as release-prep PR 1. Four store migrations ship (channel v12 and v13, persona-memory 18 and 19), so the release is forward-only rather than drop-in. **Verified live at release prep** ([execution report](docs/manual-tests/v0.3.15-execution-report.md)): the whole ten-leg [MT-MEMORY-GROUP-TENANT-001](docs/manual-tests/MT-MEMORY-GROUP-TENANT-001.md) arc ran on a real provider for $0.13 — R-2 carried the tenant on 5/5 dispatches including a persona-sender publish, every close-derived record came back single-speaker **and** single-principal, and a restart replay re-derived byte-identically under the attributed tenant. ISSUE-0125 was **taken, not cut**. | 🚧 Release prep |
| **v0.3.16 — The persona knows who is listening** | The persona weighs **who is in the room** before it speaks from memory: audience becomes an AND-condition on the RFC 0037 §D egress gate, which today decides from the channel's classification alone — so a fact learned from one person in a DM is admissible in an equally-classified room where a different person is present ([ISSUE-0132](docs/issues/ISSUE-0132-memory-egress-gate-blind-to-room-audience.md), shadow-first). Plus an operator can finally read why the persona decided what it decided ([ISSUE-0122](docs/issues/ISSUE-0122-relationship-tier-emits-no-provenance.md) identity-tier provenance, [ISSUE-0108](docs/issues/ISSUE-0108-reasoning-reason-note-no-operator-egress.md) reasoning `reason_note` egress), and the org train starts with an automated regression bar ([RFC 0044](docs/rfcs/0044-eval-set-golden-traces.md) Phase 2 CI gate, cuttable). Sequenced by the [v0.3.x-sequencing Amendment 2026-08-19](docs/v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train). | 📋 Planned |
| **v0.4.0** | Define a team, lab, or company with roles and hierarchy — and let it **reason toward a decision**, not just brainstorm (RFC 0012 organizations + RFC 0028 deliberative reasoning) | 📋 Planned |
| **v0.5.0** | Bridge your agent society into Slack, Discord, or email | 📋 Planned |
| **v0.6.0** | Run agent societies across multiple nodes and networks | 📋 Planned |

---

## RFC Master Index

Internal RFCs are the engineering planning tool. They do not drive version numbers. The table below shows each RFC's target public version.

| RFC | Title | Target Version | Status |
|-----|-------|----------------|--------|
| [0001](docs/rfcs/0001-core-orchestration-pipeline.md) | Core Orchestration Pipeline | v0.1.0 | ✅ Implemented |
| [0002](docs/rfcs/0002-rest-api-server.md) | REST API Server | v0.1.0 | ✅ Implemented |
| [0003](docs/rfcs/0003-scheduler-executor.md) | Scheduler & Executor | v0.1.0 | ✅ Implemented |
| [0004](docs/rfcs/0004-python-agent-grpc-server.md) | Python Agent gRPC Server | v0.1.0 | ✅ Implemented |
| [0005](docs/rfcs/0005-persona-agent-memory.md) | Persona Agent & Memory System | v0.2.0 | ✅ Implemented |
| [0006](docs/rfcs/0006-efficiency-execution-limits.md) | Efficiency & Execution Limits | v0.2.0 | ✅ Implemented |
| [0007](docs/rfcs/0007-conditional-looped-workflow-control-flow.md) | Conditional & Looped Workflow Control Flow | v0.4.0 | 📋 Proposed |
| [0008](docs/rfcs/0008-agent-memory-context-optimization.md) | Agent Memory & Context Optimization | v0.3.0 | ✅ Implemented (PRs 1 [#218](https://github.com/mkhomutov/Persatrix/pull/218), 1b [#219](https://github.com/mkhomutov/Persatrix/pull/219), 2 [#220](https://github.com/mkhomutov/Persatrix/pull/220), 2a [#221](https://github.com/mkhomutov/Persatrix/pull/221), 3 [#222](https://github.com/mkhomutov/Persatrix/pull/222), 3a [#224](https://github.com/mkhomutov/Persatrix/pull/224), 4 [#223](https://github.com/mkhomutov/Persatrix/pull/223), 5 [#225](https://github.com/mkhomutov/Persatrix/pull/225), 6a [#227](https://github.com/mkhomutov/Persatrix/pull/227), 6b [#228](https://github.com/mkhomutov/Persatrix/pull/228), 6 (this PR) merged. OQ #12 calibration-window gate walked back 2026-05-10 — eviction-parameter calibration deferred to a v0.3.x follow-up that fires when observed-workload telemetry exists; instrumentation from PR 5 ships unchanged.) |
| [0009](docs/rfcs/0009-security-sandboxing.md) | Agent Identity, Security & Sandboxing | v0.3.0 (Phases 1–2) + v0.4.0 (Phases 3–4) | ⚠️ Partially Implemented (Phases 1–2; PRs 1 [#233](https://github.com/mkhomutov/Persatrix/pull/233), 1b [#234](https://github.com/mkhomutov/Persatrix/pull/234), 1c [#236](https://github.com/mkhomutov/Persatrix/pull/236), 2 [#244](https://github.com/mkhomutov/Persatrix/pull/244), 3 [#253](https://github.com/mkhomutov/Persatrix/pull/253), 4 (this PR) merged — audit logger + secret redactor + orchestrator wiring + RedactStruct hardening + audit metrics + RateLimiter + CircuitBreaker + REST/gRPC middleware + unquarantine endpoint + InputSanitizer + Go canonical patterns + Python mirror + `<external_data>` envelope wrapping + tag-escape hardening + PR 4 review follow-ups (typed depth-marker sentinel; deterministic ticker test seam; `VerifyChain` exported helper; `looksLikeSHA256` → `hex.DecodeString`; `Emit` write-alloc reduction; generic-secret trailing-quote nit; GitHub/GCP/Slack/Stripe redactor patterns; `RedactStruct` benchmark; coverage-gap tests; PR #234 N-1/N-2/N-3 + PR #236 L-1/L-2/L-3/L-5 dispatched). Phases 3–4 deferred to v0.4.0 — see [RFC 0009 Implementation Notes (v0.3.0)](docs/rfcs/0009-security-sandboxing.md#implementation-notes-v030) for v0.3.0 deviations.) |
| 0010 | Sub-Agent Spawning | v0.4.0 | Not yet written |
| [0011](docs/rfcs/0011-channels-bridges.md) | Channels + Bridges | v0.3.0 (internal) + v0.5.0 (external) | ⚠️ Partially Implemented (internal channels — external bridges deferred to v0.5.0) |
| [0012](docs/rfcs/0012-protocols-organizations.md) | Protocols & Organizations | v0.4.0 (Phases 1–3) + v0.5.0 (Phases 4–5) | 📋 Proposed |
| [0013](docs/rfcs/0013-legal-ethical-compliance.md) | Legal, Ethical & Regulatory Compliance | v0.5.0 | 📋 Proposed |
| [0014](docs/rfcs/0014-agent-skill-registry-lifecycle.md) | Agent Skill Registry & Lifecycle | v0.4.0 | 📋 Proposed |
| [0015](docs/rfcs/0015-process-automation-pattern-extraction.md) | Process Automation & Pattern Extraction | v0.5.0 | 📋 Proposed |
| [0016](docs/rfcs/0016-human-participant-chat-interface.md) | Human Participant & Chat Interface | v0.2.1 | ✅ Implemented (Amended 2026-05-12 — wire-field rename `session_id` → `chat_session_id` per [RFC 0031 §OQ #8](docs/rfcs/0031-per-session-namespacing-channels.md#open-questions); see [RFC 0016 §Amendments](docs/rfcs/0016-human-participant-chat-interface.md#amendments)) |
| [0017](docs/rfcs/0017-persona-memory-injection-budget.md) | Persona Memory Injection Token Budget | v0.2.2 | ✅ Implemented (7/7) |
| [0018](docs/rfcs/0018-structured-logging-framework.md) | Structured Logging Framework | v0.2.3 | ✅ Implemented |
| [0019](docs/rfcs/0019-opentelemetry-completion.md) | OpenTelemetry Completion | v0.2.3 | ✅ Implemented |
| [0020](docs/rfcs/0020-interaction-lifecycle.md) | Interaction Lifecycle: Dialogue Boundaries & Episode Granularity | v0.3.0 | ✅ Implemented |
| [0021](docs/rfcs/0021-persona-temporal-awareness.md) | Persona Temporal Awareness | v0.3.0 (Phase 1) + v0.4.0 (Phases 2–4) | ⚠️ Partially Implemented (Phase 1) |
| [0022](docs/rfcs/0022-persona-prompt-section-templating.md) | Persona Prompt Section Templating | v0.3.0 | ✅ Implemented |
| [0023](docs/rfcs/0023-llm-call-leasing.md) | LLM Call Leasing — per-call wallet lease gating every LLM invocation (workflow / chat / autonomous TICK / sub-agent / channel-message) so cost enforcement is an in-line gate, not a post-hoc accountant; closes the v0.2.3 chat-bypass known limitation. (Slot 0023's original "Episodic Memory Quality" reservation was narrowed into [memory-quality-roadmap.md](docs/memory-quality-roadmap.md); the slot was reused for this RFC — see the [RFC 0029 numbering note](docs/rfcs/0029-personal-society-storage-split.md).) | v0.3.2 | ✅ Implemented (Phases 1–6; PRs 1 [#378](https://github.com/mkhomutov/Persatrix/pull/378), 2 [#384](https://github.com/mkhomutov/Persatrix/pull/384), 3 [#385](https://github.com/mkhomutov/Persatrix/pull/385), 4 [#387](https://github.com/mkhomutov/Persatrix/pull/387), 5 [#388](https://github.com/mkhomutov/Persatrix/pull/388), 6 [#389](https://github.com/mkhomutov/Persatrix/pull/389), 7 [#391](https://github.com/mkhomutov/Persatrix/pull/391), 8 (this PR) merged — proto/wallet.proto surface + `WalletService` skeleton; `BudgetEnforcer` enforcement + TTL reaper; Python `WalletClient` + workflow-task lease wiring; chat-path wiring (closes v0.2.3 bypass); autonomous TICK + sub-agent wiring; channel-message origin wiring; review follow-ups; RFC closeout. All five LLM-call origins now acquire a server-issued lease.) |
| [0024](docs/rfcs/0024-event-driven-scheduling.md) | Event-Driven Agent Scheduling — wake the persona on inbound RPC, salience-triggered memory write, or scheduled timer instead of a fixed-interval poll; a persona with no scheduled work and no inbound traffic costs nothing. Closes the polling-loop cost-leak class structurally instead of patching it per release. PR plan: [`0024-pr-plan.md`](docs/rfcs/0024-pr-plan.md). (Slot 0024's original "Episodic Vector Recall" reservation was deferred, gated on [MT-MEMORY-005](docs/manual-tests/MT-MEMORY-005-dementia-test.md) data; the slot was reused for this RFC — see the [RFC 0029 numbering note](docs/rfcs/0029-personal-society-storage-split.md).) | v0.3.3 (Phases 1–4) + v0.4.0 (Phase 5) + v0.5+ (Phase 6) | ⚠️ Partially Implemented (Phases 1–4; PRs 1 [#406](https://github.com/mkhomutov/Persatrix/pull/406), 2 [#407](https://github.com/mkhomutov/Persatrix/pull/407), 2.1 [#408](https://github.com/mkhomutov/Persatrix/pull/408), 3a [#409](https://github.com/mkhomutov/Persatrix/pull/409), 3b [#410](https://github.com/mkhomutov/Persatrix/pull/410), 4 [#411](https://github.com/mkhomutov/Persatrix/pull/411), 5 [#412](https://github.com/mkhomutov/Persatrix/pull/412), 5.1 [#413](https://github.com/mkhomutov/Persatrix/pull/413), 6 (this PR) merged — `agents/event_loop.py` EventLoop + WakeEvent taxonomy + SyncDispatchHandle substrate; `autonomy.timers` config + per-agent SQLite `scheduled_wakes` cache wired into `initialize_persona_agents`; write-side `salience` + `source_span_id`; `SalienceWake` enqueue + threshold + loop-back guard + rate-limit (default-off at `0.95`); channel-message fire-and-forget `InboundEventWake` dispatch + the "bored persona costs nothing" cost-regression CI gate; EventLoop lifecycle hardening + review-follow-up cleanups. RFC 0017 §F guard is structurally unreachable but stays in place with an `action_loop.py` cross-link naming Phase 5/6 as the deletion path. Phases 5 (`tick_interval_seconds` deprecation warning) → v0.4.0; Phase 6 (`tick_interval_seconds` + §F guard + `EventType.TICK` removal) → v0.5+.) |
| 0025 | Thematic Episode Clustering — superseded by RFC 0027 per [memory-quality-roadmap.md](docs/memory-quality-roadmap.md) | superseded | Reserved (superseded by 0027) |
| [0026](docs/rfcs/0026-declarative-facts-tier.md) | Declarative Facts Tier | v0.3.1 | ✅ Implemented |
| [0027](docs/rfcs/0027-reflection-driven-consolidation.md) | Reflection-Driven Consolidation | v0.4.0 | 📋 Proposed |
| [0028](docs/rfcs/0028-agent-decision-policy-engine.md) | Agent Decision Policy Engine | v0.4.0 | 📋 Proposed |
| [0029](docs/rfcs/0029-personal-society-storage-split.md) | Personal/Society Storage Split (SA-1 from [storage-architecture-roadmap.md](docs/storage-architecture-roadmap.md); originally filed as 0025, renumbered to preserve the 0025→0027 supersession edge) | v0.3.2 (Phase 1) + v0.4.0 (Phases 2–6) | ⚠️ Partially Implemented (Phase 1; PRs 1 [#370](https://github.com/mkhomutov/Persatrix/pull/370), 2 [#372](https://github.com/mkhomutov/Persatrix/pull/372), 3 [#373](https://github.com/mkhomutov/Persatrix/pull/373), 4 [#375](https://github.com/mkhomutov/Persatrix/pull/375), 5 (this PR) merged — `MemoryStore` facade promotion + `MemoryFacade` alias shim + personal/society boundary lint rule + `persona_runtime`/`sub_agents`/RFC 0026 call-site sweep + recall-latency regression gate. Phases 2–6 deferred to v0.4.0.) |
| [0030](docs/rfcs/0030-multi-agent-conversation-governance.md) | Multi-Agent Conversation Governance — layered termination + cost + reply-budget + moderator over the v0.3.0 channels stack; composes RFC 0011 amendment / RFC 0020 / RFC 0023 / RFC 0028. Motivated by the v0.3.0 F-1 finding tail (cost ceiling and productive-termination beyond cascade_depth). | v0.3.6 (Layer 2.5 — shipped) + v0.3.7–v0.3.9 (Phase 1 — [relevance gate](docs/rfcs/0030-amendment-relevance-gated-response.md) + cost/reply-budget/end-of-interaction layers) + v0.4.0 (Phase 2 — moderator / bid-and-select) + v0.5.0+ (Phase 3 — declarative types + topic-drift) | 🚧 Implementing — **Layer 2.5 (floor control / speaker serialization) ✅ shipped v0.3.6** via the [floor-control amendment](docs/rfcs/0030-amendment-floor-control-speaker-serialization.md) (on by default for group channels; incl. floor telemetry, PR 4); **relevance-gate Tier A + `respond_policy → disposition` reframe ✅ shipped v0.3.7** via the [relevance amendment](docs/rfcs/0030-amendment-relevance-gated-response.md) (addressing-aware directedness; back-compat vocabulary); **deterministic Layers 1/2/4 (cost ceiling + reply budget + end-of-interaction vote) ✅ landed v0.3.8** ([governance-layers PR plan](docs/rfcs/0030-governance-layers-pr-plan.md), opt-in/uncapped); **the `interaction_id` producer ✅ landed v0.3.8** ([producer plan](docs/rfcs/0030-interaction-id-producer-pr-plan.md), PRs [#604](https://github.com/mkhomutov/Persatrix/pull/604)–[#606](https://github.com/mkhomutov/Persatrix/pull/606)) — ids stamped on every publish, idle rotation, the agent-side vote + Layer 1 lease attribution: a discussion now closes on two end-votes (acceptance [MT-CHANNEL-GOV-003](docs/manual-tests/MT-CHANNEL-GOV-003.md)); **chair stall escalation (minimal Layer 5 slice) ✅ landed v0.3.8** ([amendment](docs/rfcs/0030-amendment-chair-stall-escalation.md), PRs [#608](https://github.com/mkhomutov/Persatrix/pull/608)–[#611](https://github.com/mkhomutov/Persatrix/pull/611)) — a zero-reply round on an open interaction forces one chair turn (synthesize-in-vote or hand off; quorum still disposes; acceptance [MT-CHANNEL-GOV-004](docs/manual-tests/MT-CHANNEL-GOV-004.md)). Tier B salience → v0.3.8; moderator (Layer 5) → v0.4.0 |
| [0031](docs/rfcs/0031-per-session-namespacing-channels.md) | Per-Session Namespacing for Channels and Persona Memory — first-class Session primitive scoping `channels.db` and per-persona `memory.db`, with an operator-visible `persatrix session …` CLI; F-3 root-cause fix, succeeds the `make reset` workaround from PR 6 of the v0.3.0 channel test-findings plan. Spawned from [ISSUE-0051](docs/issues/ISSUE-0051-per-session-memory-namespacing-channels.md). | v0.3.1 (P1) + v0.3.5 (P2–4) | ✅ Implemented (Phase 1 shipped v0.3.1; **Phase 2 recall filtering shipped v0.3.5** — session-scoped default recall across all four persona-memory tiers (episodes/relationships v7, facts v8, notes v9), closing F-3 cross-run state bleed at the root. **Phase 3 operator CLI shipped v0.3.5** — the `persatrix session …` surface (`/api/v1/sessions` REST registry + `new`/`list`/`archive`/`use`/`current`/`--activate` + the `--session` override); all three session-resolution mechanisms (env → file → flag) wired ([Phase 3 PR plan](docs/rfcs/0031-phase3-pr-plan.md), PRs 1–5). **Phase 4 docs closeout shipped v0.3.5** — [`docs/guides/sessions.md`](docs/guides/sessions.md) + reframed `make reset` breadcrumb + [ISSUE-0051](docs/issues/ISSUE-0051-per-session-memory-namespacing-channels.md) closed; see [v0.3.5 plan](docs/v0.3.5-plan.md). **Successor work** from the [scope-axes reframing](docs/memory-scope-axes.md): **`epoch` run-isolation shipped v0.3.5** (Phase 3b, [ISSUE-0085](docs/issues/ISSUE-0085-epoch-axis-run-isolation.md) closed — migration v12 `epoch_id` across five tiers + relationships PK, strict-equality filter, gRPC rail, `--epoch` operator surface, F-3 structural-isolation gate; [epoch PR plan](docs/rfcs/0031-epoch-pr-plan.md), [#472](https://github.com/mkhomutov/Persatrix/pull/472)–[#477](https://github.com/mkhomutov/Persatrix/pull/477)); subject-scoped facts ([ISSUE-0084](docs/issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md)) and the `--all-sessions` verb ([ISSUE-0086](docs/issues/ISSUE-0086-operator-all-sessions-recall-verb.md)) remain deferred, tracked separately.) |
| [0033](docs/rfcs/0033-model-alias-layer.md) | Provider-Agnostic Model Alias Layer — decouple agent configs from vendor-specific model IDs via a named alias map (`quality` / `fast` / `summarizer` → `{provider, model_id, pricing}`), so vendor deprecations and multi-provider expansion change one file instead of dozens. Retires `_infer_provider` string-prefix routing. Proximate trigger: [Anthropic Sonnet 4 retirement](https://platform.claude.com/docs/en/about-claude/model-deprecations) (2026-06-15) — the migration is absorbed as the first dogfood of the alias layer. | v0.3.4 (Phases 1–2) + v0.3.5 (Phase 3) | ✅ Implemented (Phases 1–2 in v0.3.4, Phase 3 in v0.3.5; PRs 1 [#431](https://github.com/mkhomutov/Persatrix/pull/431), 2 [#432](https://github.com/mkhomutov/Persatrix/pull/432), 3 [#433](https://github.com/mkhomutov/Persatrix/pull/433), 4 [#434](https://github.com/mkhomutov/Persatrix/pull/434), 5 [#435](https://github.com/mkhomutov/Persatrix/pull/435), 6 [#436](https://github.com/mkhomutov/Persatrix/pull/436), 7 merged — `agents/model_aliases.py` resolver + `models.aliases` config block (+ priced OpenAI peer alias, local-pricing decision); `create_provider` → `(provider, physical_model)` tuple consuming `resolve()` + §D precedence + offline/Ollama interplay regression + raw-ID startup deprecation warning + `persatrix.llm.alias.raw_id_usage` counter (the Phase 3 gate signal); config migration to aliases (Sonnet 4 → 4.6 by editing only the `quality` alias) + `SubAgentRequest.model` `None`-default + network-allowlist neutralization, so no runtime path carries a literal vendor model ID; missing-price guard failing closed for unpriced non-local aliases while a local $0-by-design alias stays distinguishable; `persatrix.llm.model_alias` span attribute + `cost.pricing.models` derived from the alias map + the `GET /api/v1/cost/summary` cost-attribution gate; documentation sweep replacing literal vendor IDs with alias examples. **Phase 3** (v0.3.5, conditional co-resident — dogfood `raw_id_usage` gate satisfied): raw-ID pass-through removal ([#481](https://github.com/mkhomutov/Persatrix/pull/481)) + `_infer_provider` / `provider_inference` / `raw_id_usage`-counter retirement + `schema_version` bump to `"0.3"` + raw vendor IDs rejected at resolve. See [0033-pr-plan.md](docs/rfcs/0033-pr-plan.md).) |
| [0034](docs/rfcs/0034-persona-conversational-working-memory.md) | Persona Conversational Working Memory — reconstruct the LLM `messages` array from the channel store on every persona turn so the model sees the in-progress conversation as a transcript instead of a single isolated message. Closes the load-bearing defect captured in [ISSUE-0052](docs/issues/ISSUE-0052-persona-conversational-working-memory-gap.md): persona forgets its own previous question, cannot resolve referential follow-ups (`"I like it"`), treats every turn as the first turn. Phase 1 ships DM channels in v0.3.1 alongside RFC 0026 — the two share an MT acceptance surface. Phase 2 (group-channel per-peer `[<peer_id>]: ` attribution + multi-persona fetch-cache correctness) shipped in v0.3.7 as the critical-path realism lever; Phase 3 (`conversation_window.*` instrumentation + the fetch-cache LRU bound) shipped in v0.3.10. | v0.3.1 (Phase 1) + v0.3.7 (Phase 2 — group working memory) + v0.3.10 (Phase 3 — instrumentation + cache LRU bound) | ✅ Implemented — Phases 1–3 ([Phase 3 plan](docs/rfcs/0034-phase3-pr-plan.md)) |
| [0035](docs/rfcs/0035-channel-membership-interval-ledger.md) | Channel Membership Interval Ledger — append-only `membership_intervals` ledger in the channel store (one row per join/leave stint) so the system can answer "was participant X a member of channel Y at time T" — the membership history the current-state-only `memberships` table destroys on a remove or rejoin. Written transactionally by `AddMember`/`RemoveMember`/`GetOrCreateDM`, backfilled from current memberships, guarded by a partial unique index (≤1 open stint per pair). Infrastructure only — no user-visible behaviour; the substrate RFC 0036 recall scoping joins against. Phase 1 = ledger/write-hooks/backfill; Phase 2 = optional operator inspection endpoint. | v0.3.9 | ✅ Implemented — [RFC 0035 PR plan](docs/rfcs/0035-pr-plan.md): PRs 1–4 merged (migration v9, read surface, transactional write hooks incl. the `CreateChannelWithMembers` fourth hook, operator inspection endpoint); closeout this PR. Substrate-only — no user-visible behaviour; the ledger RFC 0036 recall scoping joins against |
| [0036](docs/rfcs/0036-persona-message-recall.md) | Persona Verbatim Message Recall — a `recall_channel_messages` persona tool that searches the verbatim text of past conversations, scoped **server-side in SQL** (FTS5 over `messages` joined against the RFC 0035 ledger) to the channels and membership intervals the persona had access to. A removed-and-re-added persona recalls both stints, neither the pre-join period nor the gap. `epoch_id`-hard-filtered (run isolation), session-spanning, audited, `channels:recall`-permission-gated, and delimiter-escaped against prompt injection. Phase 1 = FTS index + scoped search endpoint; Phase 2 = the persona tool; Phase 3 = retrofit the RFC 0034 conversation window with the same membership filter. | v0.3.9 | ✅ Implemented — [RFC 0036 PR plan](docs/rfcs/0036-pr-plan.md): all six PRs merged (PRs 1–5 + closeout) — `messages_fts` FTS5 index, membership-scoped + epoch-filtered `RecallMessages` query, audited `POST …/recall` endpoint with server-side audit, `recall_channel_messages` persona tool + `channels:recall` permission + §F per-row escape inside the RFC 0009 `<external_data>` envelope, `?as_participant` conversation-window membership filter; builds on the ✅ Implemented RFC 0035 ledger. The user-facing win of v0.3.9 |
| [0037](docs/rfcs/0037-memory-confidentiality-channel-classification.md) | Memory Confidentiality & Channel Classification — ordered channel classification + per-entry protection levels + the deterministic §D memory-injection egress gate + the §F recall filter (+ the RFC 0038 §B single-channel-turn guard, carved in per its Decision #3), so a persona learns from a confidential channel without leaking it. The **keystone** of the v0.3.12 cross-channel persona experience — no RFC 0049 widening merges before the gate. Pulled forward from the v0.4.0 on-ramp per the 2026-07-15 lock. | v0.3.12 (Phases 1–3; P2–3 cuttable) | ✅ Implemented — v0.3.12, all three phases ([PR plan](docs/rfcs/0037-pr-plan.md) PRs 1–8, closeout 2026-07-29); live acceptance `MT-PERSONA-CONFIDENTIALITY-001` at release-prep |
| [0038](docs/rfcs/0038-concurrent-context-awareness-relay.md) | Concurrent-Context Awareness & Cross-Channel Relay — the §B single-channel-turn guard rides RFC 0037 Phase 1 (v0.3.12, per 0037 Decision #3); §C–§E (contexts / awareness / relay) stay the v0.4.0 on-ramp. | v0.3.12 (§B, via RFC 0037) + v0.4.0 (§C–§E) | ⚠️ Partially Implemented — §B ✅ v0.3.12 (RFC 0037 PR 4, [#776](https://github.com/mkhomutov/Persatrix/pull/776)); §C–§E 📋 v0.4.0 |
| [0039](docs/rfcs/0039-user-accounts-authentication.md) | User Accounts & Authentication — human accounts with password login, opaque revocable sessions, and a coarse operator/user role gate on the REST surface; an account binds 1:1 to an RFC 0016 `UserParticipant` so the caller's `participant_id` becomes a verified claim. Foundation for the human-identity axis that pairs with RFC 0009 (agent identity) and RFC 0012 (organizational clearance). Ships inert behind `auth.mode` (Phase 1), then enforces (Phase 2), then adds account administration (Phase 3). | v0.3.12 (Phases 1–2) + v0.4.0 (Phase 3) | ⚠️ Partially Implemented — Phases 1–2 ✅ shipped in v0.3.12 (PRs [#779](https://github.com/mkhomutov/Persatrix/pull/779)/[#780](https://github.com/mkhomutov/Persatrix/pull/780)/[#790](https://github.com/mkhomutov/Persatrix/pull/790)/[#791](https://github.com/mkhomutov/Persatrix/pull/791)/[#793](https://github.com/mkhomutov/Persatrix/pull/793) per the [PR plan](docs/rfcs/0039-pr-plan.md); the [enabled-mode exposure amendment](docs/rfcs/0039-amendment-enabled-mode-exposure.md) ✅ implemented; operator surface: [auth guide](docs/guides/auth.md)); Phase 3 (account admin, password change, lockout) = v0.4.0 |
| [0040](docs/rfcs/0040-agent-orchestrator-transport-unification.md) | Agent–Orchestrator Transport Unification — migrate the agent→orchestrator control-plane calls (channel publish, channel history, agent registration) from REST to gRPC, leaving REST as the dedicated client edge. The orchestrator's inbound surface splits into two audience-specific APIs (gRPC for agents via a new `OrchestratorService`, REST for CLI / future Web UI) over one shared business-logic core — giving the agent→orchestrator path the typed protobuf contract the orchestrator→agent path already has. Today's REST path is an artifact of build order, not a designed choice; the structural cost compounds as v0.4.0 work lands. Gates RFC 0041 Phase 2 (event-stream transport); frames RFC 0043's client-edge / listener-topology decision (its OQ 1) but does *not* build-gate it. | v0.3.14 (Phase 1, cuttable — pinned by the [sequencing Amendment 2026-08-02](docs/v0.3.x-sequencing.md#amendment-2026-08-02--v0313--v0314-the-two-release-tail-to-v040)) + v0.4.0 (Phases 2–4) | 📋 Proposed — **Phase 1 shipped in v0.3.14** ([#829](https://github.com/mkhomutov/Persatrix/pull/829)): the channel publish/history payload contract is pinned in `schemas/channel.schema.json`, validated fail-open on the agent send side and held by a cross-language source-parity drift test on the Go decode side. Phase 1 carries no proto change, so it does **not** advance the RFC's status. Review fixes applied; [PR-plan skeleton](docs/rfcs/0040-pr-plan.md) drafted (8 PRs — Phase 1 v0.3.x hygiene + Phases 2–4 v0.4.0 train). Proposed → Accepted gated on the Phase 0 scope decisions (`sender_id` enforcement deferred to its owner RFC 0009 Phase 4, co-sequenced onto this path via RFC 0029 Phase 2; history migration; service shape; transport selection) + OQ 1/2. |
| [0041](docs/rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md) | Typed Event Taxonomy & Lifecycle Callbacks — one ordered, typed event stream per agent turn (`ModelOutput` / `ToolCallEvent` / `ToolResultEvent` / `StateDelta` / `Error` / `Control`) as the single surface every consumer (channel publish, structured logger, OTEL tracer, dead-letter, eval runner) reads, plus four named lifecycle callbacks (`before_model` / `after_model` / `before_tool` / `after_tool`) as the one seam cross-cutting concerns (wallet lease, sanitizer/redactor, RFC 0031 F-3 recall filter, persona quality-bar) plug into. Turns the ad-hoc error-reply work ([ISSUE-0065](docs/issues/ISSUE-0065-chat-rest-budget-denied-no-channel-reply.md)/[0066](docs/issues/ISSUE-0066-chat-rest-resource-exhausted-no-channel-reply.md)) into typed `Error` events and gives [RFC 0044](docs/rfcs/0044-eval-set-golden-traces.md) the typed events its `EVAL-ERROR-*` goldens assert on. **Phase 1** = vocabulary + adapter (in-process, no transport/Go); Phase 2 (consumer migration + wire encoding) gated on RFC 0040. | v0.4.0 (Phase 1) + v0.4.0+ (Phase 2) | 🚧 Implementing (Phase 1; [PR plan](docs/rfcs/0041-pr-plan.md)) — all 7 open questions resolved. PR 1 landed the dependency-free leaf test-first: the closed `ErrorKind` / `ToolErrorKind` taxonomies ([`agents/events.py`](agents/events.py)) with exact-membership tests that fail CI on a silent addition (Goal 5), Python-only per §A. The `TurnEvent` dataclass taxonomy follows in PR 2. Phase-1 precondition (RFC 0044 golden-trace format) already landed v0.3.11; Phase 2 (consumer migration + wire encoding) gated on RFC 0040. |
| [0042](docs/rfcs/0042-state-namespacing-by-scope.md) | State Namespacing by Scope Prefix — a closed set of scope prefixes (`app:` / `persona:` / `channel:` / `session:` / `interaction:` / `temp:`) that every agent-runtime state key carries, so persistence, visibility, and lifetime are set by the key itself rather than by whichever store happens to own it. Shallow on purpose: migrates no storage and changes no schema — it adds a uniform vocabulary that removes a class of per-feature "where does this state live / who sees it / when does it expire?" decisions. Finalizes `StateDelta.scope` and the `ScopedState` type that [RFC 0041](docs/rfcs/0041-typed-event-taxonomy-lifecycle-callbacks.md) ships as forward-compatible transitional forms. | v0.4.0+ | 🔨 Draft |
| [0043](docs/rfcs/0043-inbound-agent-interop-endpoint.md) | Inbound Agent-Interop Endpoint — a bounded HTTP/JSON inbound surface plus a third participant subtype (`ExternalAgentParticipant`) that let a non-Persatrix agent join a channel (post messages, receive messages addressed to it, observe authorized traffic) without speaking the orchestrator's internal gRPC contract. Narrow by design — channel send/receive and nothing else, not a back-door for workflows, persona memory, or wallet leases; the internal gRPC contract is left untouched. Sequenced last in the vocabulary cluster. Corrected dependency set (implementation-readiness review): RFC 0016 (participant model), RFC 0011/0030/0035 (channels + governance + membership ledger), RFC 0009 (capability-token credential model), RFC 0012 (external-admission gate), RFC 0039 Phase 2 (the adjacent REST surface must be authenticated) — RFC 0040/0041 are *not* build dependencies. | v0.4.x | 🔨 Draft — review fixes applied; [PR-plan skeleton](docs/rfcs/0043-pr-plan.md) drafted (15 PRs across Phase 1a unblocked / 1b+1c gated). Stays Draft pending the leave-Draft checklist (storage tier + OQ 1 listener topology). |
| [0044](docs/rfcs/0044-eval-set-golden-traces.md) | Eval-Set Shape with Golden Traces — a multi-turn golden-trace eval format (typed assertions over events, terminal state per scope, final transcript) as the automated regression bar the qualitative manual tests (e.g. the [dementia test](docs/manual-tests/MT-MEMORY-005-dementia-test.md)) never gave. Matters most for RFC 0052 autonomous channels — no human to catch a bad conversation — so it rides v0.3.11 as a cuttable safety-net fold-in (the way RFC 0045 rode v0.3.10). **Phase 1** = eval-set format + assertion engine + replay runner (no CI gate, no recorded goldens — the seed goldens need RFC 0041 typed events). | v0.3.11 (Phase 1 format + replay) + v0.3.16 (Phase 2 CI gate, cuttable — pinned by the [sequencing Amendment 2026-08-19](docs/v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train)) + v0.4.0+ (typed-event goldens, gated on RFC 0041 Phase 1) | 🚧 Implementing (Phase 1; [PR plan](docs/rfcs/0044-pr-plan.md)) — PR 1 landed the deterministic core test-first: the eval-set format ([`schemas/eval_set.schema.json`](schemas/eval_set.schema.json)) + the §B assertion engine ([`evaluators/`](evaluators/)) with `load_eval_set` / `evaluate`, no dependency on the unlanded RFC 0041 taxonomy. PR 2 landed the replay LLM client ([`evaluators/replay_llm_client.py`](evaluators/replay_llm_client.py)) — the recorded-response provider keyed by a volatile-stripping canonicalized request hash (OQ #2), the mock-as-LLM that makes replay CI-safe. PR 3 landed the runner ([`evaluators/runner.py`](evaluators/runner.py) + `persona_driver.py` + `report.py`) — recipe → real-persona-runtime drive → `evaluate` → structured artifact, the `make eval-replay`/`eval-record`/`eval-drift` targets, the `elapsed`→`FrozenClock` temporal seam (OQ #5), and the [author guide](docs/evaluators-guide.md); record→replay symmetry proven through the runtime. PR 4a landed the first pre-0041 seed — [`EVAL-MEMORY-001`](evaluators/eval_sets/EVAL-MEMORY-001.yaml), the dementia test as a five-interaction recall recipe with an offline-recorded golden that replays green on every PR (assertions restricted to `final_transcript`/`terminal_state`; also fixed the driver to force an isolated `:memory:` DB for golden portability + no production-memory pollution). PR 4c landed the second pre-0041 seed — [`EVAL-WORKING-001`](evaluators/eval_sets/EVAL-WORKING-001.yaml), RFC 0034 within-interaction working memory (the persona references its own prior question) — plus the driver seam it required: an in-process conversation-window history fetcher ([`evaluators/eval_channel_history.py`](evaluators/eval_channel_history.py)) wired when a recipe declares `setup.channel`, so the golden is load-bearing on working memory (channel-less recipes like EVAL-MEMORY-001 stay byte-identical). The event-asserting seeds (`EVAL-ERROR-001`/`002`) follow in PR 4b (gated on RFC 0041 typed events); `EVAL-RECALL-001` (cross-session no-leak) is deferred on a per-interaction-session recipe extension. |
| [0045](docs/rfcs/0045-open-core-extraction-policy.md) | Open-Core Library Extraction Policy — foundational three-tier policy: MIT funnel libraries below (budget lease, prompt-safety kit, mock provider, schemas), the self-hostable BUSL-1.1 product in the middle, a never-published Private moat above (managed/scaled society backend, billing, identity/tenancy). Fixes the license boundary, the `MIT ← BUSL ← Private` dependency-direction invariant + its CI gate, the monorepo-canonical/mirror-out sync default, DCO governance, the core-plus-adapters repo structure, and a reserved-seams + no-retraction rule (current memory incl. relationship/trust stays BUSL). Moves no code and stands up no private track; the commercial-architecture RFC is deferred to a forcing function. | v0.3.10 (policy + CI gate) + v0.4.0+ (per-extraction RFCs) | 🚧 Implementing — v0.3.10 fold-in landed the `MIT ← BUSL ← Private` dependency-direction CI gate (Python `import-linter` contract via `make imports-check` + the Go `internal/archpolicy` check) plus the [reserved-seams note](docs/open-core-reserved-seams.md) and DCO scaffold; the per-extraction RFCs (0046/0047) stay v0.4.0+. |
| [0046](docs/rfcs/0046-budget-lease-extraction.md) | Budget-Lease Library Extraction (`persatrix-budget`) — first per-extraction RFC under RFC 0045 and the flagship one. Carves the RFC 0023 LLM budget-lease into a standalone MIT `persatrix-budget` package as a **single-language Python library** (not a polyglot bundle): a pure-Python in-process budget engine (the default, zero-infrastructure path), a remote backend that speaks `wallet.proto` to any compatible authority, and ADK/LiteLLM adapters. The Go `internal/cost` + `internal/wallet` stay BUSL as the reference server, `wallet.proto` is published as the shared contract, and Persatrix agents dogfood the library in remote mode. Resolves the audience→artifact question, obviates the `internal/cost` split, keeps the RFC 0024 idle loop internal, and refines RFC 0045 §H/§B. Code moves only after RFC 0045 is Accepted and its boundary CI gate is green. | v0.4.0+ (gated on RFC-0045 acceptance + boundary CI gate) | 📋 Proposed |
| [0047](docs/rfcs/0047-low-coupling-batch-extraction.md) | Low-Coupling Batch Library Extraction (prompt kit, mock LLM, schemas) — second per-extraction RFC under RFC 0045, covering the three low-coupling MIT candidates as one batch, each as its own single-identity repo per the RFC 0046 precedent: **`persatrix-prompts`** (the leaf `prompt_loader` + `persona_behavior` renderer + `prompts/runtime` data + a framework-agnostic composer; the persona-state-coupled `prompt_assembly.py` stays BUSL and consumes it — the one real seam-cut), **`persatrix-mock-llm`** (the `LLMProvider` protocol + `MockProvider`; the real providers + factory stay BUSL), and **`persatrix-schemas`** (language-neutral JSON Schema data + example blueprints/templates + a thin reference validator; consumed in-tree by the Go planner, Python validator, and Rust CLI under Option A). Batched because all three are leaf with near-zero seam-cutting; refines RFC 0045 §H. Code moves only after RFC 0045 is Accepted and its boundary CI gate is green, and after the RFC 0046 pattern is proven. | v0.4.0+ (gated on RFC-0045 acceptance + boundary CI gate + RFC-0046 pattern) | 📋 Proposed |
| [0048](docs/rfcs/0048-operator-tester-web-console.md) | Operator & Tester Web Console — embedded, same-origin Svelte SPA served from the Go orchestrator via `embed.FS` behind `--enable-ui` (default off), delivered as feature-toggled vertical slices (`config/ui.yaml`). **Slice 1 (Interactions)**: open a URL, pick a persona, chat with it, and watch a channel with zero CLI knowledge — render-over-existing-API (RFC 0002 / 0011 / 0016), no new architecture. Boots off two read-only endpoints (`/api/v1/ui/config` feature toggles + `/api/v1/ui/context` `principal=local` forward-compat); the consolidated conversation panel passes session/epoch through and polls with visibility-pause + error-backoff + head-poll de-dupe. Off by default and binds `127.0.0.1` — under the default `auth.mode: disabled` the console makes the *unauthenticated* REST surface browser-discoverable, so beyond-localhost exposure requires an auth reverse proxy — or, since v0.3.12, `auth.mode: enabled` over HTTPS (RFC 0039). Later slices (memory inspector, isolation verifier, cost/observability, control plane) are v0.4.0+ / post-RFC 0039; their toggles ship off. | v0.3.6 (Slice 1) + v0.4.0+ (Slices 2–5) | ⚠️ Partially Implemented (Phase 1 / Slice 1; PRs 1 [#496](https://github.com/mkhomutov/Persatrix/pull/496), 2 [#497](https://github.com/mkhomutov/Persatrix/pull/497), 3 [#498](https://github.com/mkhomutov/Persatrix/pull/498), 4 [#501](https://github.com/mkhomutov/Persatrix/pull/501), 5 [#502](https://github.com/mkhomutov/Persatrix/pull/502), Docker/demo enablement [#503](https://github.com/mkhomutov/Persatrix/pull/503), docs/closeout (this PR) merged per the [Phase 1 PR plan](docs/rfcs/0048-phase1-pr-plan.md); Slices 2–5 deferred to v0.4.0+ / post-RFC 0039) |
| [0049](docs/rfcs/0049-memory-consolidation-gradient.md) | Memory Consolidation Gradient & Scope Reconciliation — meta-RFC over the memory tiers. Adds the vertical **consolidation-level** axis (working → episodic → semantic → procedural → experiential) on top of the horizontal scope axes settled in [memory-scope-axes.md](docs/memory-scope-axes.md), and states one law: a memory's recall scope is a function of how consolidated it is, not of which tier stores it. Raw episodic stays room-scoped; consolidated knowledge/skills/experience cross rooms, made safe at egress by the RFC 0037 classification gate rather than by walling recall. Promotes the memory-scope-axes decisions to RFC status and re-roots one of them (topic-subject facts are now cross-room — ISSUE-0084), which is what makes a persona carry project knowledge like a colleague. Hard-sequences RFC 0037 as the keystone, then cross-scope consolidation (RFC 0027) and decisions-as-readable-memory (RFC 0028). **Phases 0–1 pulled forward to v0.3.12** (cross-channel persona experience) per 2026-07-15; Phases 2–4 stay v0.4.0 (need the unimplemented RFC 0027/0028 engines). The 2026-07-15 lock also reverses ratified Non-Goal #1 — applied via the [L1 amendment](docs/rfcs/0049-amendment-l1-cross-room-availability.md) (raw episodic recall cross-room available behind the 0037 gate, room-first-ranked). | v0.3.12 (Phases 0–1) + v0.4.0 (Phases 2–4) | ⚠️ Partially Implemented — Phases 0–1 ✅ v0.3.12 **live** (capture + both widenings promoted on the green measurement verdict; [PR plan](docs/rfcs/0049-pr-plan.md) PRs 1–5); Phases 2–4 v0.4.0 (need the RFC 0027/0028 engines) |
| [0050](docs/rfcs/0050-extensible-channel-configuration.md) | Extensible Channel Configuration (Operator-Editable, Single Source of Truth) — makes the RFC 0030 per-channel governance knobs (cost ceiling, reply budget, end-vote threshold/window, salience `threshold`, `escalation_chair_id`) editable at runtime without a restart, with the channel store as the single source of truth and a revision-gated YAML loader so config-as-code and live edits coexist. **Phase 1**: persisted sparse `config_overrides_json` + store-owned `config_revision` columns (channel store v8), an `If-Match`-guarded REST PATCH/GET, the revision-gated boot reconcile, and the `persatrix channel config` CLI verbs. **Phase 2**: the web-console **Channel settings** panel + `config_edit` capability threading, with `config_edit_enabled` shipped on. The [interaction-budget-enforcement amendment](docs/rfcs/0050-amendment-interaction-budget-enforcement.md) makes `interaction_budget_tokens` router-held and server-side fail-closed in the wallet. Phase 3 (schema-driven generic config / profiles) → a future RFC. | v0.3.8 (Phases 1–2) + future RFC (Phase 3) | ✅ Implemented (Phases 1–2; [Phase 1 plan](docs/rfcs/0050-phase1-pr-plan.md) PRs [#642](https://github.com/mkhomutov/Persatrix/pull/642)–[#647](https://github.com/mkhomutov/Persatrix/pull/647), [Phase 2 plan](docs/rfcs/0050-phase2-pr-plan.md) PRs [#652](https://github.com/mkhomutov/Persatrix/pull/652)–[#654](https://github.com/mkhomutov/Persatrix/pull/654), interaction-budget enforcement [#656](https://github.com/mkhomutov/Persatrix/pull/656)–[#658](https://github.com/mkhomutov/Persatrix/pull/658), member-config edit + first-edit detachment fix (ISSUE-0103) [#655](https://github.com/mkhomutov/Persatrix/pull/655)/[#659](https://github.com/mkhomutov/Persatrix/pull/659), member-threshold web editor + `config_edit_enabled` on [#660](https://github.com/mkhomutov/Persatrix/pull/660), closeout (all four channel-config MTs run live) [#661](https://github.com/mkhomutov/Persatrix/pull/661); Phase 3 schema-driven config → future RFC) |
| [0051](docs/rfcs/0051-reasoning-before-posting.md) | Reasoning Before Posting — a private, per-turn deliberation a persona runs before it publishes a channel message. Generalizes the RFC 0030 Tier-B salience bid from a bare `speak/score` into a structured `{ should_post, plan }` verdict: a `should_post=false` ends the turn in `DO_NOTHING` *before* the expensive compose (semantic silence, net cost saving on pile-on), and a `should_post=true` threads a private `plan` (intent / key points / addressed-to / avoid-restating) into the existing compose so the post is considered, not reflexive. Reuses the leased `fast`-model seam (metered against the same `interaction_id`, idle stays free); the trace is walled (never a message, never the channel store, never a peer's RFC 0034 working memory) and audit-only. Distinct from RFC 0028 (per-turn private reasoning vs. v0.4.x action-class selection) and forward-compatible with it. Default-off per-channel `reasoning` knob on the RFC 0050 config surface. | v0.3.10 | ✅ Implemented (v0.3.10) — [RFC 0051 PR plan](docs/rfcs/0051-pr-plan.md): Phases 1–3 dark verdict/plan → config/telemetry + `off → bid` go-live flip ([#692](https://github.com/mkhomutov/Persatrix/pull/692)–[#697](https://github.com/mkhomutov/Persatrix/pull/697)) + Phase 5 reflexion default `revise: 0` ([#698](https://github.com/mkhomutov/Persatrix/pull/698)) + PR 9 closeout (reflexion no-leak wall + revise telemetry). **Phase 4 `depth: deep` deferred** (provider-protocol change + OQ-1 telemetry trigger; `validate` rejects it as unbacked); the OQ 6(a) operator reveal (PR 7) was cut as the plan's explicitly-cuttable surface. Sequenced by [v0.3.x-sequencing Amendment 2026-06-23](docs/v0.3.x-sequencing.md#amendment-2026-06-23--add-v0310-reasoning-before-posting-as-the-next-realism-rung); master plan [v0.3.10-plan.md](docs/v0.3.10-plan.md). |
| [0052](docs/rfcs/0052-autonomous-agent-channels.md) | Autonomous Agent-Only Channels — a channel that convenes a roster of personas on a topic and runs a productive, **human-free** discussion that converges, terminates, and yields a readable synthesis: the human-out-of-the-loop capstone of the v0.3.7→v0.3.10 realism arc and the project's best pre-v0.4.0 adoption demo. Reuses the shipped channel/governance/scheduling/reasoning seams (RFC 0011/0030/0024/0050/0051/0020) — **assembly (self-convening) plus three new mechanisms** (each autonomous-scoped, extending a shipped invariant): *anti-collapse cadence* (counter-pressure to the bias-to-silence defaults so human-channel defaults are untouched), a *mandatory cost cap* with a *reserved synthesis allowance* (no human circuit-breaker; the reserve is new wallet accounting with no shipped analog), and a *standing-schedule aggregate bound + timer-wiring seam*. Scoped to the **brainstorm** rung; the reason-toward-a-decision version rides v0.4.x RFC 0028. Sequenced by [v0.3.x-sequencing Amendment 2026-06-28](docs/v0.3.x-sequencing.md#amendment-2026-06-28--add-the-autonomous-agent-only-channel-as-the-v03x-realism-capstone); pinned to v0.3.11 (all four phases) at [plan opening](docs/v0.3.11-plan.md). OQs resolved there: distinct convener, anti-collapse scoped to autonomous, the closing summary metered (roster-scaled `1+N` reserve). | v0.3.11 | ✅ Implemented — all four phases landed across PRs 1–9 ([PR plan](docs/rfcs/0052-pr-plan.md)); PR 9 closeout = the four-vendor headline blueprint + `MT-AUTONOMOUS-MULTIPROVIDER-001`. Live acceptance MTs run at release-prep (OQ #5 defaults calibration → [ISSUE-0109](docs/issues/ISSUE-0109-rfc0052-autonomous-defaults-calibration.md)). [v0.3.11 plan](docs/v0.3.11-plan.md). |
| [0053](docs/rfcs/0053-gemini-watsonx-providers.md) | Gemini and watsonx.ai LLM Providers — add Google Gemini and IBM watsonx.ai as first-class providers, the second concrete dogfood of the [RFC 0033 §H](docs/rfcs/0033-model-alias-layer.md#h-multi-provider-extensibility) multi-provider extensibility seam (one provider class + one `create_provider` branch + priced alias entries each — the same "Any Model, Any Provider" axis as v0.3.4). Brings the configurable cloud roster to four vendors (Anthropic / OpenAI / Gemini / watsonx.ai) plus local Ollama + offline mock. Non-local → priced (the missing-price guard keeps the RFC 0023 budget gate live); `make demo-gemini` / `demo-watsonx` + compose parity. **Bundled with RFC 0052** because its motivating consumer is that rung's flagship demo — a four-vendor human-free brainstorm. Independently shippable (RFC 0052 runs on any single provider), so cuttable from v0.3.11 — if a provider SDK drags, the four-vendor headline slips a point release. Sequenced by [v0.3.x-sequencing Amendment 2026-06-28](docs/v0.3.x-sequencing.md#amendment-2026-06-28--add-the-autonomous-agent-only-channel-as-the-v03x-realism-capstone); pinned to v0.3.11 (OQ #1 resolved: native `google-genai`) at [plan opening](docs/v0.3.11-plan.md). | v0.3.11 (bundled with RFC 0052, cuttable) | ✅ Implemented — all 3 PRs landed (Gemini [#731](https://github.com/mkhomutov/Persatrix/pull/731) → watsonx [#732](https://github.com/mkhomutov/Persatrix/pull/732) → closeout: combined `providers` extra + provider-guide finalize). The four-vendor human-free brainstorm it enables is [RFC 0052 PR 9](docs/rfcs/0052-pr-plan.md). [PR plan](docs/rfcs/0053-pr-plan.md). |

---

## v0.1.0 — Core Engine

**What a user can do**: Submit YAML workflows via CLI, orchestrator plans and schedules stages, dispatches tasks to Python agents over gRPC, agents call LLMs and tools, results flow back.

**Status**: ✅ Complete — internal baseline. Not publicly released; project was renamed to Persatrix before first public release.

### RFC Scope

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0001](docs/rfcs/0001-core-orchestration-pipeline.md) | Core Orchestration Pipeline (Planner + State + Registry) | ✅ Implemented | 6+1 | 7/7 |
| [0002](docs/rfcs/0002-rest-api-server.md) | REST API Server (HTTP Layer + Workflow Submission) | ✅ Implemented | 4 | 4/4 |
| [0003](docs/rfcs/0003-scheduler-executor.md) | Scheduler & Executor (Parallel Stage Execution + gRPC Dispatch) | ✅ Implemented | 7+4 | 11/11 |
| [0004](docs/rfcs/0004-python-agent-grpc-server.md) | Python Agent gRPC Server (AgentService Implementation) | ✅ Implemented | 7 | 7/7 |

### Dependency Chain

```
RFC 0001 (State, Registry, Planner)           ✅ Done (6 core + 1 follow-up = 7/7)
    ↓
RFC 0002 (REST API Server)                    ✅ Done
    ↓
RFC 0003 (Scheduler + Executor + gRPC)        ✅ Done (7 core + 4 follow-up = 11/11)
    ↓
RFC 0004 (Python Agent Server + Tools)        ✅ Done (7/7)
    ↓
v0.1.0 complete — end-to-end execution working
```

### Component Status

#### Go Orchestrator (`internal/`)

| Package | Purpose | Status |
|---------|---------|--------|
| `internal/state/` | Workflow/step state tracking | ✅ Complete (100% coverage) |
| `internal/registry/` | Agent registration and lookup | ✅ Complete (~95% coverage) |
| `internal/planner/` | YAML parsing, DAG validation, topological sort | ✅ Complete (100% coverage) |
| `internal/server/` | REST API (11 endpoints, middleware, graceful shutdown) | ✅ Complete (86.5% coverage) |
| `internal/scheduler/` | Workflow scheduling (pick up pending runs, drive stages) | ✅ Complete (87.3% coverage) |
| `internal/executor/` | gRPC task dispatch to agents | ✅ Complete (96.1% coverage) |
| `internal/generated/` | Protobuf/gRPC generated code | ✅ Complete (generated stubs) |
| `internal/resilience/` | Circuit breaker, dead letter queue | 🔲 TODO stub (post-v0.1) |
| `internal/security/` | Audit logger, redactor, rate limiter, circuit breaker, REST/gRPC middleware, input sanitizer | 🚧 In progress (v0.3.0 — RFC 0009 PRs 1/1b/1c/2/3) |
| `internal/observability/` (renamed from `internal/telemetry/`) | OTEL span instrumentation + structured logging encoder + log buffer + LogService | ✅ Complete (RFC 0018 + RFC 0019, shipped in v0.2.3) |
| `internal/cost/` | Token/cost tracking aggregation | ✅ Complete (RFC 0006) |

#### Python Agents (`agents/`)

| Module | Purpose | Status |
|--------|---------|--------|
| `agents/base.py` | BaseAgent ABC + dataclasses + LLM loop | ✅ Complete (RFC 0004 PR 4a) |
| `agents/llm_client.py` | Multi-provider LLM client (Anthropic + OpenAI) | ✅ Complete (RFC 0004 PR 4a) |
| `agents/server.py` | gRPC service entry point + self-registration | ✅ Complete (RFC 0004 PR 5a+5b) |
| `agents/task_agent.py` | Data-driven task agent (replaces CoderAgent, ReviewerAgent, PlannerAgent) | ✅ Complete (RFC 0005 PR 1a) |
| `agents/tools/registry.py` | Tool discovery and registration | ✅ Complete (decorator + registry) |
| `agents/tools/builtin.py` | Built-in tools (file_read, file_write, shell_exec, http_request, memory tools) | ✅ Complete (RFC 0004 PR 3, RFC 0005 PR 3b) |
| `agents/tools/permissions.py` | Deny-by-default permission gate | ✅ Complete (97% coverage) |
| `agents/tools/sandbox.py` | Filesystem path restriction (PathValidator) | ✅ Complete (100% coverage) |
| `agents/generated/` | Python gRPC generated stubs | ✅ Complete (RFC 0004 PR 5a) |

#### Rust CLI (`cli/`)

| Module | Purpose | Status |
|--------|---------|--------|
| `cli/src/main.rs` | CLI entry point, clap definitions, command dispatch | ✅ Functional |
| `cli/src/types.rs` | API request/response types, shared validation helpers | ✅ Complete |
| `cli/src/commands/workflow.rs` | Workflow commands (run, status) | ✅ Complete |
| `cli/src/commands/agent.rs` | Agent commands (list, info, reload, test persona) | ✅ Complete |
| `cli/src/commands/logs.rs` | Execution log viewing | ✅ Complete |
| `cli/src/commands/validate.rs` | Config validation (Python subprocess) | ✅ Complete |

### What Works in v0.1.0

1. Submit a workflow via CLI → `POST /api/v1/workflows/run`
2. Orchestrator receives request, planner parses YAML, validates DAG, generates execution plan
3. Server creates `WorkflowRun` in state store → status = Pending
4. Scheduler polls for pending runs, transitions to Running, drives parallel stage execution
5. Executor dispatches tasks to agents via gRPC `ExecuteTask` with retry logic
6. Step outputs resolve across stages via `{{ steps.<key>.output }}` templates
7. Poll `GET /api/v1/workflows/{id}/status` → returns Running/Completed/Failed with step details
8. CRUD operations on agents via REST API

---

## v0.2.0 — Persona Core ⭐ First Public Release

**What a user can do**: Run persistent AI agents with real personalities, memory, and evolving relationships from a terminal.

### What ships in v0.2.0

- **PersonaAgent** — full behavioral model: personality dimensions, mood, stress, goals (RFC 0005)
- **Three-tier memory** — episodic (SQLite + FTS5), relationship (trust + interaction history), working (context window management) (RFC 0005)
- **Autonomous tick loop** — agents act without being prompted (RFC 0005)
- **Relationship dynamics** — trust scores that evolve based on interactions (RFC 0005)
- **Budget controls** — token caps, spend limits, deadline enforcement, pre-dispatch budget gating (RFC 0006)
- **Execution observability** — per-step token usage, LLM call count, retry count, estimated cost in API responses (RFC 0006)
- **CLI** — `run`, `inspect`, and `observe` persona agents from the terminal (RFC 0005)

### What does not ship in v0.2.0

- Agent-to-agent conversations and channels (RFC 0011) → v0.3.0
- Conditional and looped workflow control flow (RFC 0007) → v0.4.0
- Agent memory and context optimization for non-persona agents (RFC 0008) → v0.3.0
- Security hardening beyond existing deny-by-default tool gates (RFC 0009) → v0.3.0
- Sub-agent spawning (RFC 0010) → v0.4.0
- Organizational hierarchy, roles, escalation (RFC 0012) → v0.4.0
- Skill registry and lifecycle governance (RFC 0014) → v0.4.0
- External bridges — Slack, Discord, Telegram, email (RFC 0011) → v0.5.0
- Compliance and privacy layer (RFC 0013) → v0.5.0
- Process automation and pattern extraction (RFC 0015) → v0.5.0
- Distributed mesh (v0.6.0)
- Web dashboard

### RFC Scope

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0005](docs/rfcs/0005-persona-agent-memory.md) | Persona Agent & Memory System | ✅ Implemented | 20 | 20/20 |
| [0006](docs/rfcs/0006-efficiency-execution-limits.md) | Efficiency & Execution Limits | ✅ Implemented | 12 | 12/12 |

### RFC 0006 — Execution Progress

```
RFC 0005 (PersonaAgent + Memory + TaskAgent)              ✅ Done (20/20)
    ↓
RFC 0006 (Efficiency & Execution Limits)                  ✅ Done (12/12)
    PR 1a — defaults package + Step limits + schema       ✅ #79
    PR 1b — executor + scheduler limit wiring             ✅ #81
    PR 1c — Python defaults + validation                  ✅ #83
    PR 2  — deadline derivation + retry budget            ✅ #84
    PR 3a — TokenCounter + BudgetEnforcer                 ✅ #85
    PR 3b — CostReporter + scheduler budget integration   ✅ #86
    PR 4a — StepExecutionMetadata + observability         ✅ #87
    PR 4b — response cache + cost endpoint                ✅ #88
    PR 5a — executor + scheduler + state follow-ups       ✅ #90
    PR 5b — cost package hardening                        ✅ #91
    PR 5c — planner/schema + Python fixes                 ✅ #92
    PR 6  — PR 5c follow-ups + RFC close                  ✅ #93
    ↓
v0.2.0 complete
```

> All 12 PRs merged. RFC 0006 closed.

### Component Status

#### Go Orchestrator (`internal/`) — v0.2.0 additions

| Package | Purpose | Status |
|---------|---------|--------|
| `internal/defaults/` | Centralized execution limit constants | ✅ Complete (RFC 0006 PR 1a) |
| `internal/planner/` | Step-level limit fields (`TimeoutSeconds`, `MaxLLMCalls`, `MaxTokens`, `ContextBudget`) | ✅ Updated (RFC 0006 PR 1a) |
| `internal/executor/` | Full `TaskConfig` population, derived deadlines, shared-deadline retry, response cache | ✅ Complete (RFC 0006 PRs 1b+2+4a+4b) |
| `internal/scheduler/` | Limit cascade (step → agent → defaults), pre-dispatch budget gate, token recording, metadata | ✅ Complete (RFC 0006 PRs 1b+3b+4a) |
| `internal/cost/` | `TokenCounter`, `BudgetEnforcer`, `CostReporter`, response cache | ✅ Complete (RFC 0006 PRs 3a+3b+4b) |
| `internal/state/` | `StepExecutionMetadata` (tokens, LLM calls, retries, cache hit, cost, wall time) | ✅ Complete (RFC 0006 PR 4a) |
| `internal/server/` | Cost summary endpoint (`GET /api/v1/cost/summary`) | ✅ Complete (RFC 0006 PR 4b) |
| `internal/observability/` (renamed from `internal/telemetry/`) | OTEL span instrumentation + structured logging encoder + log buffer + LogService | ✅ Complete (RFC 0018 + RFC 0019, shipped in v0.2.3) |

#### Python Agents (`agents/`) — v0.2.0 additions

| Module | Purpose | Status |
|--------|---------|--------|
| `agents/memory/working.py` | Working memory (context window management, priority retention, compression) | ✅ Complete (RFC 0005 PR 2) |
| `agents/memory/episodic.py` | Episodic memory (SQLite, FTS5, episode CRUD, recall, summarization, delegates notes to NoteStore) | ✅ Complete (RFC 0005 PR 3a+3b+3c, refactored PR 8b) |
| `agents/memory/notes.py` | Agent-initiated note storage (NoteStore, CRUD, FTS5/LIKE search, pruning) | ✅ Complete (RFC 0005 PR 8b) |
| `agents/memory/migrations.py` | Schema migrations, FTS5 DDL, scoring SQL constants | ✅ Complete (RFC 0005 PR 8b) |
| `agents/memory/relationship.py` | Relationship memory (trust tracking, interaction history, bidirectional decay) | ✅ Complete (RFC 0005 PR 4) |
| `agents/persona.py` | PersonaAgent ABC, `create_persona_agent()` factory, re-exports | ✅ Complete (RFC 0005 PR 5a+5b, refactored PR 8a+8d) |
| `agents/persona_runtime.py` | `_LLMPersonaAgent` concrete class (LLM-powered event loop, memory injection, tool use) | ✅ Complete (RFC 0005 PR 8d) |
| `agents/persona_types.py` | Persona type definitions (`PersonaState`, `Mood`, `AgentEvent`, `EventType`, `AgentAction`, `ActionType`) | ✅ Complete (RFC 0005 PR 8a) |
| `agents/persona_behavior.py` | Behavioral dimension rendering (`render_behavior`, `DIMENSION_DESCRIPTIONS`) | ✅ Complete (RFC 0005 PR 8a) |
| `agents/dispatch.py` | Event dispatch and action execution (`EventDispatcher`, `ActionExecutor`) | ✅ Complete (RFC 0005 PR 8a) |
| `agents/tick.py` | Autonomous tick scheduler (`TickScheduler`) | ✅ Complete (RFC 0005 PR 8a) |
| `agents/validate.py` | Config validation (JSON Schema) | ✅ Complete (RFC 0005 PR 6a) |
| `agents/defaults.py` | Python execution limit constants (centralizes magic numbers from `base.py`) | ✅ Complete (RFC 0006 PR 1c) |

### What Works in v0.2.0 (RFC 0005 complete)

1. Configure a persona agent in `config/agents.yaml` with personality dimensions, mood, goals, and memory settings
2. Start the agent gRPC server — agent self-registers with the orchestrator
3. Agent's autonomous tick loop fires on a configurable interval — it generates actions and events without external prompts
4. Events (user messages, tick events, relationship events) are dispatched to `on_event()`
5. Each interaction persists to episodic memory (SQLite) and updates the relationship trust score for the sender
6. Working memory manages the context window — high-priority items are retained, excess is summarized
7. CLI commands: `persatrix agent test-persona`, `persatrix agent info`, `persatrix agent list`
8. Relationships evolve over time: trust decays when agents don't interact, grows with positive interactions

---

## v0.2.1 — Talk to Your Agents ✅ Complete

**What a user can do**: Open a terminal, type `persatrix chat <agent_id>`, and have a conversation with a persona agent. The agent remembers you and builds a relationship with you over time.

### What ships in v0.2.1

- **`Participant` abstraction** — `Participant` Protocol generalising agents, users, and future system actors (RFC 0016)
- **`UserParticipant`** — persistent user identity stored in the agent SQLite database (RFC 0016)
- **Memory generalization** — `RelationshipMemory` extended to track trust and interactions with human users; `EpisodicMemory` records user-agent exchanges (RFC 0016)
- **`persatrix chat` CLI command** — interactive REPL for conversations with persona agents (RFC 0016)
- **Chat REST endpoint** — `POST /api/v1/agents/{id}/chat` for synchronous message-response round-trips (RFC 0016)
- **`SendChatMessage` gRPC RPC** — new `AgentService` method for orchestrator→agent chat routing (RFC 0016)

### What does not ship in v0.2.1

- Multi-user support — single `UserParticipant` per session; multi-user support is RFC 0011 (v0.3.0)
- Authentication — sessions are local and caller-supplied; auth is RFC 0009 (v0.3.0)
- Agent-initiated messages to users — notification infrastructure deferred
- Streaming chat responses — synchronous request-response only for v0.2.1
- Channel routing for user messages — channels are RFC 0011 (v0.3.0)

### RFC Scope

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0016](docs/rfcs/0016-human-participant-chat-interface.md) | Human Participant & Chat Interface | ✅ Implemented | 7 | 7/7 |

### Dependency Chain (v0.2.1)

```
v0.2.0 complete (RFC 0005 ✅, RFC 0006 ✅)
    ↓
RFC 0016 Phase 1 (Participant abstraction + memory generalization)
    ↓
RFC 0016 Phase 2 (proto + gRPC + REST wiring)
    ↓
RFC 0016 Phase 3 (persatrix chat CLI command)
    ↓
v0.2.1 complete
```

> **Why this is a minor release and not part of v0.3.0**: The human participation primitive is architecturally independent of channels (RFC 0011). It reuses the RFC 0005 memory and dispatch system without modification. Shipping it as v0.2.1 gives v0.2.0 users something immediately useful and generates real-world feedback on persona behavior before the larger v0.3.0 channel work begins.

### Planned Components (v0.2.1)

| Component | Go Package | Python Module | Target RFC |
|-----------|-----------|---------------|------------|
| `Participant` Protocol + `UserParticipant` | — | `agents/participant.py` | 0016 | ✅ Complete (PR #119) |
| Memory generalization | — | `agents/memory/relationship.py`, `agents/memory/migrations.py` | 0016 | ✅ Complete (PR #120) |
| Chat REST endpoint | `internal/server/` | — | 0016 | ✅ Complete (PR #123) |
| Chat gRPC dispatch | `internal/executor/` | `agents/server_servicers.py` | 0016 | ✅ Complete (PR #121) |
| `persatrix chat` CLI | — | — | 0016 (`cli/src/commands/chat.rs`) | ✅ Complete (PR #125) |

---

## v0.2.2 — Bounded Persona Memory ✅ Complete

**What a user can do**: Persona agents now operate with a deterministic per-event memory token budget — predictable context size, lower per-tick cost, and no more silent spending when nothing is happening.

### What ships in v0.2.2

- **`MemoryBudget` allocator** — per-event token ceiling distributing the available budget across episodic, relationship, and notes tiers (RFC 0017 §B)
- **`_inject_memory_context` rewrite** — allocate-loop replaces ad-hoc injection; budget tracked uniformly across all memory types (RFC 0017 §C)
- **`min_score` relevance threshold** — `EpisodicMemory.recall` / `recall_notes` accept `min_score` and filter out low-scoring matches before injection; legacy opaque gates removed (RFC 0017 §D)
- **Empty-context TICK short-circuit** — when a TICK fires with no admitted memory, no active goal, and no pending conversation turn, the LLM call is skipped and `idle_count` is incremented (RFC 0017 §F)

### What does not ship in v0.2.2

- Operator-facing config for `_MEMORY_BUDGET_TOKENS` — per-event budget constant is not yet exposed as a per-agent config field; deferred pending demand
- RFC 0008 (Memory & Context Optimization) — structural prerequisite now met; RFC 0008 implementation planned for v0.3.0

### RFC Scope

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0017](docs/rfcs/0017-persona-memory-injection-budget.md) | Persona Memory Injection Token Budget | ✅ Implemented | 7 | 7/7 |

### Dependency Chain (v0.2.2)

```
v0.2.1 complete (RFC 0016 ✅)
    ↓
RFC 0017 Phase B (MemoryBudget allocator + allocate-loop rewrite)
    ↓
RFC 0017 Phase D (min_score relevance threshold)
    ↓
RFC 0017 Phase F (empty-context TICK short-circuit)
    ↓
v0.2.2 complete
```

---

## v0.2.3 — Observability Foundation ✅ Complete

**What a user can do**: Observe your agent society end-to-end — structured JSON logs on a versioned schema across Go, Python, and CLI; distributed traces from REST handler to LLM call with OTEL Gen-AI semantic conventions; OTLP metrics with histogram exemplars; W3C Baggage propagation across the gRPC boundary; a tail-sampling Collector pipeline. Combined deliverable of RFCs 0018 + 0019.

### What ships in v0.2.3

- **`internal/observability/` Go package** — `internal/telemetry/` renamed verbatim; all OTEL instrumentation consolidated under the new name (RFC 0019 PR 1)
- **Python OTEL initialisation** — `agents/observability/tracing.py` with `init_tracing()` / `shutdown()`, Resource attributes, `BatchSpanProcessor`, and a `CompositePropagator(TraceContext + Baggage)` registered globally (RFC 0019 PR 1)
- **gRPC trace + baggage propagation** — Go executor injects `otelgrpc` client handler; Python server registers `GrpcInstrumentorServer`; baggage entries readable inside handlers (RFC 0019 PR 1)
- **otelhttp handler wrap** — orchestrator HTTP handler wrapped with `otelhttp.NewHandler` (RFC 0019 PR 1)
- **Semantic spans** — tick loop, event dispatch, memory ops, LLM calls (Gen-AI conventions), tool execution (RFC 0019 PR 2)
- **Span Links** — A2A and sub-agent causality (RFC 0019 PR 2)
- **OTLP metrics** — counters, histograms, gauges with exemplars on both Go and Python sides (RFC 0019 PR 3)
- **Structured JSON logs** — Go (zap) and Python (structlog) on a versioned schema with a log-record redactor surface (RFC 0018 PRs 1–2)
- **Log↔trace correlation** — structlog/zap enricher writes `trace_id` + `span_id` + known baggage entries into every log record (RFC 0018 PR 3)
- **Collector tail-sampling pipeline** — reference `config/observability/otel-collector.yaml`; docker-compose adds Collector, Prometheus, Loki (dev) (RFC 0019 PR 4)
- **`persatrix logs` CLI rewrite** — `--follow`, server-side filters, `--trace <id>` correlation (RFC 0018 PR 6)

### What does not ship in v0.2.3

- Distributed mesh telemetry (v0.6.0)
- Per-agent operator dashboard / alerting rules

### RFC Scope

| RFC | Title | Status | PRs | Merged |
|-----|-------|--------|-----|--------|
| [0019](docs/rfcs/0019-opentelemetry-completion.md) | OpenTelemetry Completion | ✅ Implemented | 5 | 5/5 |
| [0018](docs/rfcs/0018-structured-logging-framework.md) | Structured Logging Framework | ✅ Implemented | 7 | 7/7 |

### Joint Merge Order (RFCs 0018 + 0019)

```
0019 PR 1 (Phase 1 — telemetry→observability rename + Python OTEL init + gRPC + Baggage)  ✅ #163
  ↓
0018 PR 1 (Phase 1 — Python structlog + schema doc + redactor surface)  ✅ #164
  ↓
0018 PR 2 (Phase 2 — Go zap rename + pretty + redactor wired + source)  ✅ #165
  ↓
0019 PR 2 (Phase 2 — semantic spans + Span Links)  ✅ #167
  ↓
0018 PR 3 (Phase 3 — cross-process correlation + OTEL trace IDs on logs)  ✅ #168
  ↓
0019 PR 3 (Phase 3a — metrics)  ✅ #170
  ↓
0019 PR 4 (Phase 3b — Collector + docker-compose + E2E + schema-parity test)  ✅ #171
  ↓
0018 PR 4 (Phase 4a — proto/log_service.proto + ring buffer + disk store)  ✅ #172
  ↓
0018 PR 5 (Phase 4b — LogService server + agent shipper + REST + SSE)  ✅ #173
  ↓
0018 PR 6 (Phase 4c — CLI rewrite + E2E)  ✅ #174
  ↓
0018 PR 8 + 0019 PR 6 (post-merge polish — logbuffer/shipper + tracing/spans cluster)  ✅ #177 (0018 PR 8) + ✅ #176 (0019 PR 6)
  ↓
0018 PR 7 + 0019 PR 5 (review follow-ups + RFC close, opened together as a paired closeout)  ✅ #180 (0018 PR 7) + ✅ #181 (0019 PR 5)
```

### Planned Components (v0.2.3)

| Component | Go Package | Python Module | Target RFC |
|-----------|-----------|---------------|------------|
| OTEL traces + gRPC propagation | `internal/observability/` | `agents/observability/tracing.py` | 0019 PR 1 ✅ |
| Semantic spans + Span Links | `internal/observability/` | `agents/observability/` | 0019 PR 2 ✅ |
| OTLP metrics | `internal/observability/metrics/` | `agents/observability/metrics.py` | 0019 PR 3 ✅ |
| Collector pipeline | `config/observability/` | — | 0019 PR 4 ✅ |
| Structured logs (Python) | — | `agents/observability/logging.py` | 0018 PR 1 ✅ |
| Structured logs (Go) | `internal/observability/zapenc/` | — | 0018 PR 2 ✅ |
| Log↔trace correlation | `internal/observability/` | `agents/observability/` | 0018 PR 3 ✅ |
| Log storage + shipper | `internal/observability/` | `agents/observability/` | 0018 PR 4 ✅ / PR 5 ✅ |
| LogService REST + SSE endpoints | `internal/server/` | `agents/observability/log_shipper.py` | 0018 PR 5 ✅ |
| `persatrix logs` CLI rewrite | `cli/src/commands/logs.rs` | — | 0018 PR 6 ✅ |

---

## v0.3.0 — Agent Conversations ✅ Complete

**What a user can do**: Give agents a shared channel and watch them talk, negotiate, and form opinions about each other over time.

### What ships in v0.3.0

- **Internal channels** — group messages, DMs, threads; agents can address each other and reply (RFC 0011, internal part)
- **Channel history** visible to agents via memory integration
- **Multi-agent conversation routing** — message delivery, acknowledgement, threading
- **Channels CLI + human participation** — `persatrix channel list/join/send/reply/history/watch`; human operators can join channels and observe agent traffic (RFC 0011 Phase 4)
- **Interaction lifecycle** — dialogues (not individual messages) become the unit of episodic memory and summarization; structural + idle-gap boundary detection; per-channel scoping (RFC 0020)
- **Persona temporal awareness — Phase 1** — now-anchor in every prompt, recency-rendered episode recall, last-seen rendering on relationships (RFC 0021 Phase 1)
- **Agent memory and context optimization** — per-step context budget allocation, caller-prepared context packaging, delegation result merge contracts (RFC 0008)
- **Security hardening Phases 1–2** — audit logging, rate limiting, input sanitization (RFC 0009)

### Memory Quality Roadmap

The persona-memory subsystem failed a qualitative review on 2026-05-01 (the "dementia test" — see [memory-quality-roadmap.md](docs/memory-quality-roadmap.md)). The ratified follow-up plan rides v0.3.x and v0.4.0 alongside the six in-flight RFCs *without* expanding v0.3.0 scope. Tracked deliverables:

- **§A — Declarative Facts Tier** ([RFC 0026](docs/rfcs/0026-declarative-facts-tier.md)) — v0.3.x, new RFC.
- **§B — Continuity bridge across interaction close** — v0.3.x, no RFC, single PR.
- **§C — Salience score with use-based reinforcement** — v0.3.x, folded into the [RFC 0008 calibration review](docs/rfcs/0008-calibration-review.md).
- **§D — Outcome-tagged importance** — v0.3.x, resolves [RFC 0020 OQ #6](docs/rfcs/0020-interaction-lifecycle.md#open-questions); pinned in [`0020-pr-plan.md`](docs/rfcs/0020-pr-plan.md).
- **§E — Reflection-driven consolidation** ([RFC 0027](docs/rfcs/0027-reflection-driven-consolidation.md)) — v0.4.0, supersedes draft RFC 0025.
- **§F — Structured "since we last spoke" prompt header** — v0.3.x, single PR following [RFC 0021 P1](docs/rfcs/0021-persona-temporal-awareness.md).
- **§G — Dementia-test manual artifact** ([MT-MEMORY-005](docs/manual-tests/MT-MEMORY-005-dementia-test.md)) — v0.3.0 release-prep gate.

Sequencing and rationale live in [v0.3.0-plan.md §Memory Quality Follow-Ups](docs/v0.3.0-plan.md#memory-quality-follow-ups-v03x-and-beyond).

### RFC Scope

| RFC | Title | Target scope | Status |
|-----|-------|--------------|--------|
| [0008](docs/rfcs/0008-agent-memory-context-optimization.md) | Agent Memory & Context Optimization | Full RFC | ✅ Implemented |
| [0009](docs/rfcs/0009-security-sandboxing.md) | Security & Sandboxing | Phases 1–2 (audit, rate limiting, sanitization) | 🚧 Implementing |
| [0011](docs/rfcs/0011-channels-bridges.md) | Channels + Bridges | Internal channels (Phases 1–4: routing, history, memory integration, CLI/human participation) | ⚠️ Partially Implemented (internal channels — external bridges deferred to v0.5.0) |
| [0020](docs/rfcs/0020-interaction-lifecycle.md) | Interaction Lifecycle | Phases 1–3 (P4 topic-shift deferred) | ✅ Implemented |
| [0021](docs/rfcs/0021-persona-temporal-awareness.md) | Persona Temporal Awareness | Phase 1 only (now-anchor + recency rendering) | ⚠️ Partially Implemented (Phase 1) |

> **Note (2026-05-06)**: RFC 0007 (Conditional & Looped Workflow Control Flow) was originally scoped to v0.3.0 and has been retargeted to v0.4.0. v0.3.0's user-facing promise — *agents talk, negotiate, form opinions* — is conversation infrastructure; conditional/looped workflow control flow is workflow-engine plumbing that pairs with v0.4.0's sub-agent spawning (RFC 0010) and skill-registry (RFC 0014) work, where iterative refinement and branching on child-agent outputs are the load-bearing cases. RFC 0008 (the prerequisite) ships fully in v0.3.0, so the dep is satisfied at v0.4.0-start.

### Dependency Chain (v0.3.0)

```
v0.2.3 complete
    │
    ├── RFC 0020 P1 (Interaction tracker + additive schema)    [no v0.3.0 deps; starts immediately]
    │       │
    │       ├── RFC 0021 P1 (now-anchor + recency rendering)   [consumes 0020 P1's started_at/closed_at; independent of 0008/0011]
    │       │
    │       └── RFC 0020 P2 (summarize-on-close + janitor)     [pairs with RFC 0008 §D — interaction-bounded summarization]
    │
    ├── RFC 0008 (Memory & Context Optimization)               [prerequisite for RFC 0011 P3; coordinates with RFC 0020 P2]
    │       │
    │       └── RFC 0011 — internal channels only              [parallel workstream; P1–2 independent; P3 needs RFC 0008 P2]
    │              │
    │              └── RFC 0011 P3 + RFC 0020 P3 (joint)       [channel memory becomes interaction-scoped]
    │
    └── RFC 0009 P1–2 (Audit, Rate Limiting, Input Sanitization)  [runs throughout — no blocking dependency on 0011/0020]
            ↓
v0.3.0 complete (all five RFC scopes delivered: 0008, 0009 P1–2, 0011 internal, 0020, 0021 P1)
```

#### Why RFC 0020 Phase 1 starts immediately, ahead of RFC 0008 §D

Interactions are the *unit* RFC 0008 will summarize and RFC 0011 will store as channel history. Landing the tracker + schema (Phase 1) first means every multi-turn dialogue is bounded correctly from day one — no per-message episode debt that has to be migrated later. Phase 1 is pure scaffolding (no LLM, no behavior change), so it carries minimal risk and unblocks both RFC 0008's compression pipeline and RFC 0011's memory integration.

#### Why RFC 0020 P2 pairs with RFC 0008 §D

The summarize-on-close hook calls into RFC 0008's compression pipeline. Coordinating delivery avoids an awkward window where RFC 0008 ships per-message summarization that RFC 0020 then has to displace. The interface is small (RFC 0020 emits "interaction closed" events; RFC 0008 §D consumes them as the trigger to compress).

#### Why RFC 0020 P3 is jointly delivered with RFC 0011 P3

Channels multiply the per-message-episode problem by N participants. Per-channel scoping (DM = pair, thread = thread, group = rolling per-channel-per-agent) must land *with* channel memory integration, not after — otherwise the first cut of channel history would inherit the wrong episode granularity.

#### Why RFC 0009 Phases 1–2 run alongside, not before

Audit logging and rate limiting are foundational safety infrastructure with no RFC 0011/0020 dependency. They can develop concurrently and are integrated progressively (rate limiting into channel REST endpoints in RFC 0011 Phase 1; input sanitization into channel message storage in Phase 3). Phases 3–4 (identity tokens, HITL gates) are prerequisites for sub-agent spawning and are deferred to v0.4.0.

#### Why RFC 0021 Phase 1 lands in v0.3.0, with Phases 2–4 deferred to v0.4.0

Phase 1 is small, self-contained, and high-leverage — a now-anchor in the system prompt and recency-rendered recall make every channel conversation under RFC 0011 carry temporal annotation from day one. Without it, RFC 0011 ships a channel-history experience where agents cannot tell whether a recalled exchange happened minutes or weeks ago. Phase 1 depends only on RFC 0020 Phase 1's `started_at` / `closed_at` columns; no other v0.3.0 RFC blocks or is blocked by it. Phases 2–4 (commitments, REMINDER event, duration calibration) are a coherent forward-memory + estimation surface that pairs naturally with v0.4.0's organizational and skill-registry work — agents that can plan are also agents that can hold roles.

### Planned Components (v0.3.0)

| Component | Go Package | Python Module | Target RFC |
|-----------|-----------|---------------|------------|
| Agent Memory & Context Optimization | `internal/scheduler/`, `internal/executor/` | `agents/memory/`, `agents/task_agent.py` | 0008 |
| Security & Sandboxing (P1–2) | `internal/security/` | `agents/security.py` | 0009 |
| Internal Channels | `internal/channels/`, `internal/executor/` | `agents/server_servicers.py`, `agents/dispatch.py`, `agents/persona_types.py`, `agents/memory/` | 0011 |
| Channels CLI (Rust) | `cli/src/commands/channel.rs`, `cli/src/main.rs` | — | 0011 |
| Interaction Lifecycle | — | `agents/memory/interactions.py`, `agents/memory/episodic.py`, `agents/memory/relationship.py`, `agents/memory/relationship_mutations.py`, `agents/persona_runtime/`, `agents/dispatch.py` | 0020 |
| Persona Temporal Awareness (P1) | — | `agents/clock.py`, `agents/temporal/`, `agents/persona_runtime/prompt_assembly.py`, `agents/persona_runtime/memory_context.py`, `agents/memory/relationship.py` | 0021 |
| Observability (spans + metrics) | `internal/observability/` | `agents/observability/` | 0019 |

---

## v0.4.0 — Agent Organizations

**What a user can do**: Define a company, research lab, or team with roles and hierarchy — and let it run.

### What ships in v0.4.0

> **The usefulness ladder.** v0.3.7–v0.3.9 take the conversation from *chatter* to a **brainstorm** that converges and produces a readable outcome (minimal usefulness). v0.4.0 adds the next rung — **deliberative reasoning**: agents reason explicitly and auditably toward a justified recommendation via the decision engine (RFC 0028), rather than only generating and converging on ideas. *Collective* (quorum) decisions and the full convene→propose→deliberate→decide→ratify meeting protocol are the v0.5.0 rung (RFC 0028 Phase 4 + RFC 0012 Phase 4). See the [v0.3.x sequencing amendment 2026-06-04](docs/v0.3.x-sequencing.md#amendment-2026-06-04--re-sequence-the-v03x-tail-for-conversation-realism--usefulness-ahead-of-v040).

- **Deliberative reasoning** — agents reason through tool/delegation/publish decisions via explicit, auditable checkpoints, so a group conversation can reach a *reasoned* recommendation, not just a brainstorm (RFC 0028 Phases 1–3)
- **Organizational topologies** — hierarchy, flat, matrix; authority rules and escalation paths (RFC 0012 partial)
- **Sub-agent spawning** — ephemeral agents with narrowed, orchestrator-issued permission tokens (RFC 0010)
- **Security Phases 3–4** — tool validation, agent identity tokens, HITL gates (RFC 0009)
- **Skill Registry** — `SkillSpec` model, `SkillCatalogue`, skill validation, failure modes, fallback chains (RFC 0014)
- **Meeting and negotiation protocol scaffolding** (RFC 0012 partial)
- **Persona temporal awareness — Phases 2–4** — commitments memory class with `due_at` lifecycle, `REMINDER` tick-loop event, time-tool surface (`get_current_time`, `time_since`, `time_until`, `set_reminder`), duration calibration store with `recall_typical_duration` (RFC 0021 Phases 2–4)
- **Conditional and looped workflow control flow** — skip semantics, bounded repeat-until, for-each (RFC 0007) — retargeted from v0.3.0; pairs with sub-agent spawning and skill-registry work where iterative refinement and conditional branching on child-agent outputs are the load-bearing cases
- **Account administration & auth hardening** — operator account-management REST API, self-service password change, failed-login lockout (RFC 0039 Phase 3); the accounts, password-login, and role-gate foundation and the verified `participant_id` claim ship in v0.3.12

### RFC Scope

| RFC | Title | Target scope | Status |
|-----|-------|--------------|--------|
| [0007](docs/rfcs/0007-conditional-looped-workflow-control-flow.md) | Conditional & Looped Workflow Control Flow | Full RFC (retargeted from v0.3.0 on 2026-05-06) | 📋 Proposed |
| [0009](docs/rfcs/0009-security-sandboxing.md) | Security & Sandboxing | Phases 3–4 (identity tokens, HITL gates) | 📋 Proposed |
| 0010 | Sub-Agent Spawning | Full RFC | Not yet written |
| [0012](docs/rfcs/0012-protocols-organizations.md) | Protocols & Organizations | Phases 1–3: org model, authority axis, clearance, cross-context influence | 📋 Proposed |
| [0014](docs/rfcs/0014-agent-skill-registry-lifecycle.md) | Agent Skill Registry & Lifecycle | Full RFC | 📋 Proposed |
| [0021](docs/rfcs/0021-persona-temporal-awareness.md) | Persona Temporal Awareness | Phases 2–4 (commitments, REMINDER event, duration calibration) | 📋 Proposed |
| [0027](docs/rfcs/0027-reflection-driven-consolidation.md) | Reflection-Driven Consolidation | Full RFC (supersedes draft RFC 0025) | 📋 Proposed |
| [0028](docs/rfcs/0028-agent-decision-policy-engine.md) | Agent Decision Policy Engine | Phases 1–3 — deliberative reasoning checkpoints (the "reasoning" rung above the v0.3.x brainstorm); Phase 4 collective decisions → v0.5.0+ | 📋 Proposed |
| [0037](docs/rfcs/0037-memory-confidentiality-channel-classification.md) | Memory Confidentiality & Channel Classification | The confidentiality half of the cross-channel-experience release: classification lattice + protection levels + the deterministic memory-injection egress gate, so a persona learns from a confidential channel without leaking it. **Pulled forward to v0.3.12** (with RFC 0049) per 2026-07-15, restoring its own "why this is a v0.3.x RFC" intent. | v0.3.12 | ✅ Implemented — [PR plan](docs/rfcs/0037-pr-plan.md) PRs 1–8 |
| [0038](docs/rfcs/0038-concurrent-context-awareness-relay.md) | Concurrent-Context Awareness & Cross-Channel Relay | §B single-channel-turn guard carved forward into the RFC 0037 v0.3.12 plan (per 0037 Decision #3, 2026-07-19); §C–§E (contexts/awareness/relay) stay v0.4.0 | ⚠️ Partially Implemented — §B ✅ v0.3.12; §C–§E 📋 v0.4.0 |
| [0039](docs/rfcs/0039-user-accounts-authentication.md) | User Accounts & Authentication | Phase 3 — account administration REST API + self-service password change + failed-login lockout (the Phases 1–2 foundation ✅ shipped in v0.3.12, [PR plan](docs/rfcs/0039-pr-plan.md)) | 📋 Proposed |

### Dependency Chain (v0.4.0)

```
v0.3.0 complete (RFC 0008 fully delivered)
    │
    ├── RFC 0009 Phases 3–4 (identity tokens, HITL)       [builds on P1–2 from v0.3.0]
    │       │
    │       ├── RFC 0014 Phases 1–2 (skill registry + validation)  [depends on RFC 0009 P1; runs alongside P3–4]
    │       │       ↓
    │       │   RFC 0014 Phase 3 (SkillGrant + lifecycle)           [prerequisite for RFC 0010]
    │       │       ↓
    │       │   RFC 0010 (Sub-Agent Spawning)                       [depends on RFC 0008, RFC 0009 all phases, RFC 0014]
    │       │       ↓
    │       │   RFC 0012 partial (org topologies + authority)       [depends on RFC 0010]
    │
    └── RFC 0007 (Conditional & Looped Control Flow)        [parallel workstream; depends on RFC 0008 from v0.3.0; pairs with RFC 0010/0014 use cases]
            ↓
v0.4.0 complete
```

> **Why RFC 0014 before RFC 0010**: The skill registry is the capability-management layer RFC 0010 depends on for routing. When spawning a sub-agent the orchestrator uses `SkillCatalogue` to select and narrow the child's capabilities via `SkillGrant` records. RFC 0014 Phase 3 must land before RFC 0010's dynamic skill injection semantics are implemented.

> **Why RFC 0009 Phases 3–4 before RFC 0010**: Sub-agent spawning creates recursive execution paths. The capability token model (RFC 0009 Phase 4) ensures spawned agents receive narrowed, orchestrator-issued tokens rather than inheriting parent capabilities — a hard prerequisite for safe sub-agent scoping.

> **Why RFC 0007 lands in v0.4.0 (retargeted from v0.3.0 on 2026-05-06)**: v0.3.0's user-facing promise is *agents talk, negotiate, and form opinions over time* — channel infrastructure, conversation routing, persona memory. Conditional/looped workflow control flow does not serve that promise; it is workflow-engine plumbing. v0.4.0 is where it earns rent: sub-agent spawning (RFC 0010) introduces parent → child orchestration patterns where iterative refinement (`repeat_until` until child output passes review), branching on child status (`condition` on child results), and parallel fan-out (`for_each` over a child population) are the load-bearing primitives. RFC 0008 is the only hard dep and ships fully in v0.3.0 — by v0.4.0-start the prerequisite is satisfied. Parallel to the RFC 0009 → 0014 → 0010 chain; no blocking edge into RFC 0010.

### Planned Components (v0.4.0)

| Component | Go Package | Python Module | Target RFC |
|-----------|-----------|---------------|------------|
| Condition Evaluation | `internal/scheduler/` | — | 0007 |
| Workflow Loops | `internal/scheduler/`, `internal/planner/` | — | 0007 |
| Security & Sandboxing (P3–4) | `internal/security/` | `agents/security.py` | 0009 |
| Skill Registry & Lifecycle | `internal/registry/` | `agents/skills/` | 0014 |
| Sub-agents | — | `agents/sub_agents/` | 0010 |
| MCP Tools | `internal/mcp/` | `agents/tools/mcp_bridge.py` | 0010 |
| Organizations (partial) | `internal/protocols/` | — | 0012 |
| Persona Temporal Awareness (P2–4) | — | `agents/memory/commitments.py`, `agents/memory/duration.py`, `agents/persona_runtime/__init__.py`, `agents/persona_types.py`, `agents/tools/builtin.py` | 0021 |

---

## v0.5.0 — Connected Agents

**What a user can do**: Bridge your agent society into Slack, Discord, or email — agents receive and send real messages.

### What ships in v0.5.0

- **External bridges** — Slack, Discord, Telegram, email connectors (RFC 0011, external part)
- **Full compliance and privacy layer** — data classification, consent tracking, PII detection, right to erasure, ethical guardrails (RFC 0013)
- **RFC 0012 remainder** — meeting and negotiation protocol completion, advanced organizational features
- **Process automation & pattern extraction** — detect repeated reasoning patterns from telemetry, promote them to tested, sandboxed deterministic skills via human review (RFC 0015)

### RFC Scope

| RFC | Title | Target scope | Status |
|-----|-------|--------------|--------|
| 0011 | Channels + Bridges | External bridges | Not yet written |
| [0012](docs/rfcs/0012-protocols-organizations.md) | Protocols & Organizations | Phases 4–5: meeting/negotiation protocols, inter-org federation | 📋 Proposed |
| [0013](docs/rfcs/0013-legal-ethical-compliance.md) | Legal, Ethical & Regulatory Compliance | Full RFC | 📋 Proposed |
| [0015](docs/rfcs/0015-process-automation-pattern-extraction.md) | Process Automation & Pattern Extraction | Full RFC | 📋 Proposed |

> **Why RFC 0013 lands here and not earlier**: Phases 1–2 of RFC 0013 (risk taxonomy, data classification, PII detection) have no RFC 0009 dependency and can develop in parallel with v0.4.0 work. Phases 3–5 (erasure, consent enforcement, audit extensions) depend on RFC 0009's `AuditLogger` and HITL gates. RFC 0013 must be substantially complete before external bridges ship — bridge inputs are the primary vector for external user data entering the system.

> **Why RFC 0015 lands here and not earlier**: RFC 0015 is the learned-skill extraction pipeline deferred by RFC 0014 Open Question 4. It depends on the RFC 0014 Skill Registry (v0.4.0), RFC 0009 sandbox Phases 3–4 (v0.4.0), and RFC 0013 Phase 1 PII detection (v0.5.0) — PII redaction is a hard blocker because candidate records persist representative inputs. v0.5.0 is also when external bridges produce the high-repetition traffic patterns that make automation economically worthwhile.

### Dependency Chain (v0.5.0)

```
v0.4.0 complete (RFC 0014 Skill Registry + RFC 0009 sandbox + RFC 0010 sub-agents)
    ↓
RFC 0013 Phases 1–2 (risk taxonomy, PII detection)   [parallel with v0.4.0]
    ↓
RFC 0013 Phases 3–5 (erasure, consent, audit)        [depends on RFC 0009 P3–4]
    │
RFC 0015 Phase 1 (detection + candidate store)       [depends on RFC 0013 P1, RFC 0014 P4]
    ↓
RFC 0015 Phase 2 (drafter + registration gate)       [depends on RFC 0014 P1]
    ↓
RFC 0015 Phase 3 (deterministic dispatch + sandbox)  [depends on RFC 0009 P3–4, RFC 0014 P2–3]
    │
RFC 0011 external bridges + RFC 0012 remainder       [parallel with RFC 0015 P2–3]
    ↓
RFC 0015 Phase 4 (lifecycle governance + audit)      [depends on RFC 0009 all phases]
    ↓
v0.5.0 complete
```

### Planned Components (v0.5.0)

| Component | Go Package | Python Module | Target RFC |
|-----------|-----------|---------------|------------|
| External Bridges | `internal/bridges/` | — | 0011 |
| Compliance & Privacy | `internal/security/` | `agents/compliance.py` | 0013 |
| Automation Pipeline | `internal/automation/` | `agents/automation/` | 0015 |
| Organizations (remainder) | `internal/protocols/` | — | 0012 |
| Pattern Detection & Candidates | `internal/automation/` | — | 0015 |
| Deterministic Skill Dispatch | — | `agents/automation/`, `agents/skills/executor.py` | 0015 |

---

## v0.6.0 — Distributed Mesh

**What a user can do**: Run agent societies across multiple nodes and networks.

**Design**: Architecture sketched in [persatrix-extension-spec.md](docs/persatrix-extension-spec.md). No RFCs written yet.

### Planned Components (v0.6.0)

| Component | Package | Description |
|-----------|---------|-------------|
| Mesh Networking | `internal/mesh/` | Multi-node peer discovery and communication |
| A2A Protocol | `internal/a2a/` | Agent-to-agent networking across nodes |
| Agent Migration | — | Move agents between nodes for load balancing |
| Data Residency | — | Per-node data controls |

---

## Merged PR History

Generated: **[docs/merged-prs.md](docs/merged-prs.md)** — one row per squash
merge on `main`, newest first, regenerated by the pre-commit hook and
`make merged-prs`, checked by CI. The hand-maintained table that lived here
until 2026-09-06 had stopped at [#708](https://github.com/mkhomutov/Persatrix/pull/708)
in June while ~150 PRs merged after it; derivable data typed by hand goes
stale, so it is derived now.

---

## How to Update This File

This file must be reviewed and updated **during every task**, not just at completion.

### On every task (before starting and after finishing)

1. Verify the **RFC Scope** tables match reality — correct status, correct merged count.
2. Verify the **Component Status** tables — any component you touched should reflect current state.
3. Update the **Last updated** date at the top.

### When a PR is merged

1. The **Merged PR History** ([docs/merged-prs.md](docs/merged-prs.md)) regenerates itself on the next commit (`make merged-prs` by hand); nothing to type.
2. Increment the merged count in the relevant **RFC Scope** table.
3. If all PRs for an RFC are now merged, change its status to `✅ Implemented` here **and** in the RFC file.
4. Move completed components from "TODO stub" / "🔲 pending" → "✅ Complete" in component tables.
5. Update the **RFC Master Index** table status.

### When starting RFC implementation

1. Change the RFC status to `🚧 Implementing` here **and** in the RFC file (`docs/rfcs/NNNN-*.md`).

### When creating a new RFC

1. Add a row to the **RFC Master Index** table with status `📋 Proposed`.
2. Add a row to the relevant version's **RFC Scope** table.

### When a version ships

1. Update the **Version Map** table status from `🚧 In Progress` → `✅ Complete`.
2. Update the header "Current phase" line.

### Status markers (from [RFC README](docs/rfcs/README.md))

| Status | Marker |
|--------|--------|
| Proposed | 📋 Proposed |
| Accepted | 👍 Accepted |
| Implementing | 🚧 Implementing |
| Implemented | ✅ Implemented |
| Partially Implemented | ⚠️ Partially Implemented |
| Rejected | ❌ Rejected |
| Deferred | 🔮 Deferred |
