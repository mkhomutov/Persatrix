# RFC 0012 — Protocols & Organizations — PR Implementation Plan (Phases 1–3)

**RFC**: [0012-protocols-organizations.md](0012-protocols-organizations.md)
**Status**: ⬜ Not started
**Created**: 2026-10-20
**Branch prefix**: `feature/v040-rfc0012-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Spawned from**: row R1 of the [v0.4.0 plan](../v0.4.0-plan.md#progress); its rows C1–C3 are this plan's three phases

> Sizes are calibrated (1.7× naive, the v0.1 factor). Every PR stays under 500
> changed lines and writes its failing test first.

---

## Overview

An operator declares an organization — its topology, its roles, who manages
whom — and a persona then weighs an instruction by where it came from: who
gave it, and in which channel. This plan delivers RFC 0012's three v0.4.0
phases in 13 PRs:

- **C1, Phase 1** — the organization model: the config file and its schema,
  the Go graph and the `directs` relation, the graph loaded into the society
  tables, and each persona told its organization and role.
- **C2, Phase 2** — the authority axis: a channel `authority` level beside
  RFC 0037's `classification`, the two-factor rule, and directives written to
  the commitments tier with their cross-context influence record.
- **C3, Phase 3** — the accept / adapt / refuse decision at RFC 0028's
  pre-act checkpoint, conflict ranking and tie escalation, and runtime changes
  to the organization graph as a required human-in-the-loop class.

Two items are out, by the RFC's
[2026-10-20 amendment](0012-protocols-organizations.md#amendment-2026-10-20--the-v040-plans-ruling-b-audit):
Phase 1's membership-time clearance check (§G) and Phase 3's enforced egress
gate (§H). §G's role `authority` ceiling stays.

**The no-organization contract.** A deployment with no organization behaves
as v0.3.16 did: no prompt changes, no new records, `make eval-replay` and the
offline demo unchanged. Every PR that touches a prompt or a turn proves this
with a byte-identical test.

### What is already in place (do not rebuild)

- `internal/protocols/protocols.go` — a 15-line stub whose package doc already
  defers to this RFC; its conversation-protocol TODOs belong to Phase 4.
- `config/organizations.yaml` — a commented v0.2 sketch of an incompatible
  shape; C1 PR 1 replaces it (§B).
- RFC 0037's channel classification — the pattern C2 copies: a lattice helper
  (`internal/channels/classification.go`), migration v10→v11 in its own file,
  `SetChannelClassification`, the proto field lifted in
  `internal/channels/grpc_dispatcher_proto.go`, and one Python reader
  (`agents/channel_event_classification.py`).
- The RFC 0026 extraction pass at interaction close —
  `agents/persona_runtime/summarize_close.py` builds the close prompt, with
  `fact_extractor.py`'s suffix, and parses the reply; `finalize_close.py`
  runs it. Each record has one speaker, and facts are stamped with
  `interaction.speaker_id`.

### What this plan needs from the prerequisite plans

These shapes are requested of the RFC 0029, 0021 and 0009 PR plans (row R2);
if R2 settles them differently, the PR named here adapts before it opens.

| Row | Needed | Read by |
|-----|--------|---------|
| A1 | Society tables for organizations, roles, members and `manages` edges (with a `scope` label), and an organization-graph version counter, behind the society-store interface | C1 PR 3 |
| A2 | On the `commitments` table: a kind that tells a directive from a commitment, `due_at` nullable for a directive, §E's fields (`issuer_id`, `issuing_channel_id`, `channel_authority`, `issuer_authority`, `scope`, and a directive status — pending, accepted, adapted, refused, escalated, superseded — outside the commitment `state` constraint), the structured form PR 8 extracts (polarity, action class, target), the graph version, and `conflicts_with`; a store call that writes a directive outside the tools. RFC 0021's tools (`list_commitments`, `fulfill_commitment`, `cancel_commitment`), its prompt block and any later janitor act only on `kind = commitment` rows; directive rows change only through PR 9 | C2 PR 8 |
| A4 | The organization read (C1 PR 4) among the token-checked agent routes; verified sender ids under `auth.mode: enabled`; the pending-approval queue and its release path, which C3 uses for ties and graph changes | C1 PR 4, C3 |
| B1, B3 | The pre-act checkpoint, a `DecisionRecord` that names its subject, and approval tokens | C3 |

---

## Phase 0 — open questions and design points

The RFC's open questions, settled from the 2026-10-18 defaults and the plan's
locks. An answer marked *Confirmed here* is settled by accepting this plan; a
*Default* is confirmed or overturned in the review of the PR named.

| # | Question | Answer | Settled in |
|---|----------|--------|-----------|
| OQ 1 | Role authority: declared or derived from depth? | Declared, as proposed: each role names its `authority` ceiling, and the schema requires it. | Confirmed here; C1 PR 1 |
| OQ 2 | A stored directive after the graph changes | Every load or runtime change bumps the graph version. A `pending` directive whose version is older has its issuer authority worked out again before the checkpoint uses it. A record's verdict and provenance never change; an accepted directive can later be superseded by a higher one (D-6), which writes a new record. | Default; C3 PR 9 |
| OQ 3 | Ties in a flat organization | No `tiebreak` role in v0.4.0. Every tie, in any topology, goes to a person (D-8). A `tiebreak` role can be added later without a schema break. | Default; C3 PR 10 |
| OQ 4 | A persona in two organizations | Not in v0.4.0: validation fails it. Membership stays a list per organization, so Phase 5 can lift the rule without a schema change. | Confirmed here; C1 PR 1 |
| OQ 5 | Must authority and classification move together? | No: they stay independent, and neither schema ties them. | Confirmed here; C2 PR 5 |
| OQ 6 | What writes a directive to the commitments tier? | **The plan-opening default is confirmed**: the RFC 0026 extraction pass, and only for a persona that belongs to an organization. Consequence: a directive binds from the close of the interaction it was given in. Until then the persona has it only as conversation text. | Confirmed here; C2 PR 8 |

Points the RFC leaves open or states two ways, each settled by default in the
PR named:

- **D-1 A peer's instruction** — §D calls an instruction with no `directs`
  relation an *observation*, while §C calls a flat-organization peer's
  instruction a *request*. Reading: a member of the same organization with no
  `directs` relation makes a *request* in a `peer` channel or higher; anyone
  else, or anything in an `observed` channel, is an *observation*. C2 PR 7.
- **D-2 Membership is declared once.** §B puts it in
  `config/organizations.yaml`; Phase 1 item 4's "surfaced on the persona
  definition" is read as the persona's runtime state, not a second key in
  `config/agents.yaml`. The free-text `relationships` entries stay affective
  (§B). C1 PR 4.
- **D-3 No `clearance` key.** With its check unslotted, a ceiling nothing
  enforces would read to an operator as a control. The v0.4.0 schema rejects
  the key; it returns with the check. C1 PR 1.
- **D-4 The file wins at startup.** Each start replaces the organization
  tables from the file. A runtime change (C3 PR 11) holds until the next
  restart unless the operator writes it into the file, and the guide says so.
  C1 PR 3.
- **D-5 One `directs`, kept fresh.** Go computes the relation on each read
  from the graph. The read gives a persona every member of its organization
  with their role and role authority ceiling, and, for each member who
  directs it, the scopes and its distance up the manages chain. Python looks
  them up and keeps no graph. The persona re-reads on every re-registration,
  retries a failed read with backoff, and, from C2 PR 6, re-reads when the
  graph version stamped on a channel event is newer than its copy. C1 PR 4.
- **D-6 Conflict is judged by the model and ranked by rule, at the
  checkpoint.** The close-time extraction never sees other directives, and
  sibling closes run at once, so it cannot judge conflict. When the
  checkpoint first meets a new directive, one short model call compares it
  with the persona's pending and accepted directives in the same scope and
  records the ones it contradicts (`conflicts_with`), once. §C precedence then
  picks the winner, whichever arrived first; an accepted loser moves to
  `superseded` with a new record. C3 PRs 9–10.
- **D-7 Direct-message authority.** A `dm_default_authority` setting, like
  `dm_default_classification`, defaults to `observed`. So a manager directs a
  report in a direct message only where the operator allows it. It applies
  to DMs created after it is set; an existing DM's level changes through
  PR 5's operator route. C2 PR 5.
- **D-8 Ties go to a person.** A tie, in any topology, opens one pending
  approval per tied directive on A4's queue, once: the tied rows move to
  `escalated` and are not escalated again unless a new conflicting directive
  or a graph change re-opens them. Approving one accepts it and refuses the
  others; denying all refuses all. The record names the recipient's manager,
  whom §F prefers, but v0.4.0 has no persona-to-persona direct-message route,
  and in a matrix the tied managers are themselves the parties, so the
  manager route is not built. C3 PR 10.

---

## Progress Overview

| PR | Row | Title | Branch | Status | GitHub PR | Merged |
|----|-----|-------|--------|--------|-----------|--------|
| 1 | C1 | Organization schema, validation, glossary | `feature/v040-rfc0012-org-schema` | ⬜ | — | — |
| 2 | C1 | `internal/protocols/`: graph, topology checks, `directs` | `feature/v040-rfc0012-protocols-graph` | ⬜ | — | — |
| 3 | C1 | Graph loaded into the society tables at startup | `feature/v040-rfc0012-org-load` | ⬜ | — | — |
| 4 | C1 | Organization read; a persona's organization and role; the guide | `feature/v040-rfc0012-persona-role` | ⬜ | — | — |
| 5 | C2 | Channel authority at rest (channels-store migration) | `feature/v040-rfc0012-authority-store` | ⬜ | — | — |
| 6 | C2 | Channel authority on the wire | `feature/v040-rfc0012-authority-wire` | ⬜ | — | — |
| 7 | C2 | The two-factor rule with the role ceiling | `feature/v040-rfc0012-two-factor-rule` | ⬜ | — | — |
| 8 | C2 | Directives written by the extraction pass | `feature/v040-rfc0012-directive-extraction` | ⬜ | — | — |
| 9 | C3 | Accept / adapt / refuse evaluation and ranking | `feature/v040-rfc0012-directive-evaluation` | ⬜ | — | — |
| 10 | C3 | Evaluation at the pre-act checkpoint; escalation | `feature/v040-rfc0012-directive-checkpoint` | ⬜ | — | — |
| 11 | C3 | Runtime graph change as a required-HITL class | `feature/v040-rfc0012-org-change-hitl` | ⬜ | — | — |
| 12 | C3 | `MT-ORG-AUTHORITY-001` and its driver | `feature/v040-rfc0012-mt-org-authority` | ⬜ | — | — |
| 13 | — | Review follow-ups and closeout | `feature/v040-rfc0012-closeout` | ⬜ | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ✂️ Cut

## Dependency graph

```
R1 (this plan accepted)
PR 1 ──► PR 2 ──► PR 3 ──► PR 4                          ═══ C1 done
PR 4 ──► PR 5 (migration) ──► PR 6 ──► PR 7 ──► PR 8      ═══ C2 done
PR 8 ──► PR 9 ──► PR 10 ──► PR 11 ──► PR 12              ═══ C3 done
PR 12 ──► PR 13 (closeout)
A1 society tables ──► PR 3
A2 commitments ──► PR 8
RFC 0028 B1 checkpoint, RFC 0009 A4 tokens and queue, RFC 0028 B3 approval tokens ──► PR 9
```

The hard edges, each from the v0.4.0 plan's
[order](../v0.4.0-plan.md#implementation-prs) or an RFC phase's own
dependency:

- **A1 before PR 3** — the organization tables are A1's migration; PR 3 is
  their first writer.
- **C1 → C2 → C3** — the phases build on each other (the RFC's dependency
  lines).
- **PR 5 before PR 6** — the `channels.authority` migration lands alone,
  ahead of its first reader.
- **A2 before PR 8** — the directive rows live in A2's `commitments` table.
- **B1, A4 and B3 before PR 9**, the plan's "A4 and B3 before C3" — the
  evaluation runs at RFC 0028's checkpoint; an authority decision means
  nothing while an issuer's agent id can be forged, which A4's tokens stop
  under `auth.mode: enabled`; and an escalated tie and a runtime graph change
  wait on A4's queue and B3's approval tokens.

---

## PR Sequence

Every PR: the failing test first and confirmed red; `make lint` and the
language's suites green; `make validate` after any config change; docs,
glossary and status moved in the same PR; review findings recorded under the
PR, numbered `F-n`.

### PR 1 — `feature/v040-rfc0012-org-schema`

**Scope**: new `schemas/organization.schema.json`; `config/organizations.yaml`
(the v0.2 sketch removed, an empty `organizations: []` with a commented
example of the §B shape); a `_SCHEMA_MAP` entry in `agents/validate.py`
(162 lines, whose TODO names this file); a small `agents/org_validate.py` for
the graph checks JSON Schema cannot express; shared fixtures under
`tests/fixtures/organizations/`; the six glossary entries the RFC's next steps
name: *organization*, *topology*, *authority axis*, *directive*,
*cross-context influence record*, *clearance*. RFC 0012 and its ROADMAP rows
move to 🚧 Implementing.
**Implementation**: the schema follows §B with OQ 1 (`authority` required on
every role), OQ 4 and D-3. `make validate` fails a cycle, a second manager in
a `hierarchy`, a `matrix` edge without `scope`, any `manages` in a `flat`
organization, an `agent_id` absent from `config/agents.yaml`, and a persona in
two organizations — the plan's acceptance: "a cycle or a second manager fails
validation".
**Tests**: each invalid fixture fails `make validate` with a message naming
the rule; the valid fixtures pass. The same fixtures feed PR 2's Go tests, so
the two checkers cannot drift.
**Migration**: none.
**Acceptance**: a hierarchy, a flat and a matrix organization validate; each
bad shape is refused with its reason.
**Estimate**: ~400 lines.

### PR 2 — `feature/v040-rfc0012-protocols-graph`

**Scope**: `internal/protocols/` — `organization.go` (types), `loader.go`
(parse and check, the same rules as PR 1), `directs.go`; the package doc
rewritten for the organization model, with the Phase 4 TODOs kept.
**Implementation**: `directs(issuer, recipient, scope)` per §C — an ancestor in
a hierarchy, always false in a flat organization, per dimension in a matrix —
computed, never stored. A precedence call ranks two issuers for one recipient
and returns a winner or a tie (§C). No store, no caller yet.
**Tests**: table-driven, per topology; the PR 1 fixtures through the Go loader
with the same verdicts; ties for two peers and for two matrix managers in one
scope.
**Migration**: none.
**Acceptance**: `go test ./internal/protocols` covers every §C rule.
**Estimate**: ~450 lines.

### PR 3 — `feature/v040-rfc0012-org-load`

**Scope**: a new `cmd/orchestrator/organizations.go` beside `channels.go`
(491 lines), with the call in `main.go` (499); writes through A1's
society-store interface; an `org.loaded` audit event in
`internal/security/audit_event.go`.
**Implementation**: a missing file means no organization, logged at Info, and
is treated as `organizations: []`: tables still holding a graph from an
earlier boot are cleared, the version goes up, and `org.loaded` records it.
A malformed file stops startup, as `LoadSecurityConfig` does, because
silently running with no organization would change what binds whom. D-4:
the file replaces the tables, and the graph version goes up.
**Tests**: load, restart and reload; a malformed file stops startup; with no
file and empty tables nothing is written; a missing file after a loaded boot
clears the organization.
**Migration**: none — A1 owns the tables.
**Acceptance**: the plan's "loads into the society tables".
**Estimate**: ~350 lines.

### PR 4 — `feature/v040-rfc0012-persona-role`

**Scope**: a new `internal/server/organization_handlers.go` with its routes in
its own register function, as `channel_routes.go` does (`server.go`, at 500
lines, gains one call); the route's entry in `auth_policy.go`; a new
`agents/org_context.py`; read-only `organization` and `org_role` on
`PersonaAgent` (`agents/persona.py`, 335); a new
`docs/guides/organizations.md`.
**Implementation**: one agent-facing read returns the calling persona's
organization and role, every member with their role and role authority
ceiling, for each member who directs the persona the scopes and its distance
up the manages chain, and the graph version (D-5). The persona re-reads on
re-registration and retries a failed read with backoff. If A4 has merged, the
route joins its token-checked routes. If not, the route is `authenticated`:
under `auth.mode: enabled` the read then fails, and the persona runs with no
organization until A4 lands — that adds no anonymous route.
**Tests**: a member reads its role and every member's ceiling; a non-member
reads none; a refused read leaves the persona with no organization, logs once
and is retried; a re-registration re-reads; non-member prompts are
byte-identical.
**Migration**: none.
**Acceptance**: Phase 1 complete; the guide covers declaring an organization.
**Estimate**: ~450 lines.

### PR 5 — `feature/v040-rfc0012-authority-store`

**Scope**: `internal/channels/authority.go` (levels and rank, with a separate
call for each unknown-value rule, as RFC 0037 did); the channels-store
migration in its own file (`sqlite_authority_migration.go`, the next version
after A1's); `SetChannelAuthority`; per-channel `authority` and
`dm_default_authority` in `internal/channels/config.go` (499 — split at a
real seam first), `config/channels.yaml` and `schemas/channel.schema.json`;
the `ReconcileConfig` arm that writes a declared level onto rows already in
the store, as classification's arm does (`router_reconcile.go`); an
operator-only, audited route that sets one channel's level, DMs included.
**Implementation**: `channels.authority TEXT NOT NULL DEFAULT 'observed'`, with
no CHECK constraint, as for `classification`. Unknown and empty values resolve
to `observed` (§A). D-7 at direct-message creation; a later change to
`dm_default_authority` reaches new DMs only. OQ 5: no rule ties the level to
`classification`.
**Tests**: the migration on a populated store at A1's version; schema refusal;
direct-message stamping; unknown resolves to `observed`; a declared level
reaches a row that existed before it was declared; the route changes an
existing DM.
**Migration**: **channels store**, one version; read by PR 6.
**Acceptance**: the level is stored and nothing reads it yet.
**Estimate**: ~450 lines.

### PR 6 — `feature/v040-rfc0012-authority-wire`

**Scope**: `proto/task.proto` — `channel_authority` and the organization-graph
version on `ChannelMessageEvent`, the next free fields, stubs regenerated
with the CI-pinned toolchain; the level resolved through the router's
classification cache (`router_classification.go`), extended to hold it, so
the existing delete hook (`ForgetChannelClassification`) drops both; the Go
lift in `grpc_dispatcher_proto.go` and the REST history and catch-up
responses; a new `agents/channel_event_authority.py`, the single Python
reader, seeded from both delivery paths; the Python rank twin.
**Tests**: dispatch and catch-up carry the level; a legacy event reads as
`observed`; a deleted and re-created channel keeps no cached level; a newer
stamped graph version makes the persona re-read (D-5); a property test that
the Go and Python ranks agree.
**Migration**: none.
**Acceptance**: the level reaches the persona, which ignores it.
**Estimate**: ~400 lines.

### PR 7 — `feature/v040-rfc0012-two-factor-rule`

**Scope**: a new `agents/org_authority.py`.
**Implementation**: one pure function from (the `directs` lookup, the
channel's level, the issuer's role ceiling and whether the issuer is in the
persona's organization, both from the PR 4 read) to *directive*, *request* or
*observation*: §D's two factors, D-1, and §G's effective level,
`min(role authority, channel authority)` while `directs` holds. Unknown inputs
fall to *observation*.
**Tests**: every combination in one table; the RFC's unit cases — a manager in
an `operational` channel binds; a stranger in a `directive` channel does not; a
`staff-engineer` role caps at `operational`.
**Migration**: none.
**Estimate**: ~250 lines.

### PR 8 — `feature/v040-rfc0012-directive-extraction`

**Scope**: a new prompt fragment
`prompts/runtime/safety/directive-extractor-suffix.md`, appended in
`agents/persona_runtime/summarize_close.py` (471), which builds the close
prompt and parses its reply, only for organization members — membership is
passed down from `close_path.py` through `finalize_close.py`; an optional
`directives` key in `agents/persona_runtime/fact_envelope.py`; a new
`agents/persona_runtime/directive_writer.py` called from `finalize_close.py`
(252).
**Implementation**: OQ 6. The issuer is the record's single speaker,
`interaction.speaker_id`, as the facts tier stamps it; the model never names
it (§E: "the message sender"). A speakerless record writes no directive, and
the writer reads only the record's own turns (`own_turn_items`). Each
directive found gets its §E record: the issuer, the interaction's channel,
that channel's level as last delivered, the PR 7 verdict from the PR 4
lookup, its scope, `pending` and the graph version. Where the instruction
allows it, the model also gives a structured form — a polarity (do or do
not), one RFC 0028 action class, and a target channel or participant — that
PR 10 enforces. **Security**: sender ids are the orchestrator's, verified
once A4 lands, under `auth.mode: enabled` only; under `disabled` a REST
publish or chat message names its own sender, so the authority axis is only
as strong as `enabled`, and the guide says so. The directive text keeps the
protection level RFC 0037 §C gives channel-derived memory.
**Tests**: a recorded extractor response writes the expected rows; a relayed
"per <manager>" line is recorded under the relaying sender; a speakerless
record writes none; a non-member's close is byte-identical, and every
recorded golden still replays; the prompt-reference check passes.
**Migration**: none — A2 owns the table.
**Acceptance**: Phase 2 complete.
**Estimate**: ~450 lines; if over, the envelope parse and writer split from the
prompt wiring.

### PR 9 — `feature/v040-rfc0012-directive-evaluation`

**Scope**: a new `agents/decision/directives.py`, a constraint input for
RFC 0028's pipeline (B1).
**Implementation**: §F as a pure step over the persona's pending directives
and its accepted ones in the same scopes. It applies OQ 2's version check and
D-6's conflict pairs, and resolves each pending one: *accept*, *adapt* (a
matrix scope narrower than claimed, or a partial conflict), or *refuse*
(`no_authority`, `outranked`, or `permission_denied`, the last from
`PermissionGate` in `agents/tools/permissions.py`). An accepted directive
outranked by a new higher one moves to `superseded`. A tie is a `defer` with
`reason=authority_tie` that moves the tied rows to `escalated` (D-8). Each
outcome is one `DecisionRecord` whose subject is the directive, and the
row's status follows it.
**Tests**: the RFC's unit list — the higher manager wins, also when the
lower directive was accepted first; equal issuers defer, never pick, and
escalate once across many checkpoints; a declined request is refused with
its reason; a record's verdict never changes.
**Migration**: none.
**Estimate**: ~400 lines.

### PR 10 — `feature/v040-rfc0012-directive-checkpoint`

**Scope**: the PR 9 input at the persona's pre-act checkpoint; D-6's one-time
conflict call for a new directive; each accepted directive's structured form
as a constraint input that removes a violating candidate before selection;
a budgeted "directives you have accepted" prompt block, for members only,
wrapped in RFC 0021's delimiters for text from other sources and filled
highest issuer first, then newest, with `prompt_assembly.py` (500) split at
a real seam first in a pure-move commit; ties to a person (OQ 3, D-8). A
directive with no structured form stays prompt guidance, and the guide says
so.
**Tests**: the RFC's integration list — a manager's directive in an
`operational` channel is accepted; the same from a peer or an `observed`
channel is not binding; of two conflicting directives the higher applies and
the other is refused with `reason=outranked`, in either arrival order; a
candidate that violates an accepted structured directive is never selected;
a tie opens one pending approval per tied directive, once, and approving one
resolves it. Security: a directive planted in a low-authority channel cannot
bind the persona elsewhere.
**Migration**: none.
**Estimate**: ~450 lines.

### PR 11 — `feature/v040-rfc0012-org-change-hitl`

**Scope**: Phase 3 item 3 — a REST route that proposes a graph change; the
change mapped onto the required-HITL registry's organizational class
(RFC 0028 PR 11); an `org.changed` audit event when a change applies.
**Implementation**: a proposed change becomes a pending approval, and
approving it applies the change in the same transaction: the approve route
is the spender, so no agent and no agent token are involved. The hold writes
RFC 0028's `decision.hitl_*` events and no others. With more than one account
(RFC 0039 Phase 3, row D3), the approver must be another person; with one
account, or under `auth.mode: disabled` with RFC 0028 PR 13's approval
secret, the operator may approve their own proposal, and the audit records
it. The graph version then goes up, so OQ 2 re-checks pending directives. In
v0.4.0 changes are proposed over REST only; no persona action proposes one.
**Tests**: a change is refused without approval, and under `disabled`
without the secret; with two accounts, its proposer cannot approve it; an
approved change applies once and is audited; a second approval of the same
change is refused.
**Migration**: none.
**Acceptance**: Phase 3 complete.
**Estimate**: ~400 lines.

### PR 12 — `feature/v040-rfc0012-mt-org-authority`

**Scope**: `docs/manual-tests/MT-ORG-AUTHORITY-001.md` and its index row;
`scripts/manual_tests/mt_org_authority_001.py`; a demo organization config.
The MT runs under `auth.mode: enabled`, which its forged-id leg needs.
**Implementation**: the arc of the RFC's Test Strategy: a manager and two
reports, a manager's and a peer's conflicting instructions given in earlier
interactions (OQ 6), and a manufactured tie. Plus the plan's two legs: an
action held until its token releases it, and a forged agent id refused. The
driver checks each `DecisionRecord`'s reason, not the reply text. The live run
belongs to the release gate.
**Tests**: the driver against recorded records — the pass rules fail on a
reply that complies without a record.
**Estimate**: ~400 lines.

### PR 13 — `feature/v040-rfc0012-closeout`

**Purpose**: the review findings deferred from PRs 1–12, paraphrased by
component; the guide finished, with a two-axis diagram; RFC and ROADMAP status
to ⚠️ Partially Implemented (Phases 1–3 shipped in v0.4.0, less the
amendment's two items; Phases 4–5 for v0.5.0); this plan's statuses closed.

---

## File-size constraints

| File | Lines now | Plan |
|------|-----------|------|
| `internal/server/server.go` | 500 | PR 4 and PR 11 register routes from their own files |
| `agents/persona_runtime/prompt_assembly.py` | 500 | PR 10 splits at a real seam first |
| `internal/channels/config.go` | 499 | PR 5 splits at a real seam first |
| `cmd/orchestrator/main.go` | 499 | PR 3 adds one call; the logic lives in its own file |
| `agents/persona_runtime/fact_extractor.py` | 483 | PR 8 adds one call |

## Risks

| Risk | Mitigation |
|------|------------|
| Directive extraction changes prompts for everyone and breaks the goldens. | The fragment is appended for members only; PR 8 replays every golden. |
| A directive is credited to the wrong issuer. | PR 8 takes the issuer from the record's speaker, never from the model; under `auth.mode: enabled`, A4 verifies sender ids. |
| Go and Python disagree about the graph. | One `directs`, in Go (D-5); shared fixtures for the two validators. |
| A directive binds only after its interaction closes. | Stated in OQ 6 and the guide; the MT gives its instructions in earlier interactions. |
| A1 or A2 settle a different table shape. | The table above is the request; the reading PR adapts before it opens. |

## Notes

> 2026-10-20 — Plan written for row R1, proposed with RFC 0012's move to
> Accepted. The first implementation PR moves it to 🚧 Implementing.

## Related documentation

- [RFC 0028 PR plan](0028-pr-plan.md) — the checkpoint, records and tokens C3 uses
- [v0.4.0 plan](../v0.4.0-plan.md) — rows C1–C3, the scope locks, the live arc
- [Release cycle §Phase 1](../methodology/release-cycle.md#phase-1--implementation-prs) · [review process](../methodology/review-process.md)
- [Development workflow §Phase 3](../development-workflow.md#phase-3--pr-plan-creation)
