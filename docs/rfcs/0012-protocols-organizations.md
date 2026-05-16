---
id: RFC-0012
title: Protocols & Organizations
summary: Define the organization model — a named graph of personas with roles and a hierarchy / flat / matrix topology — and, on top of it, the authority axis: a persona reasoning about whether a directive issued in one context should influence its behaviour in another, and accepting, adapting, or refusing it. The integrity counterpart to RFC 0037's confidentiality axis. Phases 1–3 (organizations & authority) target v0.4.0; Phases 4–5 (meeting / negotiation protocols, inter-org federation) target v0.5.0.
type: architecture
status: proposed
author: Maksim Khomutov
created: 2026-05-16
target: v0.4.0 (Phases 1–3 — organizations & authority); v0.5.0 (Phases 4–5 — meeting/negotiation protocols, inter-org federation)
depends_on:
  - RFC-0009
  - RFC-0011
  - RFC-0028
  - RFC-0037
---

# RFC 0012 — Protocols & Organizations

**Type**: architecture
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-05-16
**Target**: v0.4.0 (Phases 1–3 — organizations & authority); v0.5.0 (Phases 4–5 — meeting/negotiation protocols, inter-org federation)
**Depends on**: RFC 0009 (Agent Identity, Security & Sandboxing — identity tokens, the mandatory-HITL gate, capability tokens, audit), RFC 0011 (Channels — the channel surface the authority axis annotates), RFC 0028 (Agent Decision Policy Engine — the cross-context influence decision is evaluated through its checkpoints), RFC 0037 (Memory Confidentiality & Channel Classification — the *confidentiality* axis this RFC is the integrity counterpart to)
**Relates to**: RFC 0010 (Sub-Agent Spawning — reserved, not yet written; spawned sub-agents inherit org context), RFC 0021 (Persona Temporal Awareness — the commitments memory class a directive provenance record rides on), RFC 0029 (Personal/Society Storage Split — the org graph is society state and lives in the society store), RFC 0005 (Persona Agent & Memory System — persona definition gains an org role)

---

## Table of Contents

- [Summary](#summary)
- [Motivation](#motivation)
- [Goals](#goals)
- [Non-Goals](#non-goals)
- [Design / Implementation](#design--implementation)
  - [A. The two-axis model](#a-the-two-axis-model)
  - [B. The organization model](#b-the-organization-model)
  - [C. Authority relations](#c-authority-relations)
  - [D. The channel authority level](#d-the-channel-authority-level)
  - [E. Directive provenance and the cross-context influence record](#e-directive-provenance-and-the-cross-context-influence-record)
  - [F. The accept / adapt / refuse decision](#f-the-accept--adapt--refuse-decision)
  - [G. Persona clearance and authority from role](#g-persona-clearance-and-authority-from-role)
  - [H. The enforced confidentiality-egress gate](#h-the-enforced-confidentiality-egress-gate)
  - [I. Meeting and negotiation protocols (v0.5.0)](#i-meeting-and-negotiation-protocols-v050)
  - [J. Storage](#j-storage)
- [Security Considerations](#security-considerations)
- [Phased Implementation Plan](#phased-implementation-plan)
- [Files Touched (Estimated)](#files-touched-estimated)
- [Test Strategy](#test-strategy)
- [Open Questions](#open-questions)
- [Decision / Next Steps](#decision--next-steps)
- [Related Documentation](#related-documentation)

---

## Summary

The v0.4.0 promise is: *define a company, research lab, or team with
roles and hierarchy — and let it run.* There is no organization primitive
to do that with. `config/organizations.yaml` exists only as a commented
sketch; personas relate to each other through ad-hoc per-agent
`relationships` entries (`reports_to`, `peer`) with no graph, no roles,
and no notion of authority.

This RFC defines two things:

1. **The organization model** — an organization is a named graph of
   personas with **roles** and a **topology** (hierarchy, flat, or
   matrix). It is declared in `config/organizations.yaml`, backed by the
   society store, and it is what makes "X manages Y", "A and B are
   peers", "this is a cross-functional matrix report" *structured* facts
   the runtime can reason about rather than free text in a prompt.

2. **The authority axis** — the **integrity** counterpart to
   [RFC 0037](0037-memory-confidentiality-channel-classification.md)'s
   confidentiality axis. RFC 0037 governs what information may flow *out*
   of a context (a persona must not leak a confidential channel). This
   RFC governs what may flow *in*, and how much it is trusted: a
   **directive** issued in one context — an instruction, a commitment, a
   policy — carries weight in another context only in proportion to the
   issuer's **authority** over the recipient. A persona maintains
   awareness of directives across all of its channels and conversations
   and, for each one, decides — through the [RFC 0028](0028-agent-decision-policy-engine.md)
   decision engine — to **accept**, **adapt**, or **refuse** it
   according to its own policy. When two contexts issue conflicting
   directives, the conflict is resolved by authority precedence, which is
   only rankable because the organization graph exists.

Phases 1–3 (v0.4.0) deliver the organization model and the authority
axis. Phases 4–5 (v0.5.0) deliver the **meeting and negotiation
protocol** scaffolding and inter-organization federation — the
"Protocols" half of this RFC's long-reserved scope.

## Motivation

### Organizations are the v0.4.0 product

Every v0.4.0 RFC — sub-agent spawning (RFC 0010), the skill registry
(RFC 0014), the decision policy engine (RFC 0028) — assumes a structure
the personas operate *within*. Without an organization model, "the
mobile team", "your manager", "escalate this" are prompt fictions with
no referent the runtime can check. RFC 0028 §G already names the gap
explicitly: collective decisions need "organizations and role
hierarchies (currently tracked as RFC 0012 placeholder)" to define
eligibility and vote weight. This RFC fills the placeholder.

### Concurrent-context awareness needs an integrity axis

A persona is a member of many channels at once and reasons across them:
what it learned or was told in one bears on how it should behave in
others. RFC 0037 secured one direction of that — confidentiality, what
must not flow out. The unsecured direction is **influence in**: if a
persona treats every instruction it encounters, in any channel, as
equally binding, then a directive planted in a low-trust public channel
can steer its behaviour in a high-trust one. That is cross-context prompt
injection at the *semantic* level — not a delimiter exploit (RFC 0034 /
RFC 0036 sanitization already handles those) but a plausible-looking
"directive" that should never have carried weight.

The defence is an **authority model**: an instruction is treated as a
binding *directive* only when its issuer has authority over the recipient
*and* it was issued in a context of sufficient authority. Everything else
is an *observation* or a *request* — it may inform the persona, but it
does not bind it. A persona can then legitimately **refuse** influence
that lacks authority, **adapt** to influence that has partial authority,
and **accept** influence that has full authority — "reasoning about
cross-context influence according to its own policy."

### Why authority needs organizations — and confidentiality did not

[RFC 0037](0037-memory-confidentiality-channel-classification.md) ships
in v0.3.x because confidentiality is **local**: a single persona's own
memory store plus channel config is enough to decide "this memory is too
sensitive for that channel." Authority is **relational**. "Directive A
outranks directive B" has no meaning until A's issuer and B's issuer can
be *ranked* — and ranking issuers is exactly what an organization graph
is for. There is no principled, non-arbitrary authority decision without
roles and a topology. That is the clean line along which RFC 0037 and
this RFC were split: confidentiality is enforceable without
organizations and shipped first; authority is not and ships here.

### The enforced egress gate belongs here too

RFC 0037's leak tripwire (§G of that RFC) is **logging-only** —
deliberately, because *blocking* a persona's message is an
organizational-policy decision: whose policy authorizes the block, and
where does the blocked message escalate? v0.3.x has no answer. An
organization does: a blocked egress routes to a role-holder for review.
This RFC promotes RFC 0037's tripwire signal into an **enforced** gate
(§H) via RFC 0028's mandatory human-in-the-loop machinery.

## Goals

1. An **organization** is declarable in `config/organizations.yaml`: a
   named entity with a topology (`hierarchy` | `flat` | `matrix`),
   **roles**, and a persona-to-role assignment.
2. Personas are tagged with organization membership and a role; the org
   graph is persisted in the society store.
3. **Authority relations** — "who may direct whom, in what scope" — are
   derived deterministically from the org topology and roles.
4. A **channel authority level** is declared per channel: the integrity
   counterpart of RFC 0037's confidentiality classification, an
   orthogonal second axis.
5. Memory entries that encode a **directive** carry a **cross-context
   influence record**: issuer, issuing channel, that channel's authority
   level, and the issuer's authority over the recipient.
6. A persona evaluates each cross-context directive — **accept / adapt /
   refuse** — through the RFC 0028 decision engine, with conflicts
   resolved by authority precedence and ties escalated.
7. A persona's **confidentiality clearance** and **authority** both
   derive from its organizational role; a **membership-time clearance
   check** enforces RFC 0037 classifications at channel-join time.
8. RFC 0037's logging-only leak tripwire is promoted to an **enforced
   egress gate** under an RFC 0028 mandatory-HITL class.
9. (v0.5.0) **Meeting and negotiation protocols** — structured
   multi-agent interaction patterns — and a sketch of inter-organization
   federation.

## Non-Goals

- **Confidentiality mechanics.** Channel classification, memory
  protection levels, declassification projections — all owned by
  [RFC 0037](0037-memory-confidentiality-channel-classification.md). This
  RFC consumes RFC 0037's classification; it does not redefine it.
- **Replacing the decision engine.** The accept/adapt/refuse evaluation
  runs *through* [RFC 0028](0028-agent-decision-policy-engine.md); this
  RFC is a consumer of its checkpoints, not a second decision system.
- **Collective / quorum decision mechanics.** How a *group* of agents
  selects one action is RFC 0028 Phase 4 (v0.5.0+). This RFC supplies the
  org graph that RFC 0028 Phase 4 needs (role eligibility, vote weight);
  it does not implement the aggregation.
- **Sub-agent spawning mechanics.** Owned by RFC 0010. This RFC defines
  the org context a spawned sub-agent inherits, not the spawn machinery.
- **External / human org-chart synchronization.** Bridging an
  organization to a real company directory is an external-bridge concern
  (RFC 0011 external, v0.5.0+).
- **Encryption or cryptographic trust.** Authority is an access-control
  and decision concept, not a cryptographic one. Identity tokens and
  signing remain RFC 0009's domain.

## Design / Implementation

### A. The two-axis model

A channel — and, derived from it, every memory entry — sits on **two
orthogonal axes**:

| Axis | RFC | Governs | Direction | Question it answers | Classic analogue |
|------|-----|---------|-----------|---------------------|------------------|
| **Confidentiality** | 0037 | what may flow **out** of a context | egress | "May I *say* this here?" | Bell–LaPadula — *no write down* |
| **Authority / integrity** | 0012 (this RFC) | what may flow **in** and how much it binds | ingress | "Should this *influence* me?" | Biba — *no trust up* |

The two axes are independent. A public all-hands channel is *low
confidentiality* (anything said there is public) **and** *low authority*
(an instruction posted there does not bind anyone). A leadership DM is
*high confidentiality* **and** *high authority*. A whistleblower DM might
be *high confidentiality* but *low authority*. Keeping the axes separate
is what makes each one's policy tractable — RFC 0037 reasons purely about
confidentiality, this RFC purely about authority, and a channel simply
carries one label on each.

Both axes name a per-channel level and both fail **closed**: RFC 0037's
unknown classification resolves to the restrictive `internal`; this
RFC's unknown authority level resolves to the *least* authoritative
`observed` (§D), so an unlabelled channel never grants directive weight
by accident.

### B. The organization model

An **organization** is a named graph declared in
`config/organizations.yaml` (today a commented sketch; this RFC makes it
real) and validated by a new `schemas/organization.schema.json`.

```yaml
organizations:
  - id: acme-engineering
    name: "Acme Corp — Engineering"
    topology: hierarchy            # hierarchy | flat | matrix
    roles:
      - id: vp-engineering
        clearance: secret          # max RFC 0037 classification (§G)
        authority: directive       # max channel authority it can wield (§D)
      - id: staff-engineer
        clearance: restricted
        authority: operational
      - id: product-manager
        clearance: restricted
        authority: operational
    members:
      - agent_id: ember-owl
        role: vp-engineering
        manages: [iron-fox, nova-sparrow]
      - agent_id: iron-fox
        role: staff-engineer
      - agent_id: nova-sparrow
        role: product-manager
```

- **Topology** is one of:
  - `hierarchy` — a tree; `manages` edges define the parent → child
    relation. A node has at most one manager.
  - `flat` — no `manages` edges; every member is a peer. No member can
    issue a binding directive to another (only requests — §C).
  - `matrix` — a member may have more than one manager, each scoped to a
    *dimension* (e.g. a functional manager and a project manager). Edges
    carry a `scope` label; authority is per-scope (§C).
- **Roles** name a `clearance` (the confidentiality ceiling, §G) and an
  `authority` (the channel-authority ceiling the role can wield, §D).
- **Members** map an existing `config/agents.yaml` persona to a role.
  A persona belongs to at most one organization in v0.4.0 (multi-org
  membership is Open Question #4).

The graph is loaded into the society store at startup and is the single
source of truth for every authority decision below. The per-agent
free-text `relationships` block in `config/agents.yaml` is **not**
removed — it continues to carry *affective* relationship state (trust
scores, bond history, RFC 0005). The organization graph carries
*structural* relationships (who reports to whom). The two are
deliberately separate: trust is earned and mutable; org structure is
declared and authoritative.

### C. Authority relations

From the topology and roles, the runtime derives a single relation:

```
directs(issuer, recipient, scope) -> bool
```

— *true* iff `issuer` may issue a binding directive to `recipient`
within `scope`. It is computed, never stored, so it cannot drift from the
graph:

- **hierarchy** — `directs(A, B, *)` iff `A` is an ancestor of `B` on the
  `manages` tree. Scope is unrestricted (`*`); a manager directs a report
  on any matter.
- **flat** — `directs` is *always false*. Peers cannot direct each other.
  A flat-org instruction from one peer to another is a **request**, never
  a directive — it may be accepted out of cooperation (§F) but it never
  *binds*, and it never wins a conflict against a directive.
- **matrix** — `directs(A, B, scope)` iff `A` is a manager of `B` on the
  dimension matching `scope`. A project manager directs project-scoped
  work; a functional manager directs functional-scoped work; neither
  directs outside its dimension.

`directs` ranks issuers for **conflict resolution** (§F): given two
directives to the same recipient, the issuer that is *higher* on the
recipient's `manages` chain wins. Two issuers with no ordering between
them (two peers; two matrix managers on different dimensions issuing in
the *same* scope) are an **unresolved tie** — escalated, never silently
broken.

### D. The channel authority level

A channel gains a second optional field — the integrity counterpart of
RFC 0037's `classification` — in `config/channels.yaml` and
`schemas/channel.schema.json`, persisted on the channel store's
`channels` table (a column added by the same migration discipline
RFC 0037 §B uses):

| Level | Rank | Meaning |
|-------|:----:|---------|
| `observed` | 0 | Content is information only. **Never** carries directive weight, regardless of issuer. The fail-closed default. |
| `peer` | 1 | Peer coordination. Carries *requests*, not directives. |
| `operational` | 2 | A working channel. Role-holders may issue scoped directives here. |
| `directive` | 3 | An authoritative channel. Directives here carry full weight. |

The channel authority level is a **ceiling on the context**, not a
substitute for issuer authority. An instruction is treated as a binding
**directive** only when **both** factors hold:

1. **Issuer authority** — `directs(issuer, recipient, scope)` is true
   (§C); and
2. **Context authority** — the issuing channel's authority level is
   `operational` or higher.

If (1) fails, the instruction is an *observation* (no authority at all).
If (1) holds but (2) fails, it is a *request* (cooperative weight, not
binding). Only both together make a *directive*. This two-factor rule is
the integrity analogue of RFC 0037's "need-to-know **and** a
sufficiently classified channel" — a manager does not reorganize a report
through a throwaway comment in a `peer` channel, and a stranger does not
bind anyone from a `directive` channel.

### E. Directive provenance and the cross-context influence record

For a persona to reason about a directive *later, in another context*,
the directive must be captured with enough provenance to evaluate it. A
directive is a kind of commitment, so it rides on the **commitments
memory class** introduced by [RFC 0021](0021-persona-temporal-awareness.md)
Phases 2–4 (v0.4.0). When fact/commitment extraction processes an
interaction, an instruction directed at the persona is stored with a
**cross-context influence record**:

| Field | Source |
|-------|--------|
| `issuer_id` | the message sender |
| `issuing_channel_id` | the interaction's channel |
| `channel_authority` | the issuing channel's authority level (§D) |
| `issuer_authority` | `directs(issuer, self, scope)` evaluated against the org graph (§C) — `directive` / `request` / `observation` |
| `scope` | the directive's subject, as extracted |
| `status` | `pending` → `accepted` / `adapted` / `refused` (§F) |

The record is computed **at extraction time**, against the org graph as
it stood then — so the persona's later reasoning does not have to
re-derive authority from scratch every turn, and a stored directive
carries a stable, auditable provenance. (Re-evaluation when the org graph
*changes* — a manager is reassigned — is Open Question #2.)

### F. The accept / adapt / refuse decision

When a persona acts and a pending cross-context directive is relevant to
the turn, the directive is **not** applied automatically. It enters the
[RFC 0028](0028-agent-decision-policy-engine.md) **pre-act checkpoint**
as a *candidate-shaping constraint* — RFC 0028's guardrail pipeline
already "applies hard constraints" before scoring candidates; a
cross-context directive is one more constraint input, not a new
checkpoint. The persona's decision policy resolves it to one of three
outcomes:

- **accept** — the issuer has authority over the persona for the
  directive's scope (`issuer_authority = directive`), the directive does
  not conflict with a higher-authority directive, and it violates neither
  RFC 0009 deny-by-default permissions nor the persona's own policy. The
  directive shapes the turn's candidate actions as a hard constraint.
- **adapt** — the issuer has authority, but only over a *narrower* scope
  than the directive claims (a matrix manager directing outside its
  dimension), or the directive partially conflicts with another. The
  persona complies **within** the authorized scope and records why it
  narrowed.
- **refuse** — the issuer lacks authority (`issuer_authority` is
  `request` or `observation` and the persona declines the cooperative
  ask), or the directive loses a conflict to a higher-authority directive
  (§C precedence), or it would violate RFC 0009 permissions or a
  mandatory-HITL class. A refusal is itself a recorded decision with a
  reason — refusing is a first-class, auditable outcome, not a silent
  no-op.

**Conflict resolution.** Two directives that cannot both be satisfied are
ranked by `directs` precedence (§C): the issuer higher on the recipient's
`manages` chain wins; the loser is refused with
`reason=outranked`. An **unresolved tie** — equal-authority issuers, or
peers — does not pick a winner: it produces RFC 0028's `defer` outcome
with `reason=authority_tie`, which routes to escalation (the recipient's
manager) or, where none exists, to the RFC 0028 human-in-the-loop path.

This is the concrete meaning of "a persona reasons about cross-context
influence according to its own policy": the org graph supplies the
*authority facts*, RFC 0028 supplies the *decision machinery*, and the
persona's configured decision policy supplies the *judgement* between
them.

### G. Persona clearance and authority from role

A persona's organizational **role** (§B) carries two ceilings:

- **`clearance`** — the maximum RFC 0037 confidentiality classification
  the persona may hold or be a member of. This closes the question
  RFC 0037 explicitly deferred ("which channels a persona may join").
  A **membership-time clearance check** runs when a persona is added to a
  channel — by config load or at runtime: if the channel's RFC 0037
  classification exceeds the persona's role `clearance`, the membership
  is **rejected** and the rejection is audited. The check fails closed,
  and it surfaces at `make validate` time for static config so an
  operator's mistake is caught before startup, not at runtime.
- **`authority`** — the maximum channel authority level (§D) at which the
  persona's directives are honoured. A `staff-engineer` whose role
  authority is `operational` cannot have a directive treated as
  `directive`-level even if it posts in a `directive` channel; the
  effective level is `min(role authority, channel authority, directs
  result)`.

Roles, not individual personas, carry the ceilings — so an org
reorganization (a persona changes role) updates clearance and authority
in one place, and the society store stays the single source of truth.

### H. The enforced confidentiality-egress gate

RFC 0037 §G ships a **logging-only** leak tripwire: when a persona is
about to publish a message carrying content above the target channel's
classification, an audit event fires but the message is **not** blocked.
RFC 0037 is explicit that promoting this to a *block* is an
organizational decision deferred to this RFC.

This RFC promotes it. The egress check becomes an instance of
RFC 0028 §H's mandatory human-in-the-loop class *"external
communications crossing trust boundaries"*: a publish whose content
protection level (RFC 0037) exceeds the target channel's classification
is **held**, not sent, and routed for approval to the persona's manager
on the org graph (§C) — or, where the topology gives none, to the
RFC 0028 human-in-the-loop path. The hold produces an RFC 0028
`DecisionRecord` with `reason=confidentiality_egress`, a signed,
scoped, expiring approval token gates the release (RFC 0028 Phase 2a /
RFC 0009), and the whole sequence is audited. The persona cannot
self-approve — fail-closed, exactly as RFC 0028 §H requires.

The tripwire's *detection* logic is unchanged from RFC 0037 §G; this RFC
changes only the *response* — from "log it" to "hold it and route it" —
and that change is only possible because the org graph now answers "route
it to whom."

### I. Meeting and negotiation protocols (v0.5.0)

The "Protocols" half of this RFC's reserved scope. A **meeting /
negotiation protocol** is a structured, multi-turn interaction pattern
with explicit phases — *convene → propose → deliberate → decide →
ratify* — bounded by RFC 0020 interaction lifecycle and RFC 0030
governance, and resolved by RFC 0028 Phase 4 collective decisions. Where
this RFC's Phases 1–3 give a persona authority to direct *one* other
persona, Phase 4 gives an organization a structured way to reach a
*group* decision: a chairing role convenes, eligible role-holders submit
proposals, a configured aggregation (RFC 0028 Phase 4) resolves, and the
outcome is ratified and published as an org-level decision record.

Phase 4 is sketched here for architectural continuity and detailed when
v0.5.0 is planned; it is a **non-goal of the v0.4.0 phases** and gated on
RFC 0028 Phase 4 and RFC 0030 Phase 3. Inter-organization federation
(Phase 5) — one organization's personas interacting with another's
across an authority boundary, with cross-org directives always
degrading to `request` — is sketched for v0.5.0+ and depends on RFC 0011
external bridges.

### J. Storage

The organization graph is **society state**, not personal state — it is
shared across every persona in the org and is authoritative. It therefore
lives in the **society store**, the Postgres backend
[RFC 0029](0029-personal-society-storage-split.md) introduces in v0.4.0.
This RFC's Phase 1 depends on RFC 0029's society-store facade being in
place. The cross-context influence record (§E), by contrast, is
*personal* memory — it is *this persona's* record of a directive *it*
received — and lives in the persona's own memory store on the commitments
tier (RFC 0021), carrying the same protection level RFC 0037 §C assigns
any channel-derived memory entry.

## Security Considerations

- **Authority is the defence against semantic cross-context injection.**
  Delimiter-level injection is handled by RFC 0034 / RFC 0036
  sanitization. A plausible-looking *directive* planted in a low-trust
  channel is not a delimiter exploit — the two-factor rule (§D) is what
  stops it: content from an `observed`/`peer` channel, or from an issuer
  with no `directs` relation, can never become a binding directive, so it
  cannot bind the persona in a higher-trust context.
- **Both axes fail closed.** An unknown channel authority level resolves
  to `observed` (no directive weight); an unknown classification resolves
  to `internal` (RFC 0037). An unlabelled or misconfigured channel never
  grants authority or leaks confidentiality by default.
- **The membership-time clearance check is fail-closed and static-first**
  (§G): an over-classified membership is rejected, surfaced at
  `make validate` for static config, and audited at runtime.
- **Refusal is auditable.** Every accept / adapt / refuse is an RFC 0028
  `DecisionRecord`; a refusal carries a structured reason
  (`outranked`, `no_authority`, `authority_tie`, `permission_denied`).
  An organization can be audited for *what its personas declined to do*,
  not only what they did.
- **The egress gate cannot be self-approved.** §H routes a held publish
  to a *different* principal (the manager, or HITL) and gates release on
  a signed, scoped, expiring RFC 0028 approval token. A persona cannot
  approve its own confidentiality egress.
- **The org graph is a high-value target.** Whoever can edit
  `config/organizations.yaml` or the society-store org tables can grant
  authority and clearance. Org-graph mutation is a security-sensitive
  change: it is an RFC 0028 §H mandatory-HITL class (*"high-impact
  organizational actions"*) at runtime, and audited. Static config
  changes inherit the repository's existing review controls.
- **Tie handling never silently picks a winner.** An unresolved authority
  tie (§F) escalates or goes to HITL — it does not default to "accept" or
  to "the most recent directive." A silent default would be an authority
  the system never actually established.
- **Stale provenance is bounded, not eliminated.** A cross-context
  influence record fixes issuer authority at extraction time (§E). If the
  org graph later changes, a stored directive's `issuer_authority` can be
  stale. The bound: it can only become *more* permissive than reality if
  an issuer was *demoted* after issuing — surfaced as Open Question #2,
  with re-evaluation-on-graph-change the proposed resolution.

## Phased Implementation Plan

### Phase 1: The organization model (v0.4.0)

1. `config/organizations.yaml` schema (`schemas/organization.schema.json`)
   — organization, topology, roles, members; `make validate` coverage.
2. `internal/protocols/` (Go) — org graph loader, the `directs` relation
   evaluator (§C), topology validation (no cycles in `hierarchy`, at most
   one manager per node; `scope` labels required on `matrix` edges).
3. Society-store org tables (RFC 0029 society-store facade) and graph
   load at startup.
4. Persona org-membership + role surfaced on the persona definition
   (`config/agents.yaml`, `schemas/agent.schema.json`).
5. The membership-time clearance check (§G) against RFC 0037
   classifications, static (`make validate`) and runtime.

Dependencies: RFC 0011, RFC 0029 (society-store facade), RFC 0037
(classification to check clearance against).

### Phase 2: The authority axis (v0.4.0)

1. Channel `authority` field — `config/channels.yaml`,
   `schemas/channel.schema.json`, the `channels`-table column, and the
   value on dispatched channel events (mirroring RFC 0037 §B).
2. The two-factor directive rule (§D) and the `directs`-backed authority
   computation.
3. The cross-context influence record (§E) on the RFC 0021 commitments
   tier; directive flagging in fact/commitment extraction.

Dependencies: Phase 1, RFC 0021 (commitments tier), RFC 0037 §B
(channel-event classification plumbing the authority field reuses).

### Phase 3: Cross-context influence decisions and the enforced gate (v0.4.0)

1. The accept / adapt / refuse evaluation (§F) wired into the RFC 0028
   pre-act checkpoint as a constraint input; conflict resolution and tie
   escalation.
2. The enforced confidentiality-egress gate (§H): RFC 0037's tripwire
   signal routed through the RFC 0028 mandatory-HITL path with
   manager-based routing.
3. Org-graph mutation as an RFC 0028 §H mandatory-HITL class.

Dependencies: Phase 2, RFC 0028 (Phases 1–2a — checkpoints, guardrails,
HITL approval tokens), RFC 0009 (identity, audit, capability tokens).

### Phase 4: Meeting and negotiation protocols (v0.5.0)

1. Protocol state machine (*convene → propose → deliberate → decide →
   ratify*) over RFC 0020 interactions and RFC 0030 governance.
2. Chairing role, proposal submission, ratification and org-level
   decision records.

Dependencies: RFC 0028 Phase 4 (collective decisions), RFC 0030 Phase 3.

### Phase 5: Inter-organization federation (v0.5.0+)

1. Cross-org authority degradation (cross-org directives always become
   `request`).
2. Federation over RFC 0011 external bridges.

Dependencies: RFC 0011 external bridges, Phase 4.

## Files Touched (Estimated)

| Component | Files | Change |
|-----------|-------|--------|
| Config / schema | `config/organizations.yaml`, `schemas/organization.schema.json` (new) | The organization model (§B) |
| Config / schema | `config/agents.yaml`, `schemas/agent.schema.json` | Persona org-membership + role |
| Config / schema | `config/channels.yaml`, `schemas/channel.schema.json` | Channel `authority` field (§D) |
| Go orchestrator | `internal/protocols/` (new package) | Org graph loader, `directs` evaluator, topology validation |
| Go orchestrator | `internal/channels/sqlite_schema.go` | `channels.authority` column migration |
| Go orchestrator | `internal/channels/` (event dispatch) | `authority` on dispatched channel events |
| Go orchestrator | society store (RFC 0029) | Org-graph tables |
| Go orchestrator | `internal/security/audit_event.go` | Org-mutation, clearance-rejection, egress-hold audit events |
| Protos | `proto/task.proto` | `authority` field on `ChannelMessageEvent` |
| Python agents | `agents/decision/` (RFC 0028) | Cross-context directive as a pre-act constraint input (§F) |
| Python agents | `agents/memory/commitments.py` (RFC 0021) | Cross-context influence record (§E) |
| Python agents | fact/commitment extraction | Directive flagging |
| Python agents | `agents/channel_publisher.py` | Egress gate: route RFC 0037 tripwire signal to HITL (§H) |
| Python agents | `agents/persona.py`, `agents/persona_runtime/` | Org role, clearance, authority on persona state |
| Docs | `docs/guides/`, `docs/diagrams/`, `docs/ai-glossary.md` | Organization model, the two-axis model, glossary entries |
| Tests | `internal/protocols/*_test.go`, `tests/unit/python/`, `tests/integration/` | Per Test Strategy |

## Test Strategy

- **Unit tests**:
  - `directs` for each topology: hierarchy ancestor relation; flat always
    false; matrix per-scope; cycle and multi-manager validation rejects
    bad `hierarchy` graphs.
  - The two-factor directive rule (§D): issuer-authority×channel-authority
    combinations classify as directive / request / observation correctly;
    unknown levels fail closed.
  - Conflict resolution: the higher manager wins; equal-authority issuers
    produce an `authority_tie` defer, not a winner.
  - The membership-time clearance check rejects an over-classified
    membership and accepts a within-clearance one.
- **Integration tests**:
  - A directive from a manager in an `operational` channel is accepted; the
    same instruction from a peer, or from an `observed` channel, is not
    treated as binding.
  - Two conflicting directives: the higher-authority one is applied, the
    other refused with `reason=outranked`.
  - The §H egress gate holds an over-classified publish, routes it to the
    manager, and releases only against a valid approval token.
- **Security tests**: a "directive" planted in a low-authority channel
  cannot bind a persona in a higher-trust context; a persona cannot
  self-approve a held egress; org-graph mutation requires HITL.
- **Manual tests**: a new `MT-ORG-AUTHORITY-001` — an org with a manager
  and two reports; conflicting instructions from the manager and a peer;
  the persona accepts the manager's, refuses the peer's, and escalates a
  manufactured tie.

## Open Questions

1. **Role-defined vs. derived authority levels.** §G makes role
   `authority` an explicit ceiling. Should it instead be *derived* purely
   from topology depth? Proposed resolution: keep it explicit — a flat
   org still wants differentiated authority, which depth cannot express.
2. **Stale provenance on org-graph change.** §E fixes `issuer_authority`
   at extraction time. Proposed resolution: re-evaluate the cross-context
   influence record for any `pending` directive when the org graph
   changes; resolved (`accepted`/`refused`) records are immutable history.
3. **Flat-org conflict resolution.** With `directs` always false, every
   flat-org instruction conflict is an `authority_tie`. Is escalation
   meaningful when there is no manager? Proposed resolution: a flat org
   names a `tiebreak` role in config, or ties go to HITL.
4. **Multi-org membership.** §B allows one org per persona. Cross-org
   personas (a contractor) are deferred to Phase 5 federation; flagged
   here so the Phase 1 schema does not foreclose it.
5. **Channel authority vs. RFC 0037 classification independence.** The
   two axes are modelled as fully independent (§A). Are there channels
   where they must co-vary (e.g. a `secret` channel forced to
   `directive`)? Proposed resolution: keep them independent; an operator
   may always set both, and co-variation as a constraint can be added
   later without a model change.

## Decision / Next Steps

1. Review this RFC alongside
   [RFC 0037](0037-memory-confidentiality-channel-classification.md): the
   two are the confidentiality and authority halves of one two-axis model
   (§A) and should be reviewed together.
2. Confirm the v0.4.0 / v0.5.0 phase split — organizations and authority
   (Phases 1–3) for v0.4.0; meeting/negotiation protocols and federation
   (Phases 4–5) for v0.5.0 — against the ROADMAP, which already reserves
   RFC 0012 across exactly that boundary.
3. Accept the targeted RFC 0028 update (cross-context directive as a
   pre-act constraint input; the egress gate as a mandatory-HITL class;
   the §G reference de-placeholdered) — see RFC 0028's revised Section E
   and Section G.
4. Sequence Phase 1 after RFC 0010 and the RFC 0029 society-store facade,
   per the ROADMAP v0.4.0 dependency chain.
5. Create `docs/rfcs/0012-pr-plan.md` once this RFC is accepted; PR 1
   must add the glossary entries for *organization*, *topology*,
   *authority axis*, *directive*, *cross-context influence record*,
   *clearance*.
6. Regenerate [INDEX.md](INDEX.md) via `make rfcs`.

## Related Documentation

- [RFC 0037 — Memory Confidentiality & Channel Classification](0037-memory-confidentiality-channel-classification.md) — the confidentiality axis; the other half of the two-axis model.
- [RFC 0028 — Agent Decision Policy Engine](0028-agent-decision-policy-engine.md) — the decision machinery the accept/adapt/refuse evaluation and the egress gate run through.
- [RFC 0011 — Channels & Internal Agent Messaging](0011-channels-bridges.md) — the channel surface the authority level annotates.
- [RFC 0009 — Agent Identity, Security & Sandboxing](0009-security-sandboxing.md) — identity tokens, mandatory-HITL gates, capability tokens, audit.
- RFC 0010 — Sub-Agent Spawning (reserved, not yet written) — spawned sub-agents inherit org context.
- [RFC 0021 — Persona Temporal Awareness](0021-persona-temporal-awareness.md) — the commitments memory class the cross-context influence record rides on.
- [RFC 0029 — Personal/Society Storage Split](0029-personal-society-storage-split.md) — the society store the org graph lives in.
- [RFC 0030 — Multi-Agent Conversation Governance](0030-multi-agent-conversation-governance.md) — bounds the meeting/negotiation protocols (Phase 4).
- [Architecture spec](../ai-agents-orchestration-spec.md), [Extension spec](../persatrix-extension-spec.md).
