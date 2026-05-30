# Memory Scope Axes — Discussion Notes

**Status**: 🔨 Draft
**Author**: Maksim Khomutov
**Date**: 2026-05-30
**Target**: reframes [RFC 0031](rfcs/0031-per-session-namespacing-channels.md) (Phase 2 shipped v0.3.5; Phases 3–4 pending); touches [RFC 0042](rfcs/0042-state-namespacing-by-scope.md) vocabulary
**Companion to**: [Memory Quality Roadmap](memory-quality-roadmap.md), [Storage Architecture Roadmap](storage-architecture-roadmap.md), [Agent Runtime Vocabulary Roadmap](agent-runtime-vocabulary-roadmap.md)

---

## Table of Contents

- [Why this doc exists](#why-this-doc-exists)
- [The conflation: one word, four jobs](#the-conflation-one-word-four-jobs)
- [The four-axis model](#the-four-axis-model)
- [Axis detail and rationale](#axis-detail-and-rationale)
  - [Persona — the agent's whole experience](#persona--the-agents-whole-experience)
  - [Session = room — the conversational continuity unit](#session--room--the-conversational-continuity-unit)
  - [Relationship — cross-room, per-individual](#relationship--cross-room-per-individual)
  - [Epoch — the test/run isolation axis](#epoch--the-testrun-isolation-axis)
  - [Principal — tenant ownership and deletability](#principal--tenant-ownership-and-deletability)
- [Where each memory tier rides](#where-each-memory-tier-rides)
- [Decisions taken](#decisions-taken)
- [Consequences for the current code and RFC 0031](#consequences-for-the-current-code-and-rfc-0031)
- [Open follow-ons](#open-follow-ons)
- [Related documentation](#related-documentation)

---

## Why this doc exists

Reviewing the v0.3.5 session-isolation work ([RFC 0031](rfcs/0031-per-session-namespacing-channels.md)) against two edge cases exposed that the word **"session"** is carrying several unrelated meanings at once:

1. **A channel with no human participant** (agent-to-agent traffic). The session unit was specified as `(user, agent, channel)`, but the dispatch path actually resolves `(recipient-agent, channel, sender)` — the third axis is the *sender*, not an authenticated human — so this case works. It only fails on an empty sender, which degrades to the `legacy` snapshot non-fatally.

2. **A room with two+ humans and several agents.** Here the current `(agent, channel, sender)` unit *fragments* a single group conversation: agent X gets one session per distinct speaker, so its memory of one room is split by who talked. This isolates concurrent DMs correctly but breaks shared group context — and a multi-party room is the **v0.5.0 mainline** (Slack/Discord/email bridges), not an edge case.

Pulling on that thread surfaced the real problem: **"session" is being asked to be the test-isolation namespace, the room-continuity unit, the person-continuity unit, and (adjacent to) the tenant boundary — all at once.** Those pull in opposite directions. This doc separates them into orthogonal axes, gives each a distinct name, and records the design decisions taken. It is not an RFC; it is the planning step between the observation and the RFC amendments that act on it.

The grounding principle is the project's memory-quality bar: memory is **the substrate of a continuous relationship with a participant over time** (the [dementia test](memory-quality-roadmap.md#quality-bar--the-dementia-test)), not a searchable log. The axes below are chosen so that continuity is the default and isolation is the explicit, named exception — never the other way around.

## The conflation: one word, four jobs

"Session" today fuses four genuinely distinct concepts, plus two adjacent ones it keeps colliding with:

| # | Meaning hiding in "session" | What it actually wants to be keyed on |
|---|------------------------------|----------------------------------------|
| 1 | **Run/test isolation** — "this rerun is a fresh world" (the F-3 motivation) | a disposable epoch the operator rotates |
| 2 | **Room continuity** — the ongoing conversation in one channel | `(agent, channel)` |
| 3 | **Person continuity** — trust/opinion/knowledge about an individual | `(agent, participant)`, crossing rooms |
| 4 | **Tenant ownership** — whose data, who may delete it | `principal_id` (RFC 0039) |

Adjacent, already-named, frequently confused with the above:

- **`chat_session_id`** (RFC 0016) — the per-chat-conversation UUID. Already renamed once to dodge this exact collision.
- **`scope`** (RFC 0020 §G) — the conversational-boundary unit *within* a room (`dm:a:b`, `group:planning`, `thread:<id>`).

The fragmentation bug in edge case 2 is the direct symptom of #2 and #3 fighting: concurrency isolation wants the *finest* grain (per-sender), room continuity wants the *natural* unit (the whole room). They were collapsed into one key, and the fine grain won — incorrectly, for group rooms.

## The four-axis model

The resolution is to stop overloading one identifier and name four orthogonal axes (with the persona itself as the substrate they all sit inside):

| Axis | Question it answers | Key | Crosses rooms? | Resets when |
|------|---------------------|-----|----------------|-------------|
| **Persona** | which agent's mind is this? | per-agent `memory.db` | — | never (short of deleting the persona) |
| **Session = room** | which conversation? | `(agent, channel)` | no — explicit cross-room recall only | a new channel is used |
| **Relationship** | who is this person, to me? | `(agent, participant[, participant_type])` | **yes** | epoch / principal only |
| **Epoch** | which world — test or live? | `epoch_id` (default `live`) | n/a | operator bumps it |
| **Principal** | whose data — and may it be deleted? | `principal_id` (default `local`) | n/a | tenant boundary |

The two collisions stay distinct: `chat_session_id` (RFC 0016) and `scope` (RFC 0020 §G) keep their existing meanings and are *not* renamed.

## Axis detail and rationale

### Persona — the agent's whole experience

The persona's continuous existence is **not** a session concept. Its whole memory — every room, every relationship — is the persona being alive over time; physically, the per-agent `memory.db`. A "new session" must never mean "a new persona with fresh experience." Persona identity is the substrate the other axes scope *within*; it is never reset by session, epoch, or recall semantics.

### Session = room — the conversational continuity unit

**A session is one room's ongoing memory, keyed `(agent, channel)`, isolated by default, accumulating across runs and restarts.** Each distinct channel is a distinct, isolated conversational memory. The sender drops out as a *scoping* axis — the speaker is a *participant inside* the shared room memory, not a key that splits it.

This makes the v0.5.0 multi-party room coherent: one room = one shared episodic memory per agent, with every speaker's turns visible to that agent within it. Two separate DM threads are already two distinct channel ids (`dm:a:b` vs `dm:c:b`), so `(agent, channel)` keeps them apart without a sender axis — the sender axis only ever changed the group case, which is the case it got wrong.

Reaching across rooms ("what did I discuss with Alice in the other channel?") is an **explicit, opt-in recall path**, not an automatic scope — exactly what [RFC 0031 §D](rfcs/0031-per-session-namespacing-channels.md#d-recall-semantics)'s `sessions=[…]` / `sessions="*"` already provides. Session-scoped *default* recall (Phase 2) is therefore correct under this model; what was wrong was the *framing* of session as a per-run isolation namespace and the *sender* in its key.

### Relationship — cross-room, per-individual

Relationship is **trust + opinion about an individual** (user or persona), accumulated from interactions in *any* room. It attaches to the person, not the venue, so it crosses rooms. This validates — rather than contradicts — the existing schema, where `relationships` deliberately keeps `session_id` out of its primary key ([RFC 0031 §C amendment](rfcs/0031-per-session-namespacing-channels.md#c-storage-model)): the aggregate row is cross-session by design.

Three properties this axis must carry:

- **Per-individual, fed from group rooms too.** In a group room the persona still updates its individual relationship with each speaker; those writes fire in the group room but feed the same cross-room per-participant record. The current key `(participant_id, participant_type, other_participant_id, other_participant_type)` already supports this.
- **Group-as-entity (future).** A group is just another `participant_type`, so "relationship with the group" is `(agent, group:planning-as-participant)` — a new participant type, not a new axis. Useful because behavior in a group differs from behavior one-on-one.
- **Contextual facets (future).** "Alice one-on-one ≠ Alice in a group" means relationship is layered: a cross-room **core** (overall trust/opinion) plus room-scoped **behavioral facets** (how Alice acts in room X). Only the core exists today; the facet layer is named now so the core does not get silently overloaded into it later.

### Epoch — the test/run isolation axis

**F-3 ("a rerun must not inherit the prior run's state") no longer belongs to "session."** Once session means continuity, it accumulates and never auto-resets — so isolation needs its own axis.

The decisive constraint: **F-3's bleed spans both room-scoped memory (episodes) and person-scoped memory (relationship, person-facts).** Any isolation mechanism must reset *both*. That eliminates the cheap option of "use a fresh channel name per test" — a fresh room name does not reset relationship or person-facts, because those are keyed on the participant and cross rooms. The persona would still surface old trust and old opinions about a reused `--user`, which is precisely the F-3 symptom.

So isolation is a dedicated orthogonal namespace — **`epoch_id`**, default `live`:

- **Keyed on every tier, including `relationships` in its primary key** (mirroring how `principal_id` was put in the relationship PK — otherwise an `ON CONFLICT` write bleeds trust across epochs while a residual filter masks it).
- **Strict equality, no carve-out.** Unlike the session `legacy` carve-out (which exists *for* continuity), a fresh epoch must see **nothing** — that is the entire point.
- **Same task-local rail** as session/principal: a `contextvars` binding + a gRPC metadata header; the orchestrator resolves it at boot from `PERSATRIX_EPOCH` (default `live`). Prod never changes it; CI bumps it per job.
- **Coexisting worlds.** Two epochs live side-by-side in one store, so CI continuity tests and cross-room recall tests stay authorable — the property `make reset` destroys by wiping the volume.

`make reset` is **kept as the documented nuclear option** (wipe everything); epoch is the everyday, logical-branch tool.

Rejected alternative — **reusing `principal_id` for test isolation** (test = synthetic tenant): tempting because the machinery exists, but it re-commits the exact "one identifier, many meanings" sin this doc exists to fix. Tenancy carries deletion and consent semantics that do not map to "disposable test world," and in real multi-tenant prod the principal is already meaningful and unavailable to borrow.

### Principal — tenant ownership and deletability

The principal/tenant axis (RFC 0039, currently armed-but-unfed at `local`) answers *whose data this is and whether it may be deleted*. Its purpose is **separation for easy deletion** (GDPR-style erasure): tenant data is kept separable so it can be dropped on request. A refinement to record: **with tenant consent, the *experience derived* from their data may be retained in anonymized form** — decoupled from the tenant identity and folded into the persona's general experience — rather than deleted with the raw data. Raw, tenant-attributed data stays deletable; consented, anonymized learning persists. The mechanism for that split is future work (intersects RFC 0013 erasure and RFC 0039 accounts).

## Where each memory tier rides

The key insight for the declarative tiers: **scope follows the fact's subject, not a uniform rule.**

| Tier | Rides on | Crosses rooms? | Note |
|------|----------|----------------|------|
| **Episodes** (narrative) | session `(agent, channel)` | no (explicit recall only) | the room's conversational record |
| **Facts — person subject** (`Alice's daughter is Mira`) | participant `(agent, subject)` | **yes** | knowledge about a person travels with the person, like trust |
| **Facts — topic/room subject** (`this channel shipped Friday`) | session `(agent, channel)` | no | belongs to the room |
| **Relationship** (trust/opinion) | participant `(agent, participant)` | **yes** | cross-room core (+ future facets) |
| **Notes** (agent-authored) | session by default; subject-scoped if about a person | mixed | follows the same subject rule as facts |

This resolves the earlier flip-flop on facts: the right answer is neither "all facts are room-scoped" nor "all facts are person-scoped" — it is subject-dependent. Narrative is room-bound; knowledge and affect *about people* are person-bound. This is also what keeps the dementia test passing across rooms without making every room leak into every other.

Everything above sits inside an `(epoch, principal)` pair: cross-room recall and cross-room relationships range over *rooms*, never across *epochs* or *principals*.

## Decisions taken

These were settled in the design discussion that produced this doc:

1. **Session = room-continuity**, keyed `(agent, channel)`; it accumulates and is not a per-run isolation namespace. (Confirmed.)
2. **Drop the sender axis** from the session unit: `(agent, channel, sender)` → `(agent, channel)`. (Confirmed; the sender's per-conversation isolation rationale only held for DMs, which the channel axis already separates.)
3. **Relationship is cross-room**, per-individual, fed from any room; group-as-`participant_type` and contextual facets are future extensions. (Confirmed.)
4. **Fact scope follows subject**: person-subject facts are person-scoped (cross-room); topic-subject facts stay room-scoped. (Confirmed.)
5. **Add a dedicated `epoch` axis** for test/run isolation: default `live`, strict equality, no carve-out, in the `relationships` PK, on the same task-local + header rail as session/principal. Keep `make reset` as the nuke. Reject overloading `principal`. (Confirmed.)
6. **Principal stays the tenant/deletion axis**; with consent, derived experience may be retained anonymized rather than deleted with raw data. (Confirmed; mechanism deferred.)

## Consequences for the current code and RFC 0031

Tracked as three follow-up issues:

- **[ISSUE-0083](issues/ISSUE-0083-session-binding-sender-axis-fragments-multiparty-rooms.md) — drop the sender axis.** `internal/channels/session_binding.go` / `internal/channels/grpc_dispatcher.go:219`: the `(agent, channel, user/sender)` triple becomes `(agent, channel)`. `ErrEmptySessionAxis` loses the user axis; the no-sender edge case stops being special. A behavior change to a v0.3.5-shipped path, so it needs its own PR + the multi-party-room test that the current `grpc_dispatcher_session_test.go` asserts the *opposite* of (two senders → distinct sessions becomes two senders → one room session). This is the load-bearing prerequisite for the rest.
- **[ISSUE-0084](issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md) — fact scope by subject.** Recall/write gains a subject classification (person vs topic) to choose person-scoped (cross-room) vs room-scoped keying. A precise change to the facts tier, not a rewrite; refines the blanket Phase-2 session-scoping in §D.
- **[ISSUE-0085](issues/ISSUE-0085-epoch-axis-run-isolation.md) — add the epoch axis.** A new migration adding `epoch_id` across the persona-memory tiers (and the Go channel store), structurally modeled on the `principal_id` migration (v11), as the real home of F-3 test/run isolation. Sequenced after ISSUE-0083.

Plus two doc-level reconciliations (no issue — handled in the amendments):

- **RFC 0031** — the `(agent, channel, user)` unit (§B amendment) is superseded; session is redefined from "isolation namespace" to "room continuity"; F-3 isolation moves to the epoch axis; facts gain subject-dependent scope. Recorded in the [§A amendment](rfcs/0031-per-session-namespacing-channels.md#a-vocabulary).
- **RFC 0042 reconciliation** — its proposed `session:` / `channel:` state-scope prefixes must agree with "session = room"; if session and channel are the same axis for memory, the state-namespacing vocabulary should not present them as two independent scopes without saying how they relate.

## Open follow-ons

- **Cross-room recall surface** — the operator/persona-facing path for "look across my rooms" (RFC 0031 §D `sessions=[…]/"*"`) needs a deliberate UX once it is a first-class persona capability rather than a debug flag.
- **Relationship contextual facets** — the room-scoped behavioral-observation layer beneath the cross-room core.
- **Group-as-participant** — relationship and addressing for a group entity (intersects RFC 0011 / RFC 0012).
- **Consent-gated anonymized retention** — the principal-axis split between deletable raw data and retained anonymized experience (intersects RFC 0013 / RFC 0039).
- **Agent-global maintenance sweeps** — eviction / retention / janitor already skip the principal filter; the epoch filter will have the same gap and the same deferral.

## Related documentation

- [RFC 0031 — Per-Session Namespacing for Channels and Persona Memory](rfcs/0031-per-session-namespacing-channels.md) — the RFC this doc reframes
- [RFC 0020 — Interaction Lifecycle (§G scope)](rfcs/0020-interaction-lifecycle.md#g-per-channel-scoping) — the `scope` axis kept distinct from session
- [RFC 0016 — Human Participant & Chat Interface](rfcs/0016-human-participant-chat-interface.md) — `chat_session_id`, the adjacent collision
- [RFC 0026 — Declarative Facts Tier](rfcs/0026-declarative-facts-tier.md) — the facts tier whose scope becomes subject-dependent
- [RFC 0039 — User Accounts & Authentication](rfcs/0039-user-accounts-authentication.md) — the principal/tenant axis source
- [RFC 0042 — State Namespacing by Scope Prefix](rfcs/0042-state-namespacing-by-scope.md) — `session:` / `channel:` state scopes to reconcile
- [Memory Quality Roadmap](memory-quality-roadmap.md) — the dementia-test quality bar this model serves
- [Storage Architecture Roadmap](storage-architecture-roadmap.md) — physical-storage companion
- [Agent Runtime Vocabulary Roadmap](agent-runtime-vocabulary-roadmap.md) — runtime-seam companion (Seam 3 scope prefixes)
- [MT-MEMORY-005 — Dementia test](manual-tests/MT-MEMORY-005-dementia-test.md) — the cross-room continuity acceptance surface
