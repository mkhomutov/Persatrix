# RFC 0021 — Persona Temporal Awareness — PR Implementation Plan (Phase 2)

**RFC**: [0021-persona-temporal-awareness.md](0021-persona-temporal-awareness.md)
**Status**: ⬜ Not started
**Created**: 2026-10-21
**Branch prefix**: `feature/v040-rfc0021-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Spawned from**: row R2 of the [v0.4.0 plan](../v0.4.0-plan.md#progress); its row A2 is this plan's phase. Phase 1 is in the [v0.3.0 plan](0021-pr-plan.md).

> Sizes are calibrated (1.7× naive, the v0.1 factor). Every PR stays under 500
> changed lines and writes its failing test first.

---

## Overview

A persona that says "I'll send you the notes on Friday" today keeps nothing
but the sentence: no store holds the promise, and no later prompt shows it.
This plan gives each persona a commitments tier, RFC 0021 Phase 2, in seven
PRs: a `commitments` table in its memory store, the tools that make, list,
fulfil and cancel a commitment, a nudge that tells the persona to use them,
and an open-commitments block in its prompt.

The same table holds the directives RFC 0012 Phase 2 writes, as the v0.4.0
plan's store lock says ("RFC 0021's `commitments` table … carrying RFC 0012
§E's influence-record fields"). This plan lands every column a directive
needs and the store calls RFC 0012's PRs use; it writes no directive and
reads none into a prompt.

Phase 3, the reminder that fires when a commitment falls due, is unslotted
(ruling m), so in v0.4.0 a commitment is read when the prompt is built and
never fires on its own. The goal audit proposes Phase 3 back, and the v0.4.0
plan's Known Gaps carry it.

**The no-change contract.** With no organization and `auth.mode: disabled`,
what changes is what the v0.4.0 plan's acceptance names: the commitment
tools, their nudge and the prompt block. A persona not granted the
commitment permissions sees none of them, and one with no open commitment
renders no block. PR 5 re-records the goldens once, naming the request hashes
that moved; every other PR replays them unchanged.

### What is already in place (do not rebuild)

- RFC 0021 Phase 1 — `agents/clock.py` (`Clock`, `AgentClock`, `FrozenClock`,
  `agent_now`) and `agents/temporal/rendering.py` (`format_relative`, whose
  future tense renders "in 2 hours" and "tomorrow"); the persona's `_clock`
  and `_timezone`.
- The memory store's migrations — `agents/memory/_migration_registry.py`
  (`MIGRATIONS`, latest v19) and `_migration_handlers.py`, one
  `_migration_<topic>.py` per version; `_migration_facts.py` is the model for
  a new table. `tests/unit/python/test_episodic_memory_core.py` pins the
  latest version.
- The scope helpers every tier uses — `_session_filter.py` (with the `legacy`
  carve-out), `_principal_filter.py` and `_epoch_filter.py`, strict equality
  for principal and epoch; `resolve_write_principal`; the protection level
  stamped with `normalize_for_stamp(current_acting_classification())`.
- `agents/tools/memory_tools.py` — the note tools: closures over the
  persona's store, `@tool(…, permissions=["memory:write"])`, each checking
  `PermissionGate` itself and stamping session and protection level.
  `agents/persona.py` builds them; `tool_round.py` offers them on every call.
- `agents/persona_runtime/memory_context.py` — `_inject_memory_context`
  recalls each tier, filters it through `TurnInjectionGate`, and budgets it;
  `memory_assembly.py` renders the admitted sections, relationship first.
  Its docstring already reserves the open-commitments slot "until v0.4.0".
  `memory_budget.py` — `KNOWN_TIERS`; `facts_section.py` — the
  sub-budget precedent (`DEFAULT_FACTS_BUDGET_TOKENS`).
- `agents/memory/personal_tiers.py` — `build_personal_tiers()` and
  `PersonalTiers`; personas never build `MemoryStore`.
- `agents/security.py` `sanitize` and `agents/prompt_safety.py`
  `escape_prompt_delimiters`.
- `prompts/runtime/safety/memory-tool-usage.md` — the memory-tool nudge,
  added in `prompt_assembly.py` when memory tools are offered.

### What this plan provides to the RFC 0012 plan

The [RFC 0012 plan](0012-pr-plan.md#what-this-plan-needs-from-the-prerequisite-plans)
asks row A2 for the following; nothing is asked of A2 by the RFC 0028 plan.

| Request | Provided | Revision |
|---------|----------|----------|
| A kind that tells a directive from a commitment | PR 1: `kind` (`commitment`, `directive`) | — |
| `due_at` nullable for a directive | PR 1: required for a commitment, nullable for a directive, by a check constraint | — |
| §E's fields: `issuer_id`, `issuing_channel_id`, `channel_authority`, `issuer_authority`, `scope` | PR 1 | — |
| The scope columns a fact carries, read with the facts tier's filters | PR 1 (`session_id`, `principal_id`, `epoch_id`, `protection_level`); PR 2 reads them with the same helpers | — |
| A directive status outside the commitment `state` constraint | PR 1: `directive_status`, its seven values, with `state` null on a directive row and `directive_status` null on a commitment row | — |
| The structured form: polarity, action class, target | PR 1: `polarity`, `action_class`, `target` | — |
| The graph version | PR 1: `graph_version` | — |
| `conflicts_with`, a mark that the conflict check ran, a count of its failures | PR 1: `conflicts_with_json`, `conflict_checked_at`, `conflict_failures` | — |
| A store call that writes a directive outside the tools | PR 3: `CommitmentStore.write_directive`, with list and update calls | — |
| RFC 0021's tools, prompt block and any later janitor act only on commitment rows | PRs 2, 4 and 5 filter on `kind = 'commitment'`; PR 3's test pins it | — |

Two facts the RFC 0012 plan's PRs rely on, stated here so they cannot drift:
the epoch is stamped from the bound `epoch_scope`, as every tier's is, so
C2 PR 8 writes directives inside `record_write_scopes` as the facts pass
does; and the RFC's `MemoryFacade` methods (deliverable 5) are calls on the
new tier, since `MemoryFacade` is now `MemoryStore` and personas never build
one (H-1).

---

## Phase 0 — open questions and design points

RFC 0021's next steps ask that a v0.4.0 PR plan resolve open questions 3–7.
No 2026-10-18 default exists for them, so each answer is a new default,
confirmed or overturned in the review of the PR named.

| # | Question | Answer | Settled in |
|---|----------|--------|-----------|
| OQ 3 | Relative time strings in `set_reminder`? | No: ISO-8601 or epoch seconds, the RFC's lean. An ISO time with no offset is read in the persona's `timezone`. A `due_at` in the past is refused with a message saying so. The now-anchor already gives the model the date. | New default; PR 5 |
| OQ 4 | Surface missed commitments? | Nothing becomes `missed` in Phase 2, since the janitor is Phase 3's. An open commitment past its due time stays in the block, first, marked overdue (H-4). The question itself stays with Phase 3. | New default; PR 4 |
| OQ 5 | Record others' promises as expectations? | No: a commitment row is the persona's own promise, the RFC's lean. A directive, someone's instruction to the persona, is RFC 0012's kind, not an expectation. | New default; PR 2 |
| OQ 6, 7 | Duration categories and priors | Phase 4, unslotted (ruling m); not decided here. | — |
| OQ 10 | `target_party` across renames | Accepted as the RFC says: free text, no foreign key. | Resolved |
| OQ 2, 9, 11 | Reminder horizon, duplicate suppression, `get_current_time` | Phase 3's; not decided here. | — |

Points the RFC leaves open or that the code has moved since it was written,
each settled by a new default in the PR named:

- **H-1 The tier is a store of its own on the persona's tiers.** A new
  `agents/memory/commitments.py` holds `CommitmentStore`, opened in
  `build_personal_tiers()` beside the facts tier and reached through
  `PersonalTiers` and the persona's memory namespace. Deliverable 5's
  `list_open_commitments(within_seconds)` and `list_commitments_to(target)`
  are its calls. Task agents get no commitments in v0.4.0. PR 2.
- **H-2 One table, two kinds.** The RFC's columns, RFC 0012's, and the scope
  columns, in one migration (v20). Check constraints keep each kind's
  columns apart: a commitment has a `due_at` and a `state`; a directive has a
  `directive_status` and an issuer. Indexes serve the block's read (kind,
  state, due time) and RFC 0012's (kind, directive status). Timestamps are
  epoch seconds, as RFC 0021 §F's schema has them. PR 1.
- **H-3 A commitment is scoped like a fact.** Each row is stamped with the
  session, principal, epoch and protection level of the turn that made it,
  as a note is, and every read applies the facts tier's session, principal
  and epoch filters. So a promise made to one signed-in person never appears
  in another's prompt, and a fresh epoch starts with none. PR 2.
- **H-4 The block.** "Open commitments", nearest due first, so overdue rows
  lead; those due within seven days (`temporal.commitments_horizon_seconds`)
  and those already overdue; each line rendered with `format_relative` and
  the target party. A dedicated budget, `temporal.commitments_prompt_budget_tokens`
  (default 300), fills whole lines and drops the farthest first, never
  truncating a line. It sits after the relationship summary and before
  channel history, as RFC 0011 §E's order and §J say. Commitment rows only.
  PR 4.
- **H-5 The block passes the injection gate.** `commitments` becomes a tier
  of `TurnInjectionGate`, rank-checked by protection level, so a promise made
  in a `restricted` direct message never reaches an `internal` room's
  prompt. Like notes, a commitment is authored in a turn and carries no
  source channel, so it stays out of the audience check (`AUDIENCE_TIERS`).
  Every memory tier passes the gate (RFC 0037); a new one that bypassed it
  would be a new leak, not a deferral. It is the gate's fifth tier, so
  RFC 0012 PR 10's directives tier becomes its sixth, and PR 4's split of
  `memory_context.py` is done by the time RFC 0012 PR 10 opens; the RFC 0012
  plan's notes record both. PR 4.
- **H-6 Descriptions.** Capped at 1 000 characters (§Security). Passed
  through `security.sanitize` with `source=agent_output` before storage, as
  RFC 0021's next step 3 asks; a flag is logged, not refused, as for channel
  text. Rendered with `escape_prompt_delimiters`, so a description cannot
  close a delimiter around other text. A tool-made commitment is the
  persona's own words and is not wrapped as external (§Security). PRs 2
  and 4.
- **H-7 A cap of 500 open commitments per persona**, enforced when one is
  made, with an error that says so. §Security names it as the mitigation;
  the RFC lists it under Phase 3, but nothing else bounds the tier's growth
  in v0.4.0 ([ISSUE-0186](../issues/ISSUE-0186-persona-memory-has-no-capacity-bound.md)).
  PR 2.
- **H-8 Permissions are granted in config.** `PermissionGate` has no
  defaults: every grant is in `config/agents.yaml`. `commitments: {read,
  write}` joins the `permissions` schema, and the three shipped personas get
  both, beside `memory`, which is what §G's "granted by default to persona
  agents" means here. The tools are offered only to a persona with a grant,
  so a persona without one sees no new tool and no nudge. PR 5.
- **H-9 The tools.** `set_reminder`, `list_commitments`, `fulfill_commitment`
  and `cancel_commitment`, §G's signatures, built as the note tools are.
  `fulfill_commitment`'s note becomes a note naming the commitment.
  Each acts only on commitment rows the turn's scope can read, so another
  person's commitment, or a directive, is "not found". The time tools
  (`time_since`, `time_until`) are Phase 3's. PR 5.
- **H-10 The nudge** is §G's sentence, a snippet beside
  `memory-tool-usage.md`, added only when the commitment tools are offered.
  PR 5.
- **H-11 Directive rows change only through RFC 0012's calls.** PR 3's
  `write_directive`, `list_directives` and `update_directive` check the
  vocabulary of each column; which status may follow which is RFC 0012's
  rule, enforced in its PRs 9–10a. PR 3.

---

## Progress Overview

| PR | Row | Title | Branch | Status | GitHub PR | Merged |
|----|-----|-------|--------|--------|-----------|--------|
| 1 | A2 | The `commitments` table (memory-store migration v20) | `feature/v040-rfc0021-commitments-table` | ⬜ | — | — |
| 2 | A2 | `CommitmentStore`: make, list, fulfil, cancel | `feature/v040-rfc0021-commitment-store` | ⬜ | — | — |
| 3 | A2 | Directive rows: the store calls RFC 0012 uses | `feature/v040-rfc0021-directive-calls` | ⬜ | — | — |
| 4 | A2 | The open-commitments prompt block | `feature/v040-rfc0021-commitments-block` | ⬜ | — | — |
| 5 | A2 | The commitment tools, permissions and nudge | `feature/v040-rfc0021-commitment-tools` | ⬜ | — | — |
| 6 | A2 | Integration test, guide, glossary, Known Gap issue | `feature/v040-rfc0021-commitments-guide` | ⬜ | — | — |
| 7 | — | Review follow-ups and closeout | `feature/v040-rfc0021-closeout` | ⬜ | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ✂️ Cut

## Dependency graph

```
R2 (this plan accepted)
PR 1 (migration) ──► PR 2 ──► PR 3                 (store ready for RFC 0012 C2)
PR 2 ──► PR 4 (block, empty) ──► PR 5 (tools; goldens once) ──► PR 6 ──► PR 7
A2 done ──► RFC 0012 C2
```

The hard edges:

- **PR 1 alone, ahead of its readers** — the store lock's "each lands ahead
  of its first reader"; PR 2 is the first.
- **PR 4 before PR 5** — the block renders nothing while no commitment
  exists, so it moves no request hash; the tools then move them once, in
  PR 5, as the v0.4.0 plan's acceptance asks.
- **A2 before RFC 0012 C2** — the v0.4.0 plan's order: C2 PR 8 is the first
  writer of directive rows.

RFC 0021 Phase 1 is shipped; nothing else gates this plan. PR 5 shares
`schemas/agent.schema.json` and `config/agents.yaml` with RFC 0009 PR 4 and
RFC 0028 PR 4, each adding its own block, and whichever merges second
rebases.

---

## PR Sequence

Every PR: the failing test first and confirmed red; `make lint` and the
Python suites green; `make validate` after any config change; docs, glossary
and status moved in the same PR; review findings recorded under the PR,
numbered `F-n`.

### PR 1 — `feature/v040-rfc0021-commitments-table`

**Scope**: `_migration_commitments.py` (new) and its entries in
`_migration_registry.py` and `_migration_handlers.py` (489 — the entry is
one line); the version pin in `test_episodic_memory_core.py`. RFC 0021
moves to 🚧 Implementing in this PR if it has not already.
**Implementation**: H-2. The guards every handler has (table exists, column
exists), `CREATE … IF NOT EXISTS`, one commit.
**Tests**: the migration on a populated v19 store; each check constraint
refuses a row of the wrong shape (a commitment with no `due_at`, a directive
with a `state`, an unknown `directive_status`); the defaults of the scope
columns match the facts tier's.
**Migration**: **memory store**, v19 → v20; read by PR 2.
**Acceptance**: the table exists and nothing reads it.
**Estimate**: ~300 lines.

### PR 2 — `feature/v040-rfc0021-commitment-store`

**Scope**: `agents/memory/commitments.py` (new) — the `Commitment`
dataclass, `create`, `get`, `list_open_commitments`, `list_commitments_to`,
`list`, `fulfill`, `cancel`; its opening in `build_personal_tiers()`
(`personal_tiers.py`, 79) and its place on `PersonalTiers`.
**Implementation**: H-1, H-3, H-6 and H-7 and OQ 5. The lifecycle is §F's:
`open` to `fulfilled` or `cancelled`; a closed row is read-only; nothing
moves a row to `missed`. Every call is clocked through `Clock`, never
`time.time()` (§Test Strategy's static check).
**Tests**: §Test Strategy's state machine; a read under another principal
or epoch returns none; a session read keeps the `legacy` carve-out; the
501st open commitment is refused; a description over the cap is refused and
a flagged one stored with its flag logged; a directive row is invisible to
every call here.
**Migration**: none.
**Estimate**: ~450 lines.

### PR 3 — `feature/v040-rfc0021-directive-calls`

**Scope**: in `agents/memory/commitments.py`: a `Directive` dataclass,
`write_directive`, `list_directives` (by status, under the facts tier's
filters) and `update_directive` (status, issuer authority, conflict
fields, graph version), so RFC 0012's open question 2 can store the authority
it works out again beside the version it raises.
**Implementation**: H-11. No caller yet; RFC 0012 C2 PR 8 is the first.
**Tests**: a directive written and read back with every §E field; a read
under another principal or epoch returns none; an unknown status, action
class or polarity is refused; no commitment call sees a directive and no
directive call sees a commitment.
**Migration**: none.
**Estimate**: ~300 lines.

### PR 4 — `feature/v040-rfc0021-commitments-block`

**Scope**: a new `agents/persona_runtime/commitments_section.py` that
renders the block; `memory_context.py` (500) and `memory_assembly.py` (175)
place it — `memory_context.py` splits at a real seam first, in a pure-move
commit; `commitments` added to `KNOWN_TIERS` (`memory_budget.py`) and to
`TurnInjectionGate`'s tiers (`injection_gate.py`, 449); a `temporal:` block
in `config/optimization.yaml` and `schemas/optimization.schema.json`, read
by `agents/optimization.py` as `memory_budget` is.
**Implementation**: H-4, H-5 and OQ 4. Nothing writes a commitment yet, so
the block is empty and every golden replays unchanged.
**Tests**: §Test Strategy's frozen-clock render; nearest due first, overdue
first; the budget drops whole lines, farthest first; a `restricted`
commitment is withheld from an `internal` room's prompt and recorded in the
gate's decision record; an empty store adds nothing to the prompt; the
`temporal:` block validates and a bad value fails loudly.
**Migration**: none.
**Estimate**: ~450 lines, of which the move is ~60.

### PR 5 — `feature/v040-rfc0021-commitment-tools`

**Scope**: a new `agents/tools/commitment_tools.py` beside `memory_tools.py`;
`agents/persona.py` (335) builds them when the persona has a grant;
`commitments` in the `permissions` schema (`schemas/agent.schema.json`) and
the grants on the three shipped personas in `config/agents.yaml`; the nudge
snippet, indexed in `prompts/runtime/safety/README.md` and
`docs/prompt-organization.md`, added in `prompt_assembly.py` (500 — one line
beside the memory nudge, or split first if that crosses 500); the goldens
re-recorded with `make eval-record-offline`.
**Implementation**: H-8, H-9, H-10 and OQ 3.
**Tests**: each tool against the store; a persona without the grant is
offered no tool and no nudge, and its prompt is byte-identical; a past
`due_at`, an unparsable one and a naive ISO time in the persona's time zone;
another principal's commitment is "not found"; the PR body names every
request hash that moved, and the confidentiality and audience seed replays
still pass.
**Estimate**: ~450 lines.

### PR 6 — `feature/v040-rfc0021-commitments-guide`

**Scope**: Phase 2 deliverable 6, the integration test: a persona told
"remind me to call Bob tomorrow at 3pm" stores a commitment that appears in
its next prompt, and a second principal's prompt shows none of it; a
commitments section in `docs/guides/persona-agents.md`; the glossary's
*commitment* and *commitments tier*; the issue the v0.4.0 plan's Known Gaps
assign to A2 — a commitment never fires a reminder — filed and linked from
that row.
**Acceptance**: Phase 2 complete.
**Estimate**: ~300 lines.

### PR 7 — `feature/v040-rfc0021-closeout`

**Purpose**: the review findings deferred from PRs 1–6, paraphrased by
component; RFC and ROADMAP status to ⚠️ Partially Implemented (Phases 1–2
shipped; Phases 3–4 unslotted); this plan's statuses closed. RFC 0021, near
its 8 000-word cap, splits at a real seam first if the closeout would cross
it.

---

## File-size constraints

| File | Lines now | Plan |
|------|-----------|------|
| `agents/persona_runtime/memory_context.py` | 500 | PR 4 splits at a real seam first |
| `agents/persona_runtime/prompt_assembly.py` | 500 | PR 5 adds one line, or splits first; RFC 0012 PR 10 splits it too, and whichever merges second rebases |
| `agents/memory/_migration_handlers.py` | 489 | PR 1 adds one registry line; the handler is its own file |
| `agents/persona_runtime/facts_section.py` | 500 | Untouched: the block is its own module |
| `agents/persona_runtime/injection_gate.py` | 449 | PR 4 adds a tier name |

## Risks

| Risk | Mitigation |
|------|------------|
| A commitment leaks across people or rooms. | Scoped like a fact (H-3) and gated like every tier (H-5); PRs 2, 4 and 6 test both. |
| The persona acknowledges a promise and never calls the tool. | The nudge (H-10), the reason §G gives for it; PR 6's integration test. |
| A promise that falls due is never acted on. | Overdue rows lead the block (OQ 4); the Known Gap issue (PR 6) and the goal audit's proposal of Phase 3. |
| The goldens move more than once. | The block lands empty before the tools (PR 4 before PR 5). |
| RFC 0012 needs a column this table lacks. | Its request is answered column by column above; a later need is a new migration ahead of its reader. |

## Notes

> 2026-10-21 — Plan written for row R2. RFC 0021 moves to 🚧 Implementing
> with it.

## Related documentation

- [RFC 0021 Phase 1 plan](0021-pr-plan.md) — the clock and rendering this phase uses
- [RFC 0012 PR plan](0012-pr-plan.md) — the directives this table holds
- [v0.4.0 plan](../v0.4.0-plan.md) — row A2, the store lock, the acceptance
- [Release cycle §Phase 1](../methodology/release-cycle.md#phase-1--implementation-prs) · [review process](../methodology/review-process.md)
