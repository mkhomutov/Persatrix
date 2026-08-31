# Manual Test MT-MEMORY-GROUP-TENANT-001: The tenant boundary in a multi-agent room

**Test ID**: `MT-MEMORY-GROUP-TENANT-001`
**Feature Area**: Memory scope axes — the tenant/principal axis across the *aggregate* and *relayed* writes ([ISSUE-0082](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) residuals R-1 / R-2)
**Version**: 1.1
**Created**: 2026-08-06
**Last Updated**: 2026-08-23
**Status**: Active — **authored with the R-1/R-2 designs. Runnable in both directions**: run it against v0.3.14 to *evidence* the residuals, and re-run it after [ISSUE-0123](../issues/ISSUE-0123-per-speaker-interaction-scope.md) + [ISSUE-0124](../issues/ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md) land as the **v0.3.15** gate.

---

## Overview

**Purpose**: [MT-MEMORY-MULTIUSER-001](MT-MEMORY-MULTIUSER-001.md) drives
`persatrix chat` against **one** persona, so it can reach neither
residual — a DM has no second speaker for the close to aggregate (R-1)
and no agent-to-agent cascade across the orchestrator hop (R-2). This MT
supplies the missing shape: **several personas in one group channel,
with an authenticated human speaking into it.**

It observes two things the per-turn boundary does not cover:

- **R-2 — the relayed write.** A persona's reply re-enters as an
  unauthenticated REST publish, so every cascade hop below it writes
  under `'local'` even though the whole exchange descends from an
  authenticated person's publish.
- **R-1 — the derived write.** The interaction record is keyed on the
  *room*, so its close-time summary and extracted facts aggregate every
  speaker and land under whichever principal closed it.

**Every leg carries two expected columns** — `v0.3.14` (the residual,
what a correct run shows *today*) and `after R-1+R-2`. A leg that
matches the `v0.3.14` column is a **pass** on a pre-fix run: it is the
evidence the residual is real rather than inferred from code reading.

**Scope**: ISSUE-0082 R-1 / R-2. **Out of scope**: the per-turn boundary
itself (MT-MEMORY-MULTIUSER-001), the auth substrate
([MT-AUTH-001](MT-AUTH-001.md)), per-principal capacity sweeps.

---

## Related Documentation

- [ISSUE-0123](../issues/ISSUE-0123-per-speaker-interaction-scope.md) — R-1 design (freeze the principal on the record, key the tracker `(principal, scope)`).
- [ISSUE-0124](../issues/ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md) — R-2 design (server-side causal attribution per `(channel, agent)`).
- [MT-MEMORY-MULTIUSER-001](MT-MEMORY-MULTIUSER-001.md) — the per-turn boundary; [MT-MEMORY-CROSSROOM-001](MT-MEMORY-CROSSROOM-001.md) — the cross-room travel this exploits; [MT-CHANNEL-GOV-002](MT-CHANNEL-GOV-002.md) — the governance knobs driven here.
- [docs/memory-scope-axes.md](../memory-scope-axes.md) · [RFC 0020 §G](../rfcs/0020-interaction-lifecycle.md) · [RFC 0049](../rfcs/0049-memory-consolidation-gradient.md) Phase 1.

---

## Preconditions

1. `make build-orchestrator build-cli`; a live provider
   (`ANTHROPIC_API_KEY`) — the personas must produce real replies and
   land real rows. `make demo-anthropic`, `export
   PERSATRIX_SERVER=http://127.0.0.1:8080`.
2. `make reset` first — prior rows mask results.
3. `auth.mode: enabled` in `config/security.yaml`; no `data/accounts.db`
   (Leg 0 bootstraps it).
4. The stock three-persona rooms from `config/channels.yaml`:
   `group:planning` (ember-owl, iron-fox, nova-sparrow; chair
   nova-sparrow; `end_vote_threshold: 2` / `end_vote_window: 3`) and
   `group:roundtable` as the second room for the travel check.
5. `interaction_idle_timeout_seconds` raised on `planning` (e.g. 1800)
   so no leg closes an interaction by accident before Leg 4 asks for it.

> **Pace the arc, do not sit in it.** The end-vote quorum is counted over
> a 600s / W=3 window. Read every leg first and drive Legs 1–4 from one
> script; an idle pause mid-arc expires the window and silently changes
> which close trigger fires, which is the variable Legs 4 and 5 turn on.

> **The personas re-register themselves — but give them the moment.** Through
> v0.3.14 an orchestrator restart emptied the in-memory registry for good, and
> this arc's `account bootstrap` → restart step left a healthy, green-looking
> stack in which every dispatch was dropped, no persona ever replied, and the
> run produced no cascade and no rows. It cost a full live arc on 2026-08-07
> before it was spotted. Since v0.3.15 each agent watches its own orchestrator
> connection and re-registers when it returns
> ([ISSUE-0125](../issues/ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md)),
> so no `docker compose restart agent-<each>` is needed. What still applies:
> **wait for the orchestrator to answer `/healthz` before publishing**, and if a
> leg does go quiet, `GET /api/v1/agents` remains the check — an orchestrator
> holding **zero** registered agents now says so at ERROR in its own log rather
> than only in one dispatch WARN per dropped message. The RFC 0009 rate-limiter
> bucket is *not* flushed by a restart either, so the first turn after one can
> still draw `429` for ~60 s.

> **One account at a time.** Same constraint as MT-MEMORY-MULTIUSER-001:
> `account bootstrap` refuses once an account exists, so rotating to a
> second principal means deleting `data/accounts.db`, bootstrapping
> again and restarting the orchestrator. That restart rotates the wire
> interaction id, which **structurally closes** the open records — so a
> single record holding two *humans'* turns is not reachable on shipped
> verbs. It is not needed: with one human, the room's other speakers are
> the personas, whose turns are `'local'`, and `'local'`-vs-`alice-person`
> is the same strict-equality boundary. The two-human aggregate is pinned
> deterministically instead (see Sign-off).

---

## Test Procedure

### Leg 0 — Alice, authenticated

```bash
rm -f data/accounts.db
./bin/persatrix-server account bootstrap --username alice --participant alice-person
# restart the orchestrator so it opens the new accounts.db
persatrix login          # as alice
```

### Leg 1 — Alice discloses into the room, and a cascade runs

```bash
./bin/persatrix channel send planning \
  "Before we plan Q3 — I'll be out the week of the 14th, my daughter Mira has surgery. @ember-owl can you take the review slot?"
```

Wait for the round to settle (a reply from ember-owl, then at least one
further hop — iron-fox or nova-sparrow responding to *ember-owl*, not to
Alice).

**Verification**:
- [ ] At least two hops occurred: `./bin/persatrix channel history planning` shows a persona message that replies to another persona's message.
- [ ] In **each** persona's `memory.db`: `SELECT principal_id, COUNT(*) FROM episodes GROUP BY principal_id;` returns at least one `alice-person` row. *(If every row reads `local`, emission is not reaching the personas at all — stop and diagnose here, or every later leg passes vacuously.)*

### Leg 2 — R-2: the relayed write

Every turn in this interaction descends causally from one authenticated
publish, so under a correct boundary every dispatch it produced would
carry the tenant.

> **Read the wire, NOT storage — storage cannot see this.** Verified
> 2026-08-07: a run with **9 tenant-less dispatches** stored **zero**
> `local` rows. Nothing is written per turn; the only write is at close,
> and the close-time aggregate takes one principal for the whole record,
> so R-1 silently re-attributes the relayed turns to whoever closed. A
> `principal_id` group-by reads clean and concludes wrongly. This leg was
> originally written against storage and was corrected.

**Two preconditions**, or the leg passes vacuously:

1. **`floor_control: false` on the channel.** Under floor control,
   `ChannelRouter.Publish`'s deferred-fanout skip recognises a floor-turn
   reply and suppresses its re-fanout, so agent publishes never reach
   `Dispatch` and you will see zero tenant-less hops. (Clearing it needs
   `escalation_chair_id` cleared in the *same* PATCH — the chair requires
   floor control.)
2. **Raise the collector's tail sampling.** `config/observability/otel-collector.yaml`
   samples healthy traces at **1%**; the dispatch spans are dropped
   otherwise. Set `sampling_percentage: 100` for the run and revert after.

Then read the `principal.id` attribute off `channel.dispatch` spans:

```bash
curl -s "http://localhost:16686/api/traces?service=persatrix-server&operation=channel.dispatch&limit=300&lookback=20m"
# for each span: recipient.agent_id, channel.message_id, principal.id (absent == tenant lost)
```

| | Expected |
|---|---|
| **v0.3.14** | Dispatches from **alice's** publishes carry `principal.id=alice-person`; dispatches from **persona** publishes carry **none**. Reference run: 6 with, **9 without**, in one interaction. |
| **after R-1+R-2** | **Zero** tenant-less dispatches in the causal chain. Attribution rides it, bounded by `cascade_depth`. |

- [ ] The per-dispatch table (time, recipient, message id, principal) is pasted into the execution report — a bare count is not enough; the *grouping by originating message* is the finding.

- [ ] Row counts recorded per persona, per `principal_id`, into the execution report.

### Leg 3 — Close the interaction from a *persona* publish

Drive the room to an end-vote quorum (two personas signalling
end-of-interaction inside the W=3 window) — the natural continuation of
Leg 1 usually reaches it; if not, prompt for wrap-up:

```bash
./bin/persatrix channel send planning "Anything else before we wrap?"
```

- [ ] Orchestrator logs show `interaction_closed{…end_votes}` (or
  `synthesis_reply`) — **not** `structural` / `cost`. The trigger matters:
  both descend from the chair persona's own unauthenticated publish, so
  they carry no principal.

### Leg 4 — R-1: the derived write lands in the shared tenant

Read the close-derived episode (`turn_count > 1`) and the facts extracted
from it.

```sql
SELECT principal_id, speaker_id, turn_count, scope, substr(summary,1,200)
  FROM episodes WHERE turn_count > 1;
SELECT principal_id, speaker_id, subject, predicate, object
  FROM facts ORDER BY asserted_at DESC LIMIT 20;
```

| | Expected |
|---|---|
| **v0.3.14** | One record per persona per room, `principal_id='local'`, whose summary narrates **Alice's** disclosure — her content written into the shared tenant, and simultaneously **out of her own reach** (strict equality: `alice-person` cannot read `local`). Facts about Mira carry `principal_id='local'`. |
| **after R-1+R-2** | Alice's turns form their **own** record under `alice-person`. Agent-origin turns that are not causally hers form separate `local` records — **one per agent speaker**, not one shared agent record (Phase 0b: the tracker keys `(principal, speaker, scope)`). No record mixes two speakers or two principals. |

- [ ] With three personas in the room, Leg 4 yields **three** `local` `turn_count > 1` rows, not one. A single merged `local` row means the speaker dimension of the key did not land — the exact defect [ISSUE-0131](../issues/ISSUE-0131-derived-memory-has-no-speaker-attribution.md) names, and the one plain Option A would have shipped.

- [ ] The `(principal_id, speaker_id, summary)` **triple** of every `turn_count > 1` row is pasted into the execution report. *The triple is the finding — no value alone proves anything, and a pair proves only the tenant half: with every persona sharing the `local` principal, `speaker_id` is the ONLY column that distinguishes [ISSUE-0131](../issues/ISSUE-0131-derived-memory-has-no-speaker-attribution.md)'s three records from one row written three times.*

### Leg 5 — The travel: shared-tenant content reaches another room

Facts are cross-room by default (RFC 0049 Phase 1) and **every**
agent-origin turn resolves `'local'`, so the Leg 4 rows are readable from
any room the fleet is in.

```bash
./bin/persatrix channel convene roundtable "Draft the Q3 review rota."
```

| | Expected |
|---|---|
| **v0.3.14** | A persona in `roundtable` surfaces Alice's absence / Mira — content she disclosed in `planning`, now spoken in a room whose audience she never chose. |
| **after R-1+R-2** | No reference. The content is under `alice-person`, and an agent-origin turn resolves `local`. |

- [ ] The `roundtable` transcript is pasted verbatim into the report.

### Leg 6 — The other close trigger, the other direction

Re-run Legs 1–3 but close by **crossing the bound** (raise the traffic
until `max_rounds` fires, or lower it) so the close descends from
*Alice's own publish* and carries her principal.

| | Expected |
|---|---|
| **v0.3.14** | The `turn_count > 1` record lands under `alice-person` — and its summary narrates the **personas'** contributions too. The aggregate crosses the boundary in the opposite direction; which way it falls is decided by the close trigger. This is the asymmetry in [`internal/channels/synthesis_close.go`](../../internal/channels/synthesis_close.go), observed. |
| **after R-1+R-2** | Identical to Leg 4's post-fix result. The record binds its **own** frozen principal, so the close trigger no longer selects a tenant. |

### Leg 7 — Bob: the absence bar

```bash
rm -f data/accounts.db
./bin/persatrix-server account bootstrap --username bob --participant bob-person
# restart the orchestrator; persatrix login as bob
./bin/persatrix channel send planning "Who's covering the review slot this month?"
```

**Verification**:
- [ ] The reply makes no reference to Mira or to surgery **on the post-fix run**.
- [ ] On the **v0.3.14** run, record the outcome either way: a reference here is R-1/R-2 reaching a *second human*, and is the strongest single piece of evidence the report can carry.
- [ ] `SELECT principal_id, COUNT(*) FROM episodes GROUP BY principal_id;` shows `alice-person`, `bob-person` and `local` coexisting — isolation is a recall filter, not a delete.

### Leg 8 — `auth.mode: disabled` — unchanged on the principal axis, **not** on the speaker axis

Set `auth.mode: disabled`, restart, repeat Leg 1 with no credential.

- [ ] Every new row is `principal_id='local'`.
- [ ] No `principal.id` span attribute on any dispatch — the header is absent, not empty.
- [ ] **Record count is NOT unchanged from v0.3.14.** Phase 0b splits on speaker regardless of auth mode, so a room of N personas now closes N records where v0.3.14 closed one. This leg was originally titled "byte-identical"; that claim held for Option A alone and does not survive Phase 0b. The two checks above still hold — the *principal* axis is genuinely unchanged under `disabled` — but the row count is not, and this is the deployment where the reserve multiplier bites hardest, since every speaker shares the one `local` principal.

### Leg 9 — The replayed write: a restart derives nothing twice

The [ISSUE-0130](../issues/ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md)
shape-(b) leg. With Legs 1–4's traffic in the room, snapshot **both**
partitions, restart the **agents**, wait for catch-up, and snapshot again
— sending **no new traffic** in between, or the comparison measures the
traffic instead.

**Restore `auth.mode: enabled` and re-authenticate first.** Leg 8 turns
it off and does not turn it back on, and its re-run of Leg 1 leaves the
newest traffic — the part of the window catch-up actually re-reads —
stamped `local`. Run this leg with `alice-person` traffic newest, or the
partition it tells you to read first is the one nothing replays, and it
goes flat for a reason that has nothing to do with the guard.

```sql
SELECT principal_id, speaker_id, COUNT(*) FROM episodes GROUP BY 1, 2;
SELECT principal_id, speaker_id, COUNT(*) FROM facts GROUP BY 1, 2;
-- Replay-derived rows are identifiable on their own: the shape-(b) span
-- identity is written as the interaction id, prefixed `replay-`.
SELECT interaction_id, principal_id, speaker_id FROM episodes
 WHERE interaction_id LIKE 'replay-%';
```

```bash
# The AGENTS, not the orchestrator: catch-up replay runs once, from
# `AgentServer.start()`. The ISSUE-0125 re-registration watcher
# deliberately re-runs only `_self_register`, and compose agents are
# dependents of the orchestrator rather than the reverse — so
# `restart orchestrator` replays nothing and this leg would pass with
# the guard deleted.
docker compose restart agent-ember-owl agent-iron-fox agent-nova-sparrow
sleep 20   # then wait for each agent's /healthz
```

Take **three** snapshots — before the restart (**A**), after it (**B**),
and after a second restart with no traffic in between (**C**).

| | Expected |
|---|---|
| **v0.3.14** | A = B = C, for the wrong reason: the leak-stopper derives nothing at all from a replayed span, so a restart adds no rows and also recovers no memory of the window the agent was down for. |
| **after B1+B2** | **A → B grows; B = C.** Legs 1–4's traffic derived LIVE, under `uuid4` ids, and the guard matches only a previous *replay* derivation — so the first restart has nothing to match and derives the window once. The bar is the SECOND restart: `B = C`, in every partition. |

- [ ] Snapshots **B and C** are pasted verbatim, per `(principal_id, speaker_id)`, and are **identical**. Do not expect `A = B`: that bar fails on correct code, and "correcting" it to accept whatever appears destroys the gate.
- [ ] `A → B` adds `replay-` rows, in **`alice-person`**. Zero growth means replay derived nothing — the v0.3.14 cost, not a pass.
- [ ] Read `alice-person` first. This issue originally framed the missing check as a `local` read, which was right while every replayed derivation went there; once (b) lands, a re-derived duplicate appears in the **attributed** partition, so a `local`-only assertion passes at exactly the moment the regression is worst.
- [ ] Now send one message and restart a third time. New rows **must** appear, and at least one **must** carry a `replay-` interaction id: the guard bounds duplication, and a leg that only proves nothing was written cannot tell that apart from replay having stopped deriving at all. Counting rows alone does not settle it — the live close writes rows too, with no replay involved, so the `replay-` prefix is what makes this check discriminating.
- [ ] ISSUE-0125's live proof is **Leg 8's** orchestrator restart, not this one (`docker compose restart orchestrator`, then `GET /api/v1/agents` non-empty). Keep the two apart: this leg needs the agents restarted, which is exactly what that check forbids, and running them as one leg is how the shape-(b) guard ended up with no live proof at all.

---

## Expected Results Summary

| Leg | Property | v0.3.14 | after R-1+R-2 |
|-----|----------|---------|---------------|
| 1 | Emission reaches the room | `alice-person` rows present | same |
| 2 | R-2 — relayed write | ≥1 `local` row in the causal chain | zero |
| 3 | Close trigger is persona-origin | `end_votes` / `synthesis_reply` | same |
| 4 | R-1 — derived write | aggregate under `local`, narrates Alice | per-principal **and per-speaker** records |
| 5 | Travel | Alice's content spoken in `roundtable` | no reference |
| 6 | Asymmetry | aggregate under `alice-person`, narrates personas | same as Leg 4 |
| 7 | Second human | reference = the leak, recorded | no reference |
| 8 | `disabled` | `local`, no header, one record | `local`, no header, **N records** (speaker split is auth-independent) |
| 9 | Replay across a restart | flat (nothing derives from a replay) | flat **in every partition** (replay derives, once) |

---

## Edge Cases and Notes

1. **A quiet room proves nothing.** Legs 5 and 7 are absence bars, and
   absence is also what a persona with no cascade produces. Leg 1's
   two-hop check and Leg 2's row counts are the guards: if no cascade
   ran, the arc never exercised R-2 — re-run with a higher
   `max_cascade_depth` rather than recording a pass.
2. **The close trigger is the variable, not noise.** Legs 4 and 6 differ
   only in which path closed the interaction. Confirm the trigger in the
   logs before reading either result; a mis-identified trigger inverts
   the expected column.
3. **Read the tables, not the provenance.** Per
   [ISSUE-0122](../issues/ISSUE-0122-relationship-tier-emits-no-provenance.md)
   the `relationship` tier charges the RFC 0017 budget without calling
   `record_admission`, so `PERSATRIX_MEMORY_PROVENANCE=1` is silent for
   exactly the cross-room identity read Leg 5 turns on.
4. **MT-MEMORY-MULTIUSER-001 Edge Case 2** reads that two people in one
   group channel "get per-speaker persona memory". That was true for
   per-turn writes only, and this note used to say *narrow* it. R-1 made
   the claim true for the close-derived aggregate as well, so the edit is
   the opposite one — **widen** it back to the now-true claim. Owned by
   [residuals PR 5](../issues/ISSUE-0082-residuals-pr-plan.md); corrected
   here so an executor does not make the withdrawn edit.

---

## Sign-off

- [ ] All **ten** legs (0–9) run on a live provider, with the expected column used (`v0.3.14` or post-fix) stated in the report. *This line read "eight" while the procedure held nine; the restart leg makes ten.*
- [ ] Leg 2's per-dispatch `principal.id` table — **the** Leg 2 finding, since storage cannot see R-2 — and Leg 4's `(principal_id, speaker_id, summary)` **triples** pasted verbatim.
- [ ] Leg 9's two row-count snapshots pasted verbatim, per `(principal_id, speaker_id)`, in **both** partitions.
- [ ] The two-human aggregate — unreachable on shipped verbs — is pinned deterministically by the R-1 unit gate (one tracker, two principals, one room scope, two records) rather than asserted from this arc.
- [ ] MT-MEMORY-MULTIUSER-001 re-run green as the per-turn regression.
