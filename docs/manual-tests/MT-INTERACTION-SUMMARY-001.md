# Manual Test MT-INTERACTION-SUMMARY-001: A closed interaction hands back a readable summary

**Test ID**: `MT-INTERACTION-SUMMARY-001`
**Feature Area**: Interactions (RFC 0020 summary surface — v0.3.8 Workstream 1c)
**Version**: 1.0
**Created**: 2026-06-09
**Last Updated**: 2026-06-09
**Status**: Active

---

## Overview

**Purpose**: Verify the v0.3.8 *readable synthesized outcome* — the "a real
result" half of the [*Conversations that converge*](../v0.3.8-plan.md) headline.
When a multi-turn interaction **closes**, the persona persists an
[RFC 0020 §C/§D](../rfcs/0020-interaction-lifecycle.md#c-interaction-lifecycle-states)
one-per-interaction summary, and v0.3.8 **surfaces** that summary so a converged
brainstorm hands back something a human can read instead of merely stopping. The
visible contract has three legs:

1. **Every close trigger carries a summary.** An interaction that closes by going
   **idle**, by an [RFC 0030 Layer 4](../rfcs/0030-multi-agent-conversation-governance.md)
   **end-vote** (a `structural` close), or by hitting the [RFC 0030 Layer 1](../rfcs/0030-multi-agent-conversation-governance.md)
   **cost ceiling** each leaves a `closed`/`summarized` `episodes` row reachable
   by the read API (`GET /api/v1/agents/{id}/interactions/closed`).
2. **Both surfaces render it.** The web console conversation view shows an
   "interaction closed" affordance carrying the summary + the close trigger; the
   `persatrix agent interactions` CLI prints the same.
3. **Failure is rendered honestly (SS3).** When the on-close summariser fails,
   the persisted `"[interaction summary unavailable]"` sentinel is surfaced as an
   explicit "summary unavailable" state — never a blank and never a fabricated
   synthesis.

This MT **isolates the summary surface**. The combined convergence story
(no-pile-on → bounded cost → ends on votes → readable summary, all together) is
[`MT-CONVERSATION-CONVERGENCE-001`](../v0.3.8-plan.md#acceptance-for-v038),
owned by the release-prep plan.

**Scope**: a multi-turn interaction on a DM and on the default `planning` group
channel; the close on each of the three triggers; the read API
([`internal/server/interactions_handler.go`](../../internal/server/interactions_handler.go)
→ the agent's `GetClosedInteractions` gRPC); and the two surfaces — the web
console conversation view ([`InteractionSummary.svelte`](../../web/src/panels/InteractionSummary.svelte)
inside [`ConversationFeed.svelte`](../../web/src/panels/ConversationFeed.svelte))
and the CLI ([`persatrix agent interactions`](../../cli/src/commands/interactions.rs)).

**Out of Scope**:
- *How* summaries are generated — the RFC 0020 summariser is unchanged; this
  surface only reads the persisted row (decision SS1).
- The governance layers themselves (cost ceiling / reply budget / end-vote
  mechanics) — exercised by `MT-CHANNEL-GOV-*`; here they are only the *triggers*
  that close an interaction.
- The no-pile-on Tier B salience bid — [`MT-CHANNEL-RELEVANCE-002`](MT-CHANNEL-RELEVANCE-002.md).
- The 2000-char summary cap and the failure sentinel text — inherited from
  RFC 0020 as-is.

---

## Related Documentation

- [RFC 0020 — Interaction Lifecycle](../rfcs/0020-interaction-lifecycle.md) — §C
  lifecycle states, §D storage model (the `(closed_at, summary)` encoding the
  read path filters on), §Security (the cap + the failure sentinel). The summary
  this MT surfaces.
- [Interaction-summary surface PR plan](../rfcs/0020-interaction-summary-surface-pr-plan.md)
  — this MT is PR 4's acceptance test (PRs 1–3 landed the close-trigger wiring +
  read path, the web surface, and the CLI surface).
- [v0.3.8 plan](../v0.3.8-plan.md) — Workstream 1c; the readable-outcome half of
  the convergence headline.
- [docs/guides/channels.md §"The interaction-summary surface"](../guides/channels.md)
  — the operator walkthrough this MT scripts.
- [docs/guides/web-console.md §"The conversation panel"](../guides/web-console.md)
  — the `--enable-ui` console surface.

**Related Automated Tests** (the TDD legs landed with PRs 1–3, green in CI):
- `tests/unit/python/test_closed_interactions_read.py` — every close trigger
  leaves a reachable `closed`/`summarized` row; the read path surfaces the
  failure sentinel rather than blanking it; `turn_count=1` degenerate summary.
- `tests/unit/python/test_cost_close_dispatch.py` — a Layer 1 cost-ceiling
  termination routes through the summarising close path (`by_cost` counter).
- `tests/integration/test_interaction_close_on_cost.py`,
  `tests/integration/test_interaction_summary_read_triggers.py` — the
  idle / structural (end-vote) / cost triggers each reach the read API.
- `internal/server/interactions_handler_test.go`,
  `internal/executor/interactions_test.go` — the REST projection + the gRPC
  proxy.
- `web/src/panels/InteractionSummary.test.js`, `web/src/lib/interactions.test.js`,
  `web/src/lib/api.closed.test.js` — the affordance renders summary + trigger;
  sentinel → "unavailable"; open interaction → no affordance.
- `cli/src/commands/interactions_tests.rs` — the CLI prints summary + trigger;
  the sentinel line; a clear "no closed interaction" message.

---

## Preconditions

Same as [MT-CHANNEL-004 § Preconditions](MT-CHANNEL-004.md#preconditions)
(a valid `ANTHROPIC_API_KEY` in `.env` — the persona replies and the on-close
summary are real LLM calls), **plus**:

- ☐ The orchestrator is built with the embedded Svelte bundle and run with
  `--enable-ui` (see [web-console.md § Quick start](../guides/web-console.md#quick-start-local-binary)),
  console reachable at `http://localhost:8080/ui`.
- ☐ The `persatrix` CLI is built and points at the same orchestrator
  (`PERSATRIX_API_URL`, default `http://localhost:8080`).
- ☐ The default demo personas are up (`ember-owl`, `iron-fox`, `nova-sparrow`).
- ☐ A scratch channel `group:summary-mt` exists with a **short idle window** and
  **low end-vote quorum** so the close triggers fire quickly within a test
  session. Example `config/channels.yaml` overlay:

  ```yaml
  channels:
    - name: summary-mt
      end_vote_threshold: 2     # Layer 4: 2 distinct votes …
      end_vote_window: 3        # … within 3 consecutive turns closes it
      # Layer 1 cost-ceiling case below uses a deliberately tiny budget:
      # interaction_budget_tokens: 800
      members:
        - {id: ember-owl, respond: participant}
        - {id: iron-fox, respond: participant}
        - {id: nova-sparrow, respond: participant}
  ```

> A clean run starts from a fresh `epoch` (`--epoch mt-summary-$(date +%s)`) so a
> prior interaction's summary does not mask the one under test — see the
> [epochs guide](../guides/epochs.md).

---

## Test Procedure

### Part A — Idle close surfaces a summary (DM)

1. ☐ Open the web console, pick `iron-fox` in the persona picker, and hold a
   short multi-turn DM (≥ 3 turns) on a concrete topic ("help me pick a name for
   a CLI tool — here are three candidates …"), then **stop replying**.
2. ☐ Wait past the idle window for the interaction to close.
3. ☐ **Web**: confirm an **"Conversation went idle"** affordance appears in the
   conversation view, below the live turns, carrying a readable one-paragraph
   summary of the exchange.
4. ☐ **CLI**: run

   ```bash
   persatrix agent interactions iron-fox --limit 5
   ```

   Confirm the newest block shows the close trigger `went idle`, the
   `turn_count`, the participants, and the same summary text.
5. ☐ **JSON cross-check**:

   ```bash
   persatrix agent interactions iron-fox --limit 1 --json | jq '.interactions[0] | {close_reason, turn_count, summary}'
   ```

   Confirm `close_reason == "idle_gap"` and `summary` is non-empty and not the
   sentinel.

**Pass**: the idle-closed DM shows a readable summary in **both** the web console
and the CLI, with the `went idle` / `idle_gap` trigger.

### Part B — End-vote close surfaces a summary (group, structural)

1. ☐ In `group:summary-mt`, post an open-floor question
   ("what should we name the new budget-lease library?"). Let the personas hold a
   few turns until **K=2 distinct** personas emit an `END_INTERACTION_VOTE`
   within the W=3 window and the interaction closes.
2. ☐ **Web**: confirm the conversation view shows an **"Conversation ended"**
   affordance with the synthesised summary (the structural close — the episode
   row does not distinguish a vote-close from a plain structural close, so
   "ended" is the honest label).
3. ☐ **CLI**: `persatrix agent interactions ember-owl --scope group:summary-mt --limit 3`
   shows the same block with trigger `ended`.
4. ☐ Confirm the summary reflects the *converged* discussion (it synthesises the
   round, not a single turn).

**Pass**: the end-vote-closed interaction surfaces a readable summary labelled
`ended` / `structural` on both surfaces.

### Part C — Cost-ceiling close surfaces a summary (group, Layer 1)

1. ☐ Re-create `group:summary-mt` with a **deliberately tiny**
   `interaction_budget_tokens: 800` (uncomment the overlay line) and restart so
   the cost ceiling trips within a couple of turns.
2. ☐ Post an open-floor question and let the personas reply until the
   per-interaction lease budget is exhausted (`INTERACTION_BUDGET_EXHAUSTED`) and
   the interaction is closed by cost.
3. ☐ **CLI**:

   ```bash
   persatrix agent interactions nova-sparrow --scope group:summary-mt --limit 1 --json \
     | jq '.interactions[0] | {close_reason, summary}'
   ```

   Confirm `close_reason == "cost"` and a non-empty `summary` — the cost-bounded
   conversation **still** yields a readable result (decision SS2: a cost
   termination routes through the summarising close path, it does not merely stop
   fanout).
4. ☐ **Web**: confirm the affordance reads **"Conversation cost limit reached"**
   with the summary.

**Pass**: a cost-ceiling-terminated interaction surfaces a readable summary
labelled `cost limit reached` / `cost` on both surfaces.

### Part D — Failure sentinel is rendered honestly (SS3)

1. ☐ Force a summariser failure: point the `context_management.summarization`
   model alias at the **offline/mock** provider that returns no usable summary
   (or temporarily set an unresolvable alias), then drive a multi-turn
   interaction to close as in Part A.
2. ☐ Confirm the persisted row carries the
   `"[interaction summary unavailable]"` sentinel:

   ```bash
   persatrix agent interactions iron-fox --limit 1 --json | jq '.interactions[0].summary'
   ```

3. ☐ **CLI**: the human-readable block shows an explicit
   **"Summary unavailable for this interaction."** line — not a blank, not a
   fabricated synthesis.
4. ☐ **Web**: the affordance renders a **"summary unavailable"** state, honestly,
   with the close trigger still shown.

**Pass**: a forced summary failure is surfaced as an explicit "unavailable"
state on both surfaces; nothing is blanked or fabricated.

### Part E — Absent / open-interaction negative cases

1. ☐ `persatrix agent interactions <fresh-agent-with-no-closed-interactions>`
   prints a clear "no closed interaction" message and does **not** crash.
2. ☐ While an interaction is **open** (mid-conversation, before close), confirm
   the web conversation view shows the live turns **with no summary affordance** —
   the affordance appears only at close (no regression to the live feed).

**Pass**: an absent summary is a clear message, not a crash; an open interaction
shows no summary affordance.

---

## Acceptance Criteria

- ☐ **Part A** — idle close → readable summary on web + CLI (`idle_gap`).
- ☐ **Part B** — end-vote (structural) close → readable summary on web + CLI
  (`structural`/`ended`).
- ☐ **Part C** — cost-ceiling close → readable summary on web + CLI (`cost`);
  the cost-bounded brainstorm still yields a result (SS2).
- ☐ **Part D** — forced summary failure → honest "unavailable" on both surfaces,
  never blank/fabricated (SS3).
- ☐ **Part E** — no-closed-interaction message is clear; open interaction shows
  no affordance.
- ☐ The summariser was **not** modified to make any part pass (SS1 — surface,
  don't regenerate).

---

## Notes / Observations

_(Record run date, HEAD commit, model alias in use, and any deviations.)_

| Run | Date | HEAD | Result | Notes |
|-----|------|------|--------|-------|
|     |      |      |        |       |
