# Manual Test MT-MEMORY-MULTIUSER-001: One persona, many people — the tenant boundary live

**Test ID**: `MT-MEMORY-MULTIUSER-001`
**Feature Area**: Memory scope axes (the tenant/principal axis — ISSUE-0081 storage + ISSUE-0082 Part 2 emission)
**Version**: 1.1
**Created**: 2026-08-06
**Last Updated**: 2026-08-18
**Status**: Active — **executed live at v0.3.14 release-prep PR 1** ([execution report](v0.3.14-execution-report.md)); v1.1 folds in that run's F-3 (the fresh-room rule below) and F-4 (the operator notes) so a re-run does not repeat them.

---

## Overview

**Purpose**: Verify the v0.3.14 promise live — **under `auth.mode: enabled`, one persona serving two authenticated people keeps their memory apart**. The arc: A tells the persona something private → B asks and the persona does not have it → A's own continuity is intact → the emitted principal values are read off storage for both turns → the pre-activation `'local'` corpus is unreachable → `auth.mode: disabled` is unchanged.

Every axis before this one isolated *rooms* (session) or *runs* (epoch). Neither can isolate **people**: the cross-room tiers travel by design — facts are cross-room by default since [RFC 0049](../rfcs/0049-memory-consolidation-gradient.md) Phase 1, and the relationship/person-identity record is keyed on the participant with `session_id` deliberately out of its primary key. So before this release, two accounts talking to one persona shared one memory. That is what this MT closes.

**Scope**: [ISSUE-0082](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) Part 2 (the emission half) against the [ISSUE-0081](../issues/ISSUE-0081-session-id-process-global-not-task-local.md) storage half, over the [RFC 0039 §F](../rfcs/0039-user-accounts-authentication.md) verified claim.

**Out of Scope** — deferred, **not asserted** here:

- **Per-principal capacity sweeps / quota** — episode TTL, size-cap eviction, procedural decay and note prune stay agent-global (a named Known Gap, v0.4.0). Recall stays principal-filtered either way, so this MT's absence bars are unaffected.
- **The auth substrate itself** — [MT-AUTH-001](MT-AUTH-001.md) owns bootstrap, the §E matrix, the browser session; it re-runs beside this MT as the substrate regression.
- **Concurrent sessions for two accounts** — see the two-account note below.
- **The two stated residuals** ([ISSUE-0082](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) R-1 / R-2), neither of which this MT can reach: it drives `persatrix chat` against a **single** persona, so there is no second speaker for the close-time summary to aggregate (R-1) and no agent-to-agent cascade across the orchestrator hop (R-2). Both are owned by [MT-MEMORY-GROUP-TENANT-001](MT-MEMORY-GROUP-TENANT-001.md) (designs: [ISSUE-0123](../issues/ISSUE-0123-per-speaker-interaction-scope.md) / [ISSUE-0124](../issues/ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md)), a v0.3.15 deliverable — not a gap in this arc. Do not read a green run here as evidence about either.

---

## Related Documentation

- [docs/memory-scope-axes.md](../memory-scope-axes.md) — the three axes and what each isolates.
- [ISSUE-0081](../issues/ISSUE-0081-session-id-process-global-not-task-local.md) — the storage half (migration v11, strict-equality recall, no carve-out).
- [Accounts & Auth guide](../guides/auth.md) · [RFC 0039 §F](../rfcs/0039-user-accounts-authentication.md) — the verified `participant_id` this axis is keyed on.
- [MT-SESSION-003](MT-SESSION-003.md) — the isolation-MT shape this mirrors; [MT-MEMORY-CROSSROOM-001](MT-MEMORY-CROSSROOM-001.md) — the cross-room travel this bounds; [MT-EPOCH-001](MT-EPOCH-001.md) — the run axis.

**Related Automated Tests** — the deterministic backbone:

- `tests/integration/test_principal_emission_isolation.py` — the wire pin (header → bound scope) + the two-principal tenant gate + the activation-day reset.
- `internal/server/principal_producer_test.go` — the producer: the §F participant reaches the dispatch context, two accounts dispatch under two principals, unauthenticated and `disabled` emit nothing.
- `internal/server/principal_route_table_test.go` — the origin enumeration: an unclassified route fails the build.
- `internal/channels/grpc_dispatcher_principal_test.go` — the wire header itself; `internal/channels/synthesis_close_principal_test.go` — the timer-path context reset.

---

## Preconditions

1. Fresh build: `make build-orchestrator build-cli` (`make ui` if using the console).
2. A live persona stack with a real provider (`ANTHROPIC_API_KEY`) — the persona must return real replies and land real rows:
   ```bash
   make demo-anthropic
   export PERSATRIX_SERVER=http://127.0.0.1:8080
   ```
3. **A clean memory surface** (`make reset`, then bring the stack up) so prior rows cannot mask a result.
4. **No `data/accounts.db`** — Leg 1 bootstraps it.
5. `config/security.yaml` with `auth.mode: enabled` for Legs 1–5; Leg 6 flips it back.

> ⚠️ **Every orchestrator restart empties the agent registry — re-register the personas before the next turn.** The registry is in-memory and agents register once at their *own* startup; they do **not** re-register when the orchestrator comes back. **Four** restarts below leave it empty: Legs 1, 3 and 4 (each re-bootstrap needs one) and Leg 6. Leg 5 takes the whole stack down and up, so it re-registers on its own.
>
> - **What you will see here.** `persatrix chat` resolves the agent against the registry *before* it publishes, so an empty registry answers `404 agent not found`. The leg fails **loudly** — a burnt turn, not a false PASS. On the channel-publish seam the same condition *is* silent (publish returns `201`, dispatch dropped with `channels: dispatch target not registered`, persona never replies) — not this MT's path, but one config change away.
> - **Remedy.** Wait for the orchestrator to answer `/healthz`, **then** restart each persona container (`docker compose restart agent-<id>`). Registration is best-effort and never retried, so a persona restarted against a still-booting orchestrator lands in the same empty state.
> - **Verify before sending the leg's message.** `GET $PERSATRIX_SERVER/api/v1/agents` must list every persona `healthy`. The route is `authenticated`, so under `enabled` (Legs 1–5) run it **after** `persatrix login` with the credential — an un-credentialled call answers `401`, which is not an empty registry.
> - **Long-standing, not new** ([ISSUE-0125](../issues/ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md); recorded since [v0.3.0](v0.3.0-execution-report.md)). The same restarts leave the RFC 0009 rate-limiter bucket un-flushed, so a post-restart turn can draw `429` for ~60 s.

> **Two accounts, one bootstrap verb.** RFC 0039 Phase 3 (account administration) is v0.4.0, so the only shipped account-creation verb is `account bootstrap`, and it refuses to run once any account exists. To get a second *authenticated principal* on shipped verbs, **delete `data/accounts.db` and bootstrap again under the other participant** between legs. `accounts.db` and the persona's `memory.db` are separate stores, so this rotates *who is speaking* while leaving the memory corpus untouched — which is exactly the variable under test. The cost is that the two accounts are sequential rather than concurrent; the *concurrent* case is pinned deterministically in `test_principal_emission_isolation.py` (one process, two scopes, one shared room). When Phase 3 lands, this dance collapses to one `account create`.

> **`--user` stops mattering under `enabled`.** The §F claim replaces any body `user_id` with the caller's verified participant, so the CLI's `--user` no longer selects the peer — the logged-in account does. Passing it is harmless; do not read it as the identity.

> ⚠️ **The fresh-room rule — a recall leg must be re-asked in an empty-transcript room** (v1.1, from the [PR 1 run's F-3](v0.3.14-execution-report.md#findings--follow-ups)). **Legs 4 and 5 are not valid inside Alice's populated DM.** The RFC 0034 conversation window is rebuilt from *channel history* at catch-up and is **not** principal-scoped, so the room transcript can answer the trigger whether or not memory did. The first live run made this unmistakable: at Leg 5, with **zero** rows readable by `alice-person`, the persona still named Mira — off the transcript. This is the tenant-axis twin of the trap [MT-MEMORY-CROSSROOM-001](MT-MEMORY-CROSSROOM-001.md) hit at its Leg 2b.
>
> - **Leg 3 is exempt and stays the release-critical bar.** Bob's DM is a *distinct* channel with an empty transcript **and** a distinct principal, so its absence result is unconfounded by construction. The rule applies only to the legs that assert **recall** (4) or **non-recall of one's own prior corpus** (5).
> - **`--user` is not a route to a fresh room.** It was tried (`--user alice-cleanroom`) and correctly ignored: under `enabled` the §F claim pins the DM to the verified participant, so the re-ask landed in the same room with the same transcript. Do not reach for it.
> - **The route that works — delete the DM channel between the write leg and the recall leg.** `DELETE /api/v1/channels/{id}` (`operator` role; the bootstrapped account has it) drops the channel and its transcript; the next `persatrix chat` re-creates it empty via `GetOrCreateDM`, under the **same** account and therefore the same principal. Crucially, **`channels.db` and the persona's `memory.db` are separate stores**, so this clears the room without touching the memory corpus under test — the same separation argument the two-account `accounts.db` rotation rides on. Resolve the id first (`GET /api/v1/channels`; the DM is `dm:<participant>:<agent>`), and restart the persona afterwards so its conversation window is rebuilt from the now-empty history rather than from its in-process cache.
> - **What the leg proves once the room is empty.** The persona's own memory is then the only possible source of the answer — which is the property Legs 4 and 5 exist to assert, and the reason a populated-room pass cannot be recorded as one.

---

## Test Procedure

### Leg 1 — Alice speaks (the private disclosure)

```bash
rm -f data/accounts.db
./bin/persatrix-server account bootstrap --username alice --participant alice-person
# restart the orchestrator so it opens the new accounts.db
# then, once /healthz answers, restart the personas — the restart emptied the agent registry (see above)
persatrix login          # as alice
# confirm every persona is back: GET /api/v1/agents with the login token
echo "My daughter Mira turns seven next month." | ./bin/persatrix chat ember-owl
```

Let the interaction close (the RFC 0020 idle window, or a lowered `interaction_idle_timeout_sec`) so the episode and any facts persist.

**Verification**:
- [ ] The reply is a natural acknowledgement.
- [ ] In the persona container: `SELECT DISTINCT principal_id FROM episodes;` includes **`alice-person`** — not `local`. *(If it reads `local`, emission is not reaching the persona: stop and diagnose here, because every later leg would pass vacuously.)*

### Leg 2 — Alice's own continuity holds

```bash
echo "What would be a good present for a kid that age?" | ./bin/persatrix chat ember-owl
```

Let this interaction close too, before moving on. RFC 0020 §C holds open interactions **in memory only**, so the persona restart at the top of Leg 3 discards one that is still open — Leg 4 still reads back off Leg 1's rows, but this leg's episode would never be written.

**Verification**:
- [ ] The reply references **Mira** / "your daughter" by recall — the trigger shares no keyword with Leg 1. Strictness must not narrow recall *within* a tenant.

### Leg 3 — Bob asks the same thing (the absence bar)

```bash
rm -f data/accounts.db
./bin/persatrix-server account bootstrap --username bob --participant bob-person
# restart the orchestrator, then (once /healthz answers) the personas — registry emptied, see above
persatrix login          # as bob
# confirm every persona is back: GET /api/v1/agents with the login token
echo "What would be a good present for a kid that age?" | ./bin/persatrix chat ember-owl
```

**Verification**:
- [ ] The reply makes **no** reference to Mira, to a daughter, or to a seven-year-old. It asks for context or answers generically. *A reference here is the defect this release closes.*
- [ ] `SELECT principal_id, COUNT(*) FROM episodes GROUP BY principal_id;` shows **both** `alice-person` and `bob-person` rows coexisting — isolation is a recall filter, not a delete.
- [ ] Same query against `facts` and `relationships`: alice's and bob's rows are distinct. The relationship row is the sharp one — its primary key omits `session_id`, so before this release Bob's turn would have read Alice's trust and person-facts.

> **Record the values.** Paste the `principal_id` group-by output for `episodes`, `facts` and `relationships` into the execution report. A green absence bar with every row still reading `local` proves nothing except that recall is empty; the report must show the two principals actually landed.

### Leg 4 — Alice returns, unchanged

Re-bootstrap as alice (`--participant alice-person`) — the same `rm` → `bootstrap` → orchestrator restart → **persona restart** dance as Leg 3 — **then apply the fresh-room rule**: delete Alice's DM channel so the re-ask cannot be served off the transcript, and let the next chat re-create it empty.

```bash
# resolve and drop Alice's DM (operator role; the bootstrapped account has it)
curl -s -H "Authorization: Bearer $TOKEN" $PERSATRIX_SERVER/api/v1/channels | grep -o 'dm:alice-person:[a-z-]*'
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" $PERSATRIX_SERVER/api/v1/channels/dm:alice-person:ember-owl
# restart the persona so its conversation window rebuilds from the now-empty history
docker compose restart agent-ember-owl
```

Then repeat the Leg 2 trigger in the re-created room.

**Verification**:
- [ ] The room is empty before the trigger — `GET /api/v1/channels/<dm>/messages` returns no prior turns. Without this the leg is confounded and **must not** be recorded as a pass (see the fresh-room rule).
- [ ] Mira surfaces again **from memory alone**. Bob's intervening turn neither erased nor polluted Alice's tenant.

### Leg 5 — The activation-day reset (accepted, and stated)

Migration v11 backfilled every pre-existing row to `'local'` and the predicate is strict equality with **no** carve-out, so a deployment that ran `auth.mode: enabled` before this release finds its accumulated memory unreachable the day emission lands. Bridging it would *be* the cross-tenant bridge the boundary forbids, so it is observed rather than fixed.

**Action**: with the stack down, tag the pre-activation corpus in the persona's `memory.db`. **Scope the retag on `principal_id`, across every tier** — not on content, and not on `episodes` alone (the single-table `LIKE '%Mira%'` SQL this MT shipped at v1.0 is not sufficient; the PR 1 run had to widen it live). A content predicate misses rows holding the disclosure under another subject — the PR 1 corpus had `subject='mira' predicate='has_age'` — and the bar below is a **count** bar, so one missed row fails the leg. Migration v11 put `principal_id` on all five tiers; `notes` carries the `contact:` identity row:

```sql
UPDATE episodes     SET principal_id='local' WHERE principal_id='alice-person';
UPDATE facts        SET principal_id='local' WHERE principal_id='alice-person';
UPDATE notes        SET principal_id='local' WHERE principal_id='alice-person';
UPDATE interactions SET principal_id='local' WHERE principal_id='alice-person';
-- relationships: `principal_id` is IN this tier's PRIMARY KEY, so the flip
-- collides whenever a `local` twin of the participant tuple already exists
-- (the config-seeded row). DELETE the alice-person row instead of retagging
-- it. Same end state for this leg: nothing readable.
DELETE FROM relationships WHERE principal_id='alice-person';
```

Bring the stack up, log in as alice, and **apply the fresh-room rule** (delete the DM, restart the persona) before repeating the Leg 2 trigger — otherwise the transcript answers and the leg proves nothing about the reset.

**Verification**:
- [ ] The room is empty before the trigger. **This leg is why the rule exists**: the first live run skipped it and the persona named Mira with zero readable rows, off the transcript alone.
- [ ] Readable-by-`alice-person` counts are **zero on every retagged tier** — record the three the report tabulates (`episodes 0 · facts 0 · relationships 0`), and check `notes`: a row left on the identity-capture surface answers the trigger from memory.
- [ ] The rows are still **present** under `local` (`SELECT principal_id, count(*) FROM episodes GROUP BY 1 …`) — a partition, not a deletion. This is the operator remedy: the corpus is reachable by running single-tenant, or by re-tagging.
- [ ] The persona no longer surfaces Mira in the empty room — the `local` rows are invisible to `alice-person`.
- [ ] The release notes and Known Gaps carry this statement before the tag — **at the right scope**: the reset partitions *memory*, and a live room's transcript keeps serving recent content, so "the persona forgot everything" overstates it.

### Leg 6 — `auth.mode: disabled` is byte-identical

Set `auth.mode: disabled`, restart the orchestrator **and then the personas** (see the warning above), and chat with no credential.

**Verification**:
- [ ] The turn succeeds with no login.
- [ ] New rows land under `principal_id='local'` — no header is emitted, so the persona resolves its default exactly as before v0.3.14.
- [ ] Orchestrator logs/traces show **no** `principal.id` span attribute on the dispatch (the header is absent, not empty).

---

## Expected Results Summary

| Leg | Property | Pass |
|-----|----------|------|
| 1 | Alice's turn lands under `alice-person` | Rows tagged with the verified participant, not `local` |
| 2 | Within-tenant continuity | Mira surfaces on a keyword-free trigger |
| 3 | Cross-tenant absence | Bob gets nothing of Alice's, across all tiers; both principals present in storage |
| 4 | No collateral damage | Alice's arc still reads back after Bob's turn |
| 5 | Activation-day reset | Pre-activation `local` rows unreachable, still stored |
| 6 | `disabled` no-delta | `local` everywhere, no header, no span attribute |

---

## Edge Cases and Notes

1. **A green leg that never exercised the surface is not proof.** Legs 3 and 6 are absence bars, and absence is also what a broken persona produces. Leg 1's storage check is the guard: if it does not show `alice-person`, nothing downstream means anything.
2. **Shared-room semantics — and the bound on them.** Two authenticated people in one *group channel* get per-speaker persona memory **for each turn's own write**: neither turn recalls the other's disclosures. That is the promise applied inside a room, not a regression; room continuity is unaffected (transcript and verbatim history are not principal-scoped). It does **not** extend to the close-derived aggregate or to a persona's relayed reply — [ISSUE-0123](../issues/ISSUE-0123-per-speaker-interaction-scope.md) / [ISSUE-0124](../issues/ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md), neither of which this MT can reach; both are observed by [MT-MEMORY-GROUP-TENANT-001](MT-MEMORY-GROUP-TENANT-001.md). The release notes must state the promise at **that** scope, not the wider one.
3. **Agent-origin turns stay `local`.** The persona fleet holds no accounts and drives the publish/convene REST seams anonymously (RFC 0039 §Non-Goals), so a persona's own reply emits no principal by design. Expect `local` rows from autonomous and agent-authored traffic even under `enabled`.
4. **Provenance.** `PERSATRIX_MEMORY_PROVENANCE=1` is a weaker instrument here than the storage read: per [ISSUE-0122](../issues/ISSUE-0122-relationship-tier-emits-no-provenance.md) the `relationship` tier charges the budget without calling `record_admission`, so provenance is silent for exactly the cross-room identity read Leg 3 cares about. Read `principal_id` off the tables.
5. **Rotating `accounts.db` — remove all three files, not just the database** (v1.1, [F-4](v0.3.14-execution-report.md#findings--follow-ups)). `rm data/accounts.db` alone leaves `accounts.db-wal` and `accounts.db-shm` beside a fresh empty database, and the next bootstrap fails with `accounts: read user_version: disk I/O error (522)` — a confusing failure for an operator mid-rotation. Use `rm -f data/accounts.db data/accounts.db-wal data/accounts.db-shm` at every rotation point (Legs 1, 3 and 4).
6. **Run the SQLite work in-container, with the stack up** (v1.1, F-4). `docker cp` against a **stopped** container fails in this stack on the read-only config mount, so the Leg 5 retag cannot be staged that way. Exec into the running persona instead — and note the image ships **no `sqlite3` CLI**, so the queries ride the agent runtime's `python3`:
   ```bash
   docker exec persatrix-agent-ember-owl-1 python3 -c \
     "import sqlite3;d=sqlite3.connect('/data/memory.db');print(d.execute('SELECT principal_id,count(*) FROM episodes GROUP BY 1').fetchall())"
   ```
7. **A dispatch span attribute is not a log line, and a missing span is not evidence** (v1.1, from the PR 1 Leg 6 run). `principal.id` is set **only** as an OTEL span attribute and is never written to orchestrator stdout, so grepping `docker logs` for it returns zero under `enabled` too — a check that cannot fail is not a check. The collector compounds this: its `tail_sampling` policies keep errors, slow (≥ 5 s) and workflow traces and sample the healthy remainder at **1 %**, so an absent span is more likely a sampling artifact than a result. Run Leg 6 with `sampling_percentage` temporarily at **100** and execute **both arms back to back**, so the `disabled` and `enabled` spans are directly comparable and the absence is an observation about the header rather than about the sampler.

---

## Sign-off

- [ ] All six legs pass on a live provider.
- [ ] The `principal_id` group-by output for all three tiers is pasted into the execution report.
- [ ] **Legs 4 and 5 were re-asked in an empty-transcript room** (the fresh-room rule), and the leg records the room was empty before the trigger.
- [ ] Leg 6 was run against a positive control at 100 % sampling — both arms, not an unmatched absence.
- [ ] [MT-AUTH-001](MT-AUTH-001.md) re-run green as the auth-substrate regression.
