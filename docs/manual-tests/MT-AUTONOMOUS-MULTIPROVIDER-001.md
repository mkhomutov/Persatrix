# Manual Test MT-AUTONOMOUS-MULTIPROVIDER-001: Four-vendor human-free brainstorm — Anthropic + OpenAI + Gemini + watsonx.ai, no human, converge + synthesize

**Test ID**: `MT-AUTONOMOUS-MULTIPROVIDER-001`
**Feature Area**: Channels (autonomous agent-only channels — RFC 0052 Phase 4, the flagship cross-vendor demo) × Providers (RFC 0053 — Gemini + watsonx.ai)
**Version**: 1.0
**Created**: 2026-07-13
**Last Updated**: 2026-07-13
**Status**: Active — the v0.3.11 **headline**; **live execution is a release-prep (master-plan Phase 3) deliverable** (needs all four vendors keyed). Gated on RFC 0053 — **landed** (Gemini [#731](https://github.com/mkhomutov/Persatrix/pull/731), watsonx [#732](https://github.com/mkhomutov/Persatrix/pull/732)), so this MT is in scope (had RFC 0053 slipped, it would have tracked into v0.3.12 per the cuttable [PR 9](../rfcs/0052-pr-plan.md#pr-9-featurev0311-rfc0052-demo-multivendor--phase-4b-four-vendor-headline--closeout-cuttable)).

---

## Overview

**Purpose**: Verify the v0.3.11 **flagship** promise — **four personas, each running on a *different* cloud vendor (Anthropic + OpenAI + Gemini + watsonx.ai), hold a productive discussion in one channel with no human in the loop**, then converge and synthesize. It is the most vivid possible proof that the conversation layer is provider-agnostic (RFC 0033 §H): the governed wake chain, the bounded close, the roster-scaled synthesis reserve, and the per-persona summaries all behave exactly as [MT-AUTONOMOUS-001] proved on a single provider — except each turn is authored by a different vendor's model, and nothing in the channel/governance/wallet layer knows or cares. This is `MT-AUTONOMOUS-001` (the autonomous arc) **crossed with** `MT-PROVIDER-GEMINI-001` / `MT-PROVIDER-WATSONX-001` (the per-vendor routing) in one run.

**Scope**: the four-vendor roster from [`blueprints/autonomous-multivendor/blueprint.yaml`](../../blueprints/autonomous-multivendor/blueprint.yaml) — four seats pinned by RFC 0033 alias to four distinct cloud vendors (`nova-sparrow`→Anthropic convener, `ember-owl`→OpenAI chair, `iron-fox`→Gemini, `slate-heron`→watsonx.ai), one autonomous group channel, the mandatory **single shared** per-interaction cost cap (not a per-seat cap), and the RFC 0053 native provider paths (`gemini` / `watsonx` in [`agents/llm_factory.py`](../../agents/llm_factory.py) `create_provider()`, alongside `anthropic` / `openai`). The per-seat vendor routing is confirmed on the OTEL telemetry (`gen_ai.system` per seat).

**Out of Scope** — explicitly deferred, **not asserted** here:

- **The single-provider autonomous contract** (bounded close, both artifacts, spend ≤ cap on one vendor) — that is [MT-AUTONOMOUS-001]'s job; this MT assumes it and adds the *cross-vendor* dimension.
- **Per-vendor provider correctness** (tool loop, finish-reason mapping, cost keying) — [MT-PROVIDER-GEMINI-001] / [MT-PROVIDER-WATSONX-001] own those single-provider smokes; here the vendors only need to *each author turns in one shared discussion*.
- **The offline `make demo-autonomous` face** (Phase 4a / PR 8, every seat on `mock`, zero keys) — [MT-AUTONOMOUS-001]'s CI backbone + the offline smoke cover it.
- **Anti-collapse cadence / standing convening** — [MT-AUTONOMOUS-002] / [MT-AUTONOMOUS-003]; this is a one-shot brainstorm.

---

## Related Documentation

- [RFC 0052 — Autonomous Agent-Only Channels](../rfcs/0052-autonomous-agent-channels.md) — [§Phase 4 flagship demo](../rfcs/0052-autonomous-agent-channels.md#phase-4-flagship-demo) (the two faces), [§Test Strategy](../rfcs/0052-autonomous-agent-channels.md#test-strategy) (this MT).
- [RFC 0053 — Gemini and watsonx.ai LLM Providers](../rfcs/0053-gemini-watsonx-providers.md) — the two providers this demo needs; [§Phase 3 four-vendor enablement](../rfcs/0053-gemini-watsonx-providers.md#phase-3-four-vendor-enablement-handoff-to-rfc-0052) (the handoff to here).
- [`blueprints/autonomous-multivendor/blueprint.yaml`](../../blueprints/autonomous-multivendor/blueprint.yaml) — the four-vendor roster + the inline per-seat alias→vendor mapping this MT wires up. Its CI validation is [`tests/unit/python/test_autonomous_multivendor_blueprint.py`](../../tests/unit/python/test_autonomous_multivendor_blueprint.py) (four distinct cloud vendors, priced fail-closed, agent-only, capped).
- [MT-AUTONOMOUS-001] — the single-provider autonomous contract this MT crosses with cross-vendor routing (arm/convene/converge/synthesize/spend mechanics are identical; reuse its Step-by-step).
- [MT-PROVIDER-GEMINI-001] / [MT-PROVIDER-WATSONX-001] — the per-vendor live smokes; their `gen_ai.system` telemetry check is reused here per seat.
- [Model-providers guide](../guides/model-providers.md) — the combined `providers` extra (`pip install 'persatrix-agents[providers]'`) + the four-cloud-vendor install path.
- [Channels guide §Autonomous channels](../guides/channels.md) — the operator arming/convening how-to.

**Related Automated Tests**:

- [`tests/unit/python/test_autonomous_multivendor_blueprint.py`](../../tests/unit/python/test_autonomous_multivendor_blueprint.py) — pins that the blueprint's four seats resolve (through the **real** RFC 0033 resolver) to four distinct cloud vendors, that every seat alias is priced (fail-closed — the demo cannot silently zero a vendor's RFC 0023 budget gate), and that the roster is a well-formed agent-only capped autonomous channel (convener ≠ chair, anchors resolve, no human member).
- The single-provider autonomous invariants (bounded close, `1 + N` reserve honoured, both artifacts) are CI-pinned in [`internal/channels/autonomous_acceptance_test.go`](../../internal/channels/autonomous_acceptance_test.go) + [`tests/unit/python/test_autonomous_phase1_acceptance.py`](../../tests/unit/python/test_autonomous_phase1_acceptance.py) (mock provider); this live MT adds only the cross-vendor routing dimension on top.

---

## Preconditions

1. **RFC 0053 landed** — Gemini ([#731](https://github.com/mkhomutov/Persatrix/pull/731)) + watsonx ([#732](https://github.com/mkhomutov/Persatrix/pull/732)) merged; the agent image carries both optional SDKs (`AGENT_EXTRAS: providers`, i.e. `pip install 'persatrix-agents[providers]'` → `google-genai` + `ibm-watsonx-ai`).
2. **All four vendors keyed** in your environment or `.env`:
   - `ANTHROPIC_API_KEY`
   - `OPENAI_API_KEY`
   - `GEMINI_API_KEY` (or `GOOGLE_API_KEY`)
   - `WATSONX_API_KEY` **and** the non-secret `WATSONX_PROJECT_ID` (or `WATSONX_SPACE_ID`); `WATSONX_URL` optional (defaults to us-south). Set a hard spend cap in each vendor's console first — this is a **live, billed** run on four clouds.
3. **The four-vendor alias config assembled.** Lift the `model_aliases` block from [`blueprints/autonomous-multivendor/blueprint.yaml`](../../blueprints/autonomous-multivendor/blueprint.yaml) into a mounted `optimization.yaml` (the four `config/demo/<vendor>/optimization.yaml` priced rows, combined into one alias map — RFC 0053's "references all four cloud alias configs at once"), and set `context_management.summarization.model: summarizer`. Regenerate `cost.pricing.models` from the alias map (`derived_cost_pricing()` — RFC 0033 §F) so the Go cost table carries all five physical models.
4. **The four demo personas pinned per-seat.** In `config/agents.yaml`, set each of the four personas' `model:` to its seat alias per the blueprint (`nova-sparrow`→`anthropic_seat`, `ember-owl`→`openai_seat`, `iron-fox`→`gemini_seat`, `slate-heron`→`watsonx_seat`). (`slate-heron` is the blueprint's fourth seat — add it as a persona if your roster ships only the three `autonomous-roundtable` demo personas.)
5. A **clean store** (`make reset` or a fresh `PERSATRIX_EPOCH`) so prior participants and spend do not steer the run; `persatrix` CLI on `PATH`; the web console served (`--enable-ui`) for the timeline; `config_edit_enabled` on (the bundled default).
6. Operator access to the orchestrator logs and the OTEL traces/metrics (to read the per-seat `gen_ai.system` and the interaction ledger).

---

## Test Procedure

The autonomous arc (arm → convene → converge → terminate → synthesize) is **identical** to [MT-AUTONOMOUS-001] — reuse its Steps 1–4 verbatim against the `multivendor-roundtable` channel. This MT adds one dimension: **each seat's turns route to a different vendor**, and the shared cap still holds.

### Step 1: Arm the four-vendor channel — the safety gates hold

Arm the `multivendor-roundtable` group channel from the CLI (or the web `AutonomousSettings` panel), with the blueprint's topic/agenda/goal, `convener=nova-sparrow`, `escalation_chair_id=ember-owl`, and the mandatory `interaction_budget_tokens=200000`:

```bash
persatrix channel config set group:multivendor-roundtable \
  autonomous.enabled=true \
  autonomous.topic="Should an early-stage startup commit to a single cloud provider or design for multi-cloud from day one?" \
  autonomous.agenda='Cost and vendor lock-in,Operational complexity and team expertise,Reliability and failover' \
  autonomous.convener=nova-sparrow \
  autonomous.goal="A synthesized recommendation with the strongest argument on each side." \
  autonomous.max_rounds=12 \
  interaction_budget_tokens=200000 \
  escalation_chair_id=ember-owl
```

**Before** the successful set, confirm the two validate gates still fire (identical to [MT-AUTONOMOUS-001] Step 1): arming without `interaction_budget_tokens` → 400 (cap-required); arming without `escalation_chair_id` → 400 (chair-required).

**Pass**: both unsafe shapes are rejected; the full set round-trips.

### Step 2: Convene — the discussion opens with zero human turns

```bash
persatrix channel convene group:multivendor-roundtable --json
# → 202 {channel_id, convener: "nova-sparrow", status: "convening"}
```

**Pass**: the opening turn appears from `nova-sparrow` posing the topic, with **no human message preceding it**; the roster replies over the following minutes through the ordinary governed floor rounds.

### Step 3: Each seat's turns route to its own vendor

While the discussion runs (and after it closes), read the OTEL traces / the orchestrator LLM-call logs and confirm the **per-seat vendor routing** — the cross-vendor assertion this MT exists for:

| Seat | Persona | `gen_ai.system` | Physical model |
|------|---------|-----------------|----------------|
| convener | `nova-sparrow` | `anthropic` | `claude-sonnet-4-6` |
| chair | `ember-owl` | `openai` | `gpt-4o` |
| participant | `iron-fox` | `gemini` | `gemini-3.5-flash` |
| participant | `slate-heron` | `watsonx` | `meta-llama/llama-3-3-70b-instruct` |

**Pass**: every seat authored at least one turn, and each seat's LLM calls file under **its** `gen_ai.system` (four distinct vendors observed in one interaction) — no seat silently fell back to another provider, and no seat resolved to `mock`/`ollama`.

### Step 4: Terminate + both artifacts (as MT-AUTONOMOUS-001 Step 3)

Wait (do **not** intervene) until the bound fires (`max_rounds=12` or the wallet soft budget). The orchestrator logs `interaction closed by RFC 0052 bounded close` (`trigger=structural|cost`); the **chair's** (`ember-owl`, OpenAI) goal-directed synthesis is the **final message**; every persona's closed-interaction surface carries a **real** RFC 0020 summary (never `[interaction summary unavailable]`) — including the watsonx and Gemini seats:

```bash
persatrix agent interactions nova-sparrow   # and ember-owl, iron-fox, slate-heron
```

**Pass**: the interaction closes without human help; the chair synthesis is the final message; **all four** personas' summaries are real; the channel is idle (re-convenable).

### Step 5: Spend stayed under the single shared cap

Read the interaction's total spend from the wallet's per-interaction ledger. The cap is a **single shared per-interaction ceiling**, so it bounds the **whole** four-vendor discussion — every seat's turns (each priced at its own vendor's rate) **plus** the chair synthesis turn **plus** the four metered close summaries — **not** four separate per-seat budgets.

**Pass**: total interaction spend ≤ `interaction_budget_tokens` (200 000); no close-path lease denial (the `1 + N` reserve funded the chair turn + all four summaries). Per-token cost re-keys correctly to each seat's vendor rate (the cost table is derived from the alias map).

---

## Expected Results Summary

| # | Check | Expected |
|---|-------|----------|
| 1 | Safety gates + arming | uncapped and chairless arming rejected; the four-vendor block round-trips |
| 2 | Convene | 202; convener opens; **zero human turns** in the transcript |
| 3 | Per-seat vendor routing | four distinct `gen_ai.system` values (`anthropic`/`openai`/`gemini`/`watsonx`) observed in one interaction; every seat authored ≥1 turn |
| 4 | Terminate + both artifacts | bounded close fires; chair synthesis is the final message; **all four** personas' summaries are real |
| 5 | Spend ≤ shared cap | interaction total (all seats + chair turn + four metered summaries) ≤ `interaction_budget_tokens`; no close-path lease denial |

---

## Edge Cases & Error Scenarios

### Edge Case 1: one vendor key missing → that seat fails at its first request

Unset (say) `WATSONX_API_KEY` and convene. The `watsonx` seat (`slate-heron`) warns at startup and **fails on its first LLM call** (the S-09 lazy-failure posture — the client is built lazily), while the other three seats discuss normally. Missing `WATSONX_PROJECT_ID`/`SPACE_ID` is different: it **fails closed loudly at startup** (a `SystemExit`, RFC 0053 §C — required config the client cannot be built without), so that seat's agent never registers.

**Expect**: a missing *key* degrades to a three-vendor discussion (the run still converges + synthesizes — the bounded close never waits on a model); a missing watsonx *project_id* is a loud startup failure caught by the `make demo-watsonx` preflight. Note the degraded shape rather than treating it as a four-vendor pass.

### Edge Case 2: an unpriced seat alias would silently disable that vendor's budget gate

If a seat alias is edited to drop its pricing, `resolve()` fails closed (`SystemExit`, RFC 0033 §F) before the agent can route — the CI test `test_every_seat_alias_is_priced_fail_closed` guards the blueprint against exactly this, and the runtime guard backstops a hand-edited config.

**Expect**: an unpriced cloud seat never reaches a live, un-metered request.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| — | — | — | ⬜ Pending | Live execution scheduled for v0.3.11 release-prep (master-plan Phase 3, all four vendors keyed). The blueprint's four-distinct-vendor + priced-fail-closed + agent-only invariants are CI-pinned now (`test_autonomous_multivendor_blueprint.py`); the single-provider autonomous contract is CI-pinned on the mock provider (see §Related Automated Tests). |

---

## Notes

- **The cap is a single shared per-interaction ceiling, not a per-seat cap** — the whole four-vendor discussion settles under one `interaction_budget_tokens`. A vendor whose per-token price drifts is still bounded by the cap (RFC 0053 §D: the priced lease meters spend and can drift; the cap bounds spend regardless — two complementary guarantees, not the same one twice).
- **The summarizer alias is shared, not per-seat.** The RFC 0020 per-persona close summaries all resolve the one `summarizer` alias (pinned to a cheap Anthropic model in the blueprint), so the four summaries route to one vendor even though the discussion turns spanned four. Any keyed cloud vendor works for it.
- **This is the cross-vendor face of the same demo.** The offline face (`make demo-autonomous`, every seat on `mock`, zero keys) is [MT-AUTONOMOUS-001]'s CI backbone + the offline smoke; the two faces share the arc and differ only in the provider mapping (RFC 0052 §Phase 4).
- **`persatrix init --blueprint` is not yet wired** (`cli/src/main.rs` `Init` is a stub), so Preconditions 3–4 are a manual assembly from the blueprint. A turnkey `make demo-autonomous-multivendor` target is a natural release-prep follow-up (it would need to plumb four sets of credentials incl. the watsonx `project_id` preflight); until then this MT documents the wiring.

[MT-AUTONOMOUS-001]: MT-AUTONOMOUS-001.md
[MT-AUTONOMOUS-002]: MT-AUTONOMOUS-002.md
[MT-AUTONOMOUS-003]: MT-AUTONOMOUS-003.md
[MT-PROVIDER-GEMINI-001]: MT-PROVIDER-GEMINI-001.md
[MT-PROVIDER-WATSONX-001]: MT-PROVIDER-WATSONX-001.md
