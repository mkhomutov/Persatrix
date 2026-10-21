# RFC 0028 — Agent Decision Policy Engine — PR Implementation Plan (Phases 1, 2, 2a and 3)

**RFC**: [0028-agent-decision-policy-engine.md](0028-agent-decision-policy-engine.md)
**Status**: ⬜ Not started
**Created**: 2026-10-20
**Branch prefix**: `feature/v040-rfc0028-`
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Spawned from**: row R1 of the [v0.4.0 plan](../v0.4.0-plan.md#progress); its rows B1–B3 and the cuttable D1 are this plan's four phases

> Sizes are calibrated (1.7× naive, the v0.1 factor). Every PR stays under 500
> changed lines and writes its failing test first.

---

## Overview

Today a persona's choice — reply, call a tool, stay silent — is made inside
prompt text and scattered branches, and nothing keeps a record of why. This
plan gives task agents and persona agents one decision step that writes down
each choice and its reason, and that can hold an action until a person
approves it. It delivers RFC 0028 in 18 PRs:

- **B1, Phase 1** — the decision types, the selection pipeline, a
  deterministic baseline policy, a Python secret redactor, the decision log,
  the configuration, and the pre-act checkpoint in both runtimes, in manual
  mode only.
- **B2, Phase 2** — candidate scoring with a confidence, approval routing
  decided in the agent, a replay harness, and decision metrics.
- **B3, Phase 2a** — the required-HITL class registry, signed approval tokens
  issued by the orchestrator with a persisted key and used-nonce record, and
  fail-closed release of a held action.
- **D1, Phase 3, cuttable** — automated mode for allowed low-risk classes,
  its promotion rule and kill switch. Cut whole, under the plan's
  [cut clause](../v0.4.0-plan.md#scope-locks), if it has not merged when the
  last core PR starts the release gate.

**The no-change contract.** With no `decision:` block, an agent behaves as in
v0.3.16: the checkpoint records each choice and changes none (E-2), and
`make eval-replay` and the offline demo stay unchanged. PRs 5 and 6 prove it
byte for byte.

### What is already in place (do not rebuild)

- `agents/persona_types.py` — `ActionType`, the persona's action vocabulary.
  `REQUEST_APPROVAL`, `GRANT_APPROVAL`, `DENY_APPROVAL`, `DELEGATE` and
  `SPAWN_SUB_AGENT` reach `agents/action_executor.py` and return
  `not_implemented`.
- `agents/persona_runtime/action_loop.py` — the turn. A persona's tool calls
  run inside its model loop, in `_ToolRoundMixin._execute_tools`
  (`agents/persona_runtime/tool_round.py`), before `_parse_actions`. After it,
  `synthesize_channel_reply`, reflexion and `enforce_single_channel_turn` still
  add, rewrite and drop actions, so the final action list exists only after
  the guard.
- `agents/base.py` `_run_llm_loop` — the task agent's step; tool dispatch is
  the point where a choice exists. Personas never run its `_execute_tools`:
  the persona's own copy in `tool_round.py` wins.
- `agents/call_log.py` — the one-JSON-line-per-call file the decision log
  copies. `agents/observability/spans.py` and the `_metrics_*.py` modules —
  the span-name and metrics pattern.
- `internal/security/audit_event.go` — the audit vocabulary. `agent.deliberated`
  is commented there as the precursor of `DecisionRecord`, and `hitl.*`
  constants are reserved with no producer.
- `evaluators/` — the RFC 0044 replay harness; `persona_driver._collect_events()`
  returns `[]` and is the seam where decision records join an eval run.
- `autonomy.requires_approval_for` in `schemas/agent.schema.json` — a string
  list with no reader anywhere.

### What this plan needs from the prerequisite plans

| Row | Needed | Read by |
|-----|--------|---------|
| A1 | A society table for the approval-signing key, behind the society-store interface, beside the pending approvals | PR 12 |
| A4 | The pending-approval queue (RFC 0009 §F): a held action's row carrying its token's nonce and an approved → spent state, which is the used-nonce record (E-8); its REST approve and deny routes, its expiry, and its path back to the agent; agent tokens, so an agent token cannot approve; PR 13's spend route among the token-checked agent routes. B3's hold replaces §F's agent-side pause: A4 builds the queue, not a second pause | PRs 13–14 |
| A4 | Tools tagged `irreversible`; where `hitl_enabled` lives and its default (RFC 0009 puts the flag in `config/agents.yaml`) | PR 11 |

---

## Phase 0 — open questions and design points

The RFC's open questions, settled from the 2026-10-18 defaults and the plan's
locks. An answer marked *Confirmed here* is settled by accepting this plan; a
*Default* is confirmed or overturned in the review of the PR named.

| # | Question | Answer | Settled in |
|---|----------|--------|-----------|
| OQ 1 | Which classes may run in automated mode in v0.4.0? | The allowed list ships empty. An operator may allow `respond` and `defer`, never a class that needs approval or is required-HITL. v0.5.0 looks again. | Default; PR 15 |
| OQ 2 | Approval routing: orchestrator or agent first? | Agent first: in Phase 2 the agent decides that a class needs approval and defers with `reason=approval_required` (PR 9). From Phase 2a the hold and the release are the orchestrator's, on A4's queue: PR 14 holds the agent's approval-list classes as well as the required-HITL ones. | Confirmed here |
| OQ 3 | The confidence threshold for promotion | The method is fixed: worked out with the PR 8 replay harness over manual-mode records, per persona type. PR 15 records the number before any class is promoted; with none recorded, nothing is promoted. | Default; PR 15 |
| OQ 4 | A protobuf decision event in v0.4.0? | Already resolved: no. Records stay in the agent, which the RFC 0029 amendment confirms (Phase 5 unslotted). | Resolved |
| OQ 5, 6 | Collective strategy and dissent | Phase 4, v0.5.0+; outside this plan. | — |
| OQ 7 | Which classes are required-HITL in every environment? | Of §H's four: *security-sensitive policy changes* and *high-impact organizational actions* hold even with `hitl_enabled: false`. *Irreversible operations* (tools tagged `irreversible`) and *external communications* follow `hitl_enabled`, as RFC 0009 §F made the gate opt-in per deployment. In v0.4.0 the organizational class covers RFC 0012's runtime graph change and the irreversible class covers the tools A4 tags; no shipped action falls in the other two, since no external bridge ships and nothing changes permissions at runtime. | Default; PR 11 |
| OQ 8 | Who issues approval tokens? | Already resolved: the orchestrator. | Resolved |
| OQ 9 | Which key signs approval tokens? | **The plan-opening default is confirmed** by the society-backend lock: a dedicated key the orchestrator generates once and keeps in the society tables in `data/channels.db`, beside the pending approvals. The used-nonce record is the pending approval itself, whose row a spend flips from approved to spent (E-8). Phase 2a step 2's wording is corrected with this plan. | Confirmed here; PR 12 |

Points the RFC leaves open or states two ways, each settled by default in the
PR named:

- **E-1 The default mode is manual.** §B's table makes semi-automated "the
  default in v0.4.0", and manual mode requires approval "for high-impact
  classes". Either would change every v0.3.16 deployment. With no `decision:`
  block an agent runs manual mode, and its approval list is
  `autonomy.requires_approval_for` (E-3), empty unless set. Semi-automated
  mode is opt-in per agent, after baseline records exist (next step 3). PR 4.
- **E-2 Phase 1 adds records, not gates.** Permissions, channel access and the
  wallet stay where they are enforced today. A refused action is refused by
  the same code with the same text, so recorded replies do not move. The
  checkpoint's own enforcement starts with inputs that have no other gate: the
  approval list (PR 9), required-HITL classes (B3) and directives (RFC 0012
  C3). Before a policy may choose (PR 7, semi-automated), the constraint stage
  calls those existing gates read-only — `PermissionGate`, channel membership,
  the wallet's budget check — so selection is among permitted candidates, as
  §C and the Security Considerations require. PR 2.
- **E-3 Autonomy levels.** Goal 4 maps `manual`, `semi-autonomous`,
  `autonomous` and `supervisor`. The schema's levels are `passive`,
  `reactive`, `semi-autonomous`, `autonomous` and `supervisor`, and they only
  decide whether a tick scheduler starts. v0.4.0 keeps that unchanged: the
  level does not set the mode. `requires_approval_for` becomes the list of
  classes that need approval, checked against the class names, rather than a
  second key. PR 4 also rewrites the glossary's Autonomy Level entry, which
  says the level controls what an agent may initiate without approval.
- **E-4 Action classes.** PR 1 maps every `ActionType` and each task-agent
  step onto §A's six classes, and a test fails on an unmapped new member.
  `DO_NOTHING` is `defer`.
- **E-5 One record type.** `DecisionRecord` names its subject — the next
  action, or an RFC 0012 directive — so §F's accept / adapt / refuse needs no
  second record. PR 1.
- **E-6 Pre-delegate waits.** No delegation runs today (RFC 0010 is
  unslotted), so the checkpoint is defined and not wired. Pre-tool and
  post-outcome are wired in PR 10, where calibration first needs them, in
  both places tools run: the persona's `tool_round.py` and the task agent's
  executor.
- **E-7 Dashboards.** The repository holds no dashboard files, so Phase 2
  item 4 ships as metrics plus documented queries in `docs/observability.md`.
  PR 10.
- **E-8 Who checks a token, and what releases it.** HMAC-SHA256, checked only
  by the orchestrator; agents never hold the key. A token alone releases
  nothing. One transaction checks the signature, finds the pending approval
  by the token's nonce, requires it to be `approved`, unexpired, for the same
  action hash and for the calling agent (the caller's agent token is the
  token's `actor_id`), and flips it to `spent`. A missing, spent or expired
  row refuses. So reading the key out of `data/channels.db`, which is
  world-readable today, mints nothing a person did not approve, and no clock
  step revives a used token. PRs 12–13.
- **E-9 One hold, one audit family.** B3's hold replaces RFC 0009 §F's
  agent-side pause, so a tool is held once. B3 adds RFC 0028's
  `decision.hitl_requested`, `_approved`, `_denied` and `_expired`, PR 13
  retires RFC 0009's unused `hitl.*` constants, and a test asserts that one
  hold's transition writes one event. PR 13.
- **E-10 The decision log** is one file per agent, rotated at a size cap set
  in PR 3, which also says how many rotated files are kept. Records pass
  through a real secret redactor (PR 3a) before writing. A channel-message
  candidate is stored as its class, ids and a content hash, never its text, so
  the log does not become a confidentiality sink (RFC 0037).

The [decisions-as-memory amendment](0028-amendment-decisions-as-readable-memory.md)
is unslotted with RFC 0049 Phase 3 and is not in this plan.

---

## Progress Overview

| PR | Row | Title | Branch | Status | GitHub PR | Merged |
|----|-----|-------|--------|--------|-----------|--------|
| 1 | B1 | Decision types, action classes, glossary | `feature/v040-rfc0028-types` | ⬜ | — | — |
| 2 | B1 | Selection pipeline and baseline policy | `feature/v040-rfc0028-pipeline` | ⬜ | — | — |
| 3a | B1 | Python secret redactor | `feature/v040-rfc0028-redactor` | ⬜ | — | — |
| 3 | B1 | Decision log and checkpoint spans | `feature/v040-rfc0028-decision-log` | ⬜ | — | — |
| 4 | B1 | `decision:` configuration and schema | `feature/v040-rfc0028-config` | ⬜ | — | — |
| 5 | B1 | Persona pre-act checkpoint | `feature/v040-rfc0028-persona-preact` | ⬜ | — | — |
| 6 | B1 | Task-agent pre-act checkpoint | `feature/v040-rfc0028-task-preact` | ⬜ | — | — |
| 7 | B2 | Scoring with confidence; semi-automated mode | `feature/v040-rfc0028-scoring` | ⬜ | — | — |
| 8 | B2 | Replay harness for decision traces | `feature/v040-rfc0028-replay` | ⬜ | — | — |
| 9 | B2 | Approval routing in the agent | `feature/v040-rfc0028-approval-routing` | ⬜ | — | — |
| 10 | B2 | Pre-tool and post-outcome; decision metrics | `feature/v040-rfc0028-outcome-metrics` | ⬜ | — | — |
| 11 | B3 | Required-HITL class registry | `feature/v040-rfc0028-hitl-registry` | ⬜ | — | — |
| 12 | B3 | Approval tokens and the persisted key (Go) | `feature/v040-rfc0028-approval-tokens` | ⬜ | — | — |
| 13 | B3 | Tokens on the pending-approval queue (Go) | `feature/v040-rfc0028-approval-release` | ⬜ | — | — |
| 14 | B3 | Fail-closed hold and release in the agent | `feature/v040-rfc0028-hitl-enforcement` | ⬜ | — | — |
| 15 | D1 | Automated mode, promotion rule, kill switch *(cuttable)* | `feature/v040-rfc0028-automated-mode` | ⬜ | — | — |
| 16 | D1 | Mode-selection guidance *(cuttable)* | `feature/v040-rfc0028-mode-guide` | ⬜ | — | — |
| 17 | — | Review follow-ups and closeout | `feature/v040-rfc0028-closeout` | ⬜ | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ✂️ Cut

## Dependency graph

```
R1 (this plan accepted)
PR 1 ──► PR 2 ──► PR 3a ──► PR 3 ──► PR 4 ──► PR 5 ──► PR 6       ═══ B1 done
PR 6 ──► PR 7 ──► PR 8 ──► PR 9 ──► PR 10                         ═══ B2 done
PR 10 ──► PR 15 ──► PR 16                                         ═══ D1 (cuttable)
PR 10 ──► PR 11 ──► PR 12 ──► PR 13 ──► PR 14                     ═══ B3 done
A1 society tables ──► PR 12
A4 queue, agent tokens, irreversible tags ──► PRs 11, 13, 14
PR 14 ──► RFC 0012 C3 ──► PR 17
```

The hard edges:

- **B1 → B2 → B3** — the RFC's phase order, and the v0.4.0 plan's.
- **A1 before PR 12** — the key lives in A1's tables.
- **A4 before PRs 11 and 13** — the registry covers the tools A4 tags
  `irreversible`, and tokens release A4's pending approvals.
- **B3 before RFC 0012 C3** — its tie escalation and graph changes wait on
  approvals.
- **B2 before D1** — automated mode ranks by PR 7's scores and is promoted by
  PR 8's replay. If PR 15 lands before PR 11, PR 11 adds the registry to the
  classes automated mode refuses.
- **RFC 0041 Phase 1 (D4)** shares `action_loop.py` and `base.py` with PRs 5
  and 6. That is not an edge: whichever merges second rebases.

---

## PR Sequence

Every PR: the failing test first and confirmed red; `make lint` and the
language's suites green; `make validate` after any config change; docs,
glossary and status moved in the same PR; review findings recorded under the
PR, numbered `F-n`.

### PR 1 — `feature/v040-rfc0028-types`

**Scope**: a new `agents/decision/` with `types.py`: `ActionClass` (§A's six),
`Checkpoint` (the four), `DecisionContext`, `DecisionCandidate`,
`DecisionOutcome`, `DecisionRecord`, and a `DecisionPolicy` protocol with
`select(context, candidates)`. The eight glossary entries the RFC's next steps
name: *Decision Policy Engine*, *decision checkpoint*, *DecisionRecord*,
*human-in-the-loop / HITL*, *approval token*, *agent society*, *collective
scope*, *individual scope*. RFC 0028 and its ROADMAP rows move to
🚧 Implementing.
**Implementation**: E-4 and E-5. The types are frozen, and a record converts
to one JSON line and back unchanged, because that line is the replay format.
**Tests**: the closed enums; every `ActionType` member mapped; the JSON round
trip is stable; nothing in `agents/decision/` imports a runtime loop.
**Estimate**: ~300 lines.

### PR 2 — `feature/v040-rfc0028-pipeline`

**Scope**: `agents/decision/pipeline.py` and `baseline.py`.
**Implementation**: §C's five steps as one pure function — build the
candidates, apply the constraint inputs, score, pass the approval gate,
select. If nothing survives, the outcome is `defer` with its reason. The
baseline policy (manual mode) ranks the runtime's own proposal first. E-2:
the runtime gets the pipeline with no enforcing inputs; those arrive with
PR 7, PR 9, B3 and RFC 0012 C3.
**Tests**: constraint filtering with fake inputs; the empty-survivor `defer`;
deterministic ranking; manual mode never selects a candidate that a
constraint removed.
**Estimate**: ~350 lines.

### PR 3a — `feature/v040-rfc0028-redactor`

**Scope**: a Python secret redactor in `agents/observability/` implementing the
existing `Redactor` protocol, whose only implementation today is the
pass-through `NoopRedactor`. Its patterns are generated from Go's
`SecretRedactor` (`internal/security/redactor.go`) by extending
`cmd/genpatterns`, with a parity gate like
`make generate-sanitizer-patterns-check`.
**Implementation**: the decision log (PR 3) calls it. Installing it for logs
and spans is out of scope, so log output does not change.
**Tests**: each Go pattern masks the same sample in Python; the gate fails on
a hand edit of the generated file.
**Estimate**: ~350 lines.

### PR 3 — `feature/v040-rfc0028-decision-log`

**Scope**: a new `agents/observability/decision_log.py`; a checkpoint span name
in `spans.py`; a new `_metrics_decision.py` (`metrics.py` is at 500 lines).
**Implementation**: §D's two sinks. The log writes one JSON line per record,
in one write, as `call_log.py` does, redacted first by PR 3a's redactor
(E-10). A channel-message candidate is written as its class, ids and a
content hash, not its text. It is the source of truth for replay. The span
carries the selected class, the candidate count and the confidence; spans are
sampled and not relied on. A failing sink counts a metric and never fails the
turn. The size cap and the number of rotated files kept are config keys.
**Tests**: one line per record; a secret in a candidate is redacted by the
real redactor; message text never reaches the file; rotation at the cap keeps
the configured number of files; a sink error leaves the turn's result
unchanged; the span attributes.
**Estimate**: ~350 lines.

### PR 4 — `feature/v040-rfc0028-config`

**Scope**: a `decision:` block in `schemas/agent.schema.json` (`mode`, and
class names for `autonomy.requires_approval_for`); a commented example in
`config/agents.yaml`; a new `agents/decision/config.py`; the glossary's
Autonomy Level entry rewritten (E-3); the `requires_approval_for` examples in
`docs/ai-agents-orchestration-spec.md` and `docs/persatrix-extension-spec.md`
moved to class names, and a line for the release's Upgrade Notes.
**Implementation**: Phase 1 item 4, with E-1 and E-3. With no block the mode is
manual and the approval list is `autonomy.requires_approval_for`, empty
unless set; the autonomy level still only decides the tick scheduler.
**Tests**: the schema accepts the three modes and refuses unknown classes; the
defaults; an agent with no block and no list reads manual and nothing to
approve; one with `requires_approval_for` and no block keeps its list.
**Estimate**: ~300 lines.

### PR 5 — `feature/v040-rfc0028-persona-preact`

**Scope**: the pre-act checkpoint in `agents/persona_runtime/action_loop.py`
(426 lines) on the final action list, after `synthesize_channel_reply`,
reflexion and `enforce_single_channel_turn`, one record per action. Every
early return of the turn writes a `defer` record with its reason: replay,
the response gate, salience silence, an empty TICK, a model-call error, no
response, MAX_TOKENS. The tick loop's `max_actions_per_tick` cut
(`agents/tick.py`) records the actions it drops.
**Implementation**: each final action is the candidate; manual mode selects
it, and the log takes the record. Later gates sit on this seam (PR 9,
PR 14, RFC 0012 PR 10), so they see the publish that actually runs, and a
deferred DM reply publishes nothing, not the "…" placeholder. The salience
gate's verdict becomes a DecisionRecord and `agent.deliberated` is emitted
from it, as RFC 0051's amendment asks. Phase 1 item 2, persona half.
**Tests**: a scripted turn writes one record per final action with its class;
a plain-text reply is recorded as the publish it becomes; a gated silence
writes its record; every recorded golden replays byte-identically under
`make eval-replay`; the offline demo's transcript is unchanged.
**Estimate**: ~400 lines.

### PR 6 — `feature/v040-rfc0028-task-preact`

**Scope**: `agents/base.py` (505 lines) — first a pure-move commit taking
`_execute_tools` into its own module, a real seam; then the checkpoint in
`_run_llm_loop` where a tool call or a final reply is chosen. Personas do not
run this copy; PR 10 wires both executors.
**Tests**: a task-agent step writes its record; the workflow suites pass
unchanged.
**Acceptance**: Phase 1 complete; baseline records collected from here on.
**Estimate**: ~350 lines, of which the move is ~80.

### PR 7 — `feature/v040-rfc0028-scoring`

**Scope**: `agents/decision/scoring.py`; semi-automated mode in the pipeline.
**Implementation**: Phase 2 item 1. The candidates are the runtime's proposal
plus `defer`, which is always available. Before scoring, the constraint
stage calls the existing gates read-only — `PermissionGate`
(`agents/tools/permissions.py`), channel membership and the wallet's budget
check — and removes what they would refuse (E-2). A rule-weighted scorer,
with its weights in config, gives each survivor a score and a confidence in
[0, 1]. Below the configured floor the outcome is `defer` with
`reason=low_confidence`.
**Tests**: deterministic ranking; confidence bounds; a candidate the
permission gate would refuse is never selected in any mode.
**Estimate**: ~450 lines.

### PR 8 — `feature/v040-rfc0028-replay`

**Scope**: a new `evaluators/decision_replay.py` and a Make target; decision
records fed into `EvalRun.events` through `persona_driver._collect_events()`,
so an eval set can assert on them with the existing `event_sequence` matcher.
**Implementation**: Phase 2 item 3. It replays a recorded log through a policy
and reports each record whose selection changed.
**Tests**: replaying through the same policy gives no change; a changed
weight gives exactly the expected changes; an eval set asserts a record
sequence.
**Estimate**: ~400 lines.

### PR 9 — `feature/v040-rfc0028-approval-routing`

**Scope**: the approval gate step in `pipeline.py`.
**Implementation**: Phase 2 item 2 and OQ 2. A selected candidate in a class
on the agent's approval list becomes `defer` with `reason=approval_required`,
recorded, and nothing runs until PR 14 puts these classes on A4's queue. The
classes are `delegate` and `publish_channel`; no external bridge exists to
route.
**Tests**: each listed class defers in manual and semi-automated mode; an
empty list changes nothing.
**Estimate**: ~250 lines.

### PR 10 — `feature/v040-rfc0028-outcome-metrics`

**Scope**: the pre-tool and post-outcome checkpoints (E-6) in both places
tools run — `agents/persona_runtime/tool_round.py` and PR 6's task-agent
executor module; acceptance, rejection and fallback counters in
`_metrics_decision.py`; a decision section in `docs/observability.md` with
the queries (E-7).
**Implementation**: Phase 2 item 4. Post-outcome records whether the selected
action ran, failed or was refused; a `PermissionError` counts as refused in
both executors.
**Tests**: each checkpoint writes its record in each runtime; the counters
move as each outcome is recorded.
**Acceptance**: Phase 2 complete.
**Estimate**: ~400 lines.

### PR 11 — `feature/v040-rfc0028-hitl-registry`

**Scope**: a `hitl:` block in `config/security.yaml` and
`schemas/security.schema.json`; the readers in `agents/decision/` and Go's
`LoadSecurityConfig`, which also read `hitl_enabled` where A4 puts it.
**Implementation**: Phase 2a item 1 with OQ 7. The four §H classes, each with
what it covers. The two classes required in every environment cannot be
switched off: validation refuses a config that removes them.
**Tests**: the schema; each covered action is classified; `hitl_enabled:
false` lifts only the two switchable classes.
**Estimate**: ~300 lines.

### PR 12 — `feature/v040-rfc0028-approval-tokens`

**Scope**: a new `internal/security/approval_token.go` and its tests.
**Implementation**: Phase 2a item 2 with OQ 9 and E-8. The token is signed
over `(action_hash, actor_id, scope, expires_at, nonce)`. The key is made
from `crypto/rand` on first start and stored through A1's interface. A token
expires no later than the pending approval it answers (RFC 0009's 24 h
default). Spending is PR 13's, on the approval row (E-8).
**Tests**: sign and check; each field changed is refused; expired is refused;
**after a restart** an earlier token still checks; the key appears in no log
and on no route.
**Migration**: none — A1 owns the tables.
**Estimate**: ~400 lines.

### PR 13 — `feature/v040-rfc0028-approval-release`

**Scope**: A4's approve, deny and expiry paths mint, refuse and expire tokens;
a spend route that an agent calls, with its `auth_policy.go` entry and a
place among A4's token-checked agent routes; the four `decision.hitl_*` audit
events (security class), with RFC 0009's unused `hitl.*` constants retired.
**Implementation**: approving a pending approval mints its token, bound to the
held action's hash. A spend is one transaction (E-8): the row must be
`approved`, unexpired, for the same hash and for the calling agent, and it
becomes `spent`. Only a person approves. Under `auth.mode: enabled` that is
an operator account. Under `disabled`, where every caller is the anonymous
`local` identity, the approve and deny routes need an operator-set approval
secret, as unquarantine needs `SECURITY_UNQUARANTINE_TOKEN`. An agent token
never approves. E-9.
**Tests**: requested, approve, deny and expire each write their event once;
an agent token cannot approve; under `disabled` an approve without the
secret is refused; a spend by another agent, for another action's hash, or
of a spent or expired row is refused.
**Estimate**: ~400 lines.

### PR 14 — `feature/v040-rfc0028-hitl-enforcement`

**Scope**: the agent side — a candidate in a required-HITL class or on the
agent's approval list (PR 9) opens a pending approval and defers. So does a
required-HITL tool call at the pre-tool seat in both executors (PR 10): it
does not run, and the model gets a rejection result. A released action runs
only after a successful spend; a released tool call's result reaches the
persona's next turn as an event.
**Implementation**: Phase 2a item 3. A missing, invalid, expired or replayed
token gives `defer` with `reason=hitl_required`, and nothing runs, in every
mode.
**Tests**: the security tests — no mode runs a required-HITL action or tool
call without a token; an approval-list class reaches the queue and releases;
an approval survives an orchestrator restart and still releases once;
the plan's acceptance, "held, released only by a signed, scoped, expiring
approval token that survives an orchestrator restart, and refused on replay".
**Acceptance**: Phase 2a complete.
**Estimate**: ~400 lines.

### PR 15 — `feature/v040-rfc0028-automated-mode` *(cuttable)*

**Scope**: automated mode in the pipeline; an allowed-class list; the
confidence floor, forced fallback and kill switch.
**Implementation**: Phase 3 items 1–3 with OQ 1 and OQ 3. Automated mode
selects the top candidate directly, only for an allowed class. A required-HITL
or approval-needing class is never automated. The kill switch puts every
agent back in manual mode at once.
**Tests**: an unlisted class falls back to semi-automated; required-HITL
classes cannot be allowed; the kill switch; with no recorded threshold
nothing is promoted.
**Estimate**: ~350 lines.

### PR 16 — `feature/v040-rfc0028-mode-guide` *(cuttable)*

**Scope**: Phase 3 item 4 — which mode to choose for development, staging and
production, in a section of `docs/guides/persona-agents.md` or its own guide
if that one has no room.
**Estimate**: ~200 lines.

### PR 17 — `feature/v040-rfc0028-closeout`

**Purpose**: the review findings deferred from PRs 1–16, paraphrased by
component; RFC and ROADMAP status to ⚠️ Partially Implemented (Phases 1–2a,
plus Phase 3 if taken, shipped in v0.4.0; Phase 4 for v0.5.0+); D1's rows
marked ✂️ or *taken, not cut*.

---

## File-size constraints

| File | Lines now | Plan |
|------|-----------|------|
| `agents/base.py` | 505 | PR 6 moves `_execute_tools` out first |
| `agents/observability/metrics.py` | 500 | PR 3 adds a `_metrics_decision.py` module |
| `agents/persona_runtime/action_loop.py` | 426 | PR 5 adds the call; the logic lives in `agents/decision/` |
| `internal/security/audit_event.go` | 305 | PR 13 adds four constants |

## Risks

| Risk | Mitigation |
|------|------------|
| The checkpoint changes replies and breaks the goldens. | E-1 and E-2; PRs 5 and 6 replay every golden. |
| A used token works again after a restart. | A spend flips the persisted approval row to spent in one transaction (E-8); PRs 12–13 test across a restart. |
| A persona approves its own action. | Only a person approves: an operator account, or under `disabled` the approval secret (PR 13); PR 14's security tests. |
| The decision log fills the disk or leaks secrets. | PR 3a's redactor, message text kept as hashes, and rotation (E-10). |
| D1 slips. | Cuttable whole; nothing in the core depends on it. |

## Notes

> 2026-10-20 — Plan written for row R1, proposed with RFC 0028's move to
> Accepted, and Phase 2a step 2 corrected as open question 9 recorded. The
> first implementation PR moves it to 🚧 Implementing.

## Related documentation

- [RFC 0012 PR plan](0012-pr-plan.md) — the directive input and the graph-change class that use this engine
- [v0.4.0 plan](../v0.4.0-plan.md) — rows B1–B3 and D1, the scope locks, the acceptance
- [Release cycle §Phase 1](../methodology/release-cycle.md#phase-1--implementation-prs) · [review process](../methodology/review-process.md)
- [Development workflow §Phase 3](../development-workflow.md#phase-3--pr-plan-creation)
