# Manual Test MT-AUTONOMOUS-001: One-shot autonomous brainstorm — convene, converge, terminate, synthesize, with zero human turns

**Test ID**: `MT-AUTONOMOUS-001`
**Feature Area**: Channels (autonomous agent-only channels — RFC 0052 Phase 1, on top of RFC 0030 governance / RFC 0050 config / RFC 0023 leasing / RFC 0020 interaction summary)
**Version**: 1.0
**Created**: 2026-07-07
**Last Updated**: 2026-07-07
**Status**: Active — authored at RFC 0052 PR 5; **live execution is a v0.3.11 release-prep (Phase 3) deliverable** (run against a real provider per [v0.3.11-plan §Acceptance](../v0.3.11-plan.md#acceptance-for-v0311)).

---

## Overview

**Purpose**: Verify the v0.3.11 headline promise — **a channel runs a productive discussion with no human in the loop**. An operator arms a channel with a topic/agenda/goal, a convener, a chair, and the **mandatory cost cap**, presses **convene**, and walks away: the convener opens the discussion under a fresh interaction with **no human message**, the roster discusses it through the ordinary governed wake chain, the discussion **terminates deterministically** (at `autonomous.max_rounds` or the wallet soft budget — whichever fires first), and **both §D artifacts** are produced — the chair's goal-directed **closing synthesis** delivered to every member, and a **real RFC 0020 interaction summary for every participating persona** (never the `[interaction summary unavailable]` placeholder). Total interaction spend stays **≤ the mandatory cap**.

**Scope**: a 3-persona group channel (`nova-sparrow` the convener, `iron-fox` the chair, `ember-owl` — all `respond: always` so the opener has an open-floor audience), the `autonomous.*` block on the RFC 0050 config surface (CLI + web), the **convene** action from the CLI verb and (smoke) the web Channel-settings button, the channel timeline (web + REST), the per-agent closed-interaction summary surface (`persatrix agent interactions` / `GET /api/v1/agents/{id}/interactions/closed`), and the close telemetry (`interaction_closed{trigger=structural|cost}`, `synthesis_turn{outcome}`).

**Out of Scope** — explicitly deferred, **not asserted** here:

- **Anti-collapse cadence** (RFC 0052 Phase 2 / PR 6) — a collapse-prone roster working a multi-item agenda is [MT-AUTONOMOUS-002]'s leg; this MT uses an `always`-disposition roster that discusses without convener pressure.
- **Standing / scheduled convening + the aggregate bound** (Phase 3 / PR 7) — [MT-AUTONOMOUS-003].
- **The four-vendor cross-provider headline** (Phase 4 / PR 9, depends on RFC 0053) — `MT-AUTONOMOUS-MULTIPROVIDER-001`.
- **The offline `make demo-autonomous` face** (Phase 4a / PR 8).

---

## Related Documentation

- [RFC 0052 — Autonomous Agent-Only Channels](../rfcs/0052-autonomous-agent-channels.md) — [§B self-convening](../rfcs/0052-autonomous-agent-channels.md#b-self-convening--starting-without-a-human-turn), [§D termination and synthesis](../rfcs/0052-autonomous-agent-channels.md#d-termination-and-synthesis--always-produce-an-artifact).
- [RFC 0052 PR plan](../rfcs/0052-pr-plan.md) — the 9-PR breakdown; this MT is the PR 5 (Phase 1e) acceptance artifact.
- [v0.3.11-plan §Acceptance](../v0.3.11-plan.md#acceptance-for-v0311) — the release gate this MT anchors.
- [Channels guide §Autonomous channels](../guides/channels.md) — the operator-facing arming/convening how-to (incl. the full convene error table).
- [MT-CHANNEL-CONFIG-001](MT-CHANNEL-CONFIG-001.md) — the live-edit config-knob mechanics the `autonomous.*` legs reuse.

**Related Automated Tests** — the deterministic CI backbone of this MT (mock provider, real wallet):

- [`internal/channels/autonomous_acceptance_test.go`](../../internal/channels/autonomous_acceptance_test.go) — the full convene→converge→terminate→synthesize cycle against a real wallet; the **no-runaway leg** (an adversarial "everyone always wants to talk" roster stays bounded); the **close-by-budget leg** (the roster-scaled `1 + N` synthesis reserve funds the chair turn **and** every persona's metered summary where the fail-closed hard cap would have denied them).
- [`tests/unit/python/test_autonomous_phase1_acceptance.py`](../../tests/unit/python/test_autonomous_phase1_acceptance.py) — the per-persona close-artifact chain: truthful `cost`/`structural` close reason, the synthesis ingested as the final turn, the OQ #6 metered summary lease billing the shared governance id, a real summary (never the placeholder).

This live MT confirms the *operator-observable* behaviour on a real provider; the bounded-close invariants themselves are pinned in CI.

---

## Preconditions

1. The compose stack is up against a **real provider** (`make demo-anthropic` / `make demo-openai` / equivalent — *not* the mock provider; convergence and a readable synthesis need real replies). Per [persona-agents guide §Starting the stack](../guides/persona-agents.md).
2. A **clean store** (`make reset` or a fresh `PERSATRIX_EPOCH`) so prior participants and spend do not steer the run.
3. The three demo personas are up (`agent-ember-owl`, `agent-iron-fox`, `agent-nova-sparrow`) and are members of the target group channel with `respond: always` (an unspecified member defaults to `when_mentioned` and will **not** answer the open-floor opener — the convene then 409s with *no open-floor responder*).
4. `persatrix` CLI on `PATH` pointed at the running orchestrator; the web console served (`ENABLE_UI=1` / `--enable-ui`) for the Step 5 smoke; `config_edit_enabled` on (the bundled default) — convene shares that toggle.
5. Operator access to the orchestrator logs (`docker compose logs -f orchestrator`) for the close/synthesis records, and to the OTEL metrics if charting `interaction_closed` / `synthesis_turn`.

---

## Test Procedure

### Step 1: Arm the channel — and confirm the safety gates hold

Arm a group channel from the CLI (the RFC 0050 surface; the web `AutonomousSettings` section is equivalent):

```bash
persatrix channel config set group:planning \
  autonomous.enabled=true \
  autonomous.topic="Should we adopt a monorepo? Lay out the tradeoffs." \
  autonomous.agenda='Build tooling cost,Cross-team coupling,Migration effort' \
  autonomous.convener=nova-sparrow \
  autonomous.goal="A synthesized recommendation with the strongest argument on each side." \
  autonomous.max_rounds=8 \
  interaction_budget_tokens=200000 \
  escalation_chair_id=iron-fox
```

**Before** the successful set, confirm the two validate gates fire (the no-runaway safety contract):

- Arming **without** `interaction_budget_tokens` → rejected (400, cap-required — RFC 0052 Goal #4).
- Arming **without** `escalation_chair_id` → rejected (400, chair-required — the role that authors the mandatory synthesis).

**Pass**: both unsafe shapes are rejected; the full set above round-trips (`persatrix channel config get group:planning` shows the block; the web panel renders it).

### Step 2: Convene from the CLI — the discussion opens with zero human turns

```bash
persatrix channel convene group:planning --json
# → 202 {channel_id, convener: "nova-sparrow", status: "convening"}
```

Watch the timeline (web console, or `GET /api/v1/channels/group:planning/messages`):

- The **opening turn** appears from `nova-sparrow` (the convener), posing the topic — **no human message precedes it**.
- The roster replies over the following minutes through the ordinary governed floor rounds; personas quote and build on each other (the v0.3.8 realism arc, unattended).

**Pass**: every message in the transcript is authored by a roster persona; no operator/human sender appears at any point in the interaction.

### Step 3: The discussion terminates on its own and leaves both artifacts

Wait (do **not** intervene) until the bound fires — with the Step 1 config, `max_rounds=8` typically lands within ~5–15 minutes.

- **Termination**: the orchestrator log records `channels: interaction closed by RFC 0052 bounded close` with `trigger=structural` (or `cost` if the soft budget fired first); `interaction_closed{trigger}` increments; **no further discussion messages** follow the close.
- **Artifact #1 — the synthesis**: the **final message** of the discussion is `iron-fox`'s (the chair's) goal-directed synthesis against `autonomous.goal` — a readable recommendation, delivered to every member as the closing message.
- **Artifact #2 — the summaries**: each persona's closed-interaction surface carries a **real** summary of the discussion:

```bash
persatrix agent interactions nova-sparrow   # and ember-owl, iron-fox
# → the just-closed interaction, close_reason structural|cost,
#   summary = a real recap — NOT "[interaction summary unavailable]"
```

**Pass**: the interaction closes without human help; the chair synthesis is the final message; all three personas' summaries are real; the channel is idle (re-convenable) afterwards.

### Step 4: Spend stayed under the mandatory cap

Read the interaction's total spend (the wallet's per-interaction ledger — the orchestrator log's lease records, or the cost telemetry) and compare against `interaction_budget_tokens`:

- Total tokens attributed to the closed interaction — the discussion turns **plus** the chair synthesis turn **plus** the three metered close summaries — is **≤ 200 000**.
- No lease denial appears on the close path (no persona fell back to the placeholder for budget reasons — that would be the reserve failing).

**Pass**: spend ≤ cap with the close path fully funded. (The tight version of this invariant — the close firing at the soft threshold with the `1 + N` reserve honoured to the token — is CI-pinned in the close-by-budget leg; here the live run confirms the ledger arithmetic on real usage.)

### Step 5: Web-button smoke — re-convene from the console

In the web Channel-settings panel for `group:planning` (§Autonomous channel section): the **Convene** button is enabled (the channel is armed and idle again). Press it.

**Pass**: the button acks (toast / "convening…" indicator), a fresh opening turn from `nova-sparrow` appears in the timeline, and a second bounded discussion runs — proving the retired interaction left the channel re-convenable and the web trigger drives the same endpoint. (The second run may be left to terminate on its own; its completion is not separately asserted.)

---

## Expected Results Summary

| # | Check | Expected |
|---|-------|----------|
| 1 | Safety gates + arming | uncapped and chairless arming rejected at validate; the full block round-trips on CLI + web |
| 2 | CLI convene | 202; the convener authors the opening turn; **zero human turns** in the transcript |
| 3 | Terminate + both artifacts | bounded close fires (`structural`/`cost`); the chair synthesis is the final message; every persona's summary is real (no placeholder) |
| 4 | Spend ≤ cap | interaction total (discussion + chair turn + metered summaries) ≤ `interaction_budget_tokens`; no close-path lease denial |
| 5 | Web convene smoke | the Convene button re-convenes the idle channel; a fresh opener appears |

---

## Edge Cases & Error Scenarios

### Edge Case 1: convening an unarmed channel is refused

```bash
persatrix channel convene group:standup
# Expect: 409 — not autonomous.enabled; nothing dispatched.
```

### Edge Case 2: convening a channel with a live interaction is refused

Re-run `persatrix channel convene group:planning` while the Step 2 discussion is still running.
**Expect**: `409` (already has a live interaction) — the convener opens one discussion, never a second over a running one.

### Edge Case 3: an audience that cannot answer is refused

Set every member except the convener to `respond: when_mentioned` (or observer) and convene.
**Expect**: `409` — *no open-floor responder besides the convener*; the opener is never dispatched into a room nobody answers.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| — | — | — | ⬜ Pending | Live execution scheduled for v0.3.11 release-prep (master-plan Phase 3). Dry-run on the mock provider: the CI backbone suites (see §Related Automated Tests) are green at PR 5. |

---

## Notes

- **The convene ack is `202 Accepted`** — "the convener was woken", not "the discussion ran". The discussion's progress is observed on the timeline, not the convene response.
- **The opening turn is documented-uncapped** ([RFC 0052 §B](../rfcs/0052-autonomous-agent-channels.md#b-self-convening--starting-without-a-human-turn)): the wallet snapshots the cap at the interaction's first commit, so the opener's own lease predates it. Step 4's ledger comparison starts from the first *capped* lease; the Layer-0 depth cap bounds the opener.
- **If the run collapses to silence before the bound** (every persona passes on round one), that is the Phase-2 anti-collapse territory — [MT-AUTONOMOUS-002]'s roster is built to provoke it. For this MT, re-run with a sharper `topic`; the Phase-1 contract under test is *bounded termination with artifacts*, not liveness pressure.
- A chair that never replies to the synthesis directive (provider error, gate suppression) falls back to an **immediate artifact-bearing close after the synthesis timeout** — termination never waits on a model. The summaries still produce; only the goal-directed synthesis message is missing, which the run should note as a degraded pass.

[MT-AUTONOMOUS-002]: MT-AUTONOMOUS-002.md
[MT-AUTONOMOUS-003]: ../rfcs/0052-pr-plan.md#pr-7-featurev0311-rfc0052-standing--phase-3-standing--scheduled-convening--aggregate-bound
