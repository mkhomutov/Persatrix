# Manual Test MT-MEMORY-MULTIUSER-001: One persona, many people — the tenant boundary live

**Test ID**: `MT-MEMORY-MULTIUSER-001`
**Feature Area**: Memory scope axes (the tenant/principal axis — ISSUE-0081 storage + ISSUE-0082 Part 2 emission)
**Version**: 1.0
**Created**: 2026-08-06
**Last Updated**: 2026-08-06
**Status**: Active — **authored at v0.3.14 PR 2; live execution is a v0.3.14 release-prep deliverable** (Phase 3 of [the plan](../v0.3.14-plan.md#phase-3--release-prep-execution)).

---

## Overview

**Purpose**: Verify the v0.3.14 promise live — **under `auth.mode: enabled`, one persona serving two authenticated people keeps their memory apart**. The arc: A tells the persona something private → B asks and the persona does not have it → A's own continuity is intact → the emitted principal values are read off storage for both turns → the pre-activation `'local'` corpus is unreachable → `auth.mode: disabled` is unchanged.

Every axis before this one isolated *rooms* (session) or *runs* (epoch). Neither can isolate **people**: the cross-room tiers travel by design — facts are cross-room by default since [RFC 0049](../rfcs/0049-memory-consolidation-gradient.md) Phase 1, and the relationship/person-identity record is keyed on the participant with `session_id` deliberately out of its primary key. So before this release, two accounts talking to one persona shared one memory. That is what this MT closes.

**Scope**: [ISSUE-0082](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) Part 2 (the emission half) against the [ISSUE-0081](../issues/ISSUE-0081-session-id-process-global-not-task-local.md) storage half, over the [RFC 0039 §F](../rfcs/0039-user-accounts-authentication.md) verified claim.

**Out of Scope** — deferred, **not asserted** here:

- **Per-principal capacity sweeps / quota** — episode TTL, size-cap eviction, procedural decay and note prune stay agent-global (a named Known Gap, v0.4.0). Recall stays principal-filtered either way, so this MT's absence bars are unaffected.
- **The auth substrate itself** — [MT-AUTH-001](MT-AUTH-001.md) owns bootstrap, the §E matrix, the browser session; it re-runs beside this MT as the substrate regression.
- **Concurrent sessions for two accounts** — see the two-account note below.
- **The two stated residuals** ([ISSUE-0082](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) R-1 / R-2), neither of which this MT can reach: it drives `persatrix chat` against a **single** persona, so there is no second speaker for the close-time summary to aggregate (R-1) and no agent-to-agent cascade across the orchestrator hop (R-2). Both need a multi-agent group-channel MT; that is a v0.4.0 deliverable, not a gap in this arc. Do not read a green run here as evidence about either.

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

> ⚠️ **Every orchestrator restart empties the agent registry — restart the personas after it, before publishing.** The registry is in-memory and agents register once at their *own* startup; they do **not** re-register when the orchestrator comes back. Both the Leg 1 and Leg 3 restarts below therefore leave a healthy-looking stack in which every dispatch is dropped with `channels: dispatch target not registered` — the persona never replies, and the leg produces no rows to read. Nothing in the output says the run was void. After **any** orchestrator restart, restart the persona containers (`docker compose restart agent-<id>` for each) and confirm `GET /api/v1/agents` lists every persona `healthy` **before** sending the leg's message. Verified 2026-08-07 on a live arc, where it silently consumed the whole run before it was noticed.

> **Two accounts, one bootstrap verb.** RFC 0039 Phase 3 (account administration) is v0.4.0, so the only shipped account-creation verb is `account bootstrap`, and it refuses to run once any account exists. To get a second *authenticated principal* on shipped verbs, **delete `data/accounts.db` and bootstrap again under the other participant** between legs. `accounts.db` and the persona's `memory.db` are separate stores, so this rotates *who is speaking* while leaving the memory corpus untouched — which is exactly the variable under test. The cost is that the two accounts are sequential rather than concurrent; the *concurrent* case is pinned deterministically in `test_principal_emission_isolation.py` (one process, two scopes, one shared room). When Phase 3 lands, this dance collapses to one `account create`.

> **`--user` stops mattering under `enabled`.** The §F claim replaces any body `user_id` with the caller's verified participant, so the CLI's `--user` no longer selects the peer — the logged-in account does. Passing it is harmless; do not read it as the identity.

---

## Test Procedure

### Leg 1 — Alice speaks (the private disclosure)

```bash
rm -f data/accounts.db
./bin/persatrix-server account bootstrap --username alice --participant alice-person
# restart the orchestrator so it opens the new accounts.db
# then restart the personas too — the restart emptied the agent registry (see the warning above)
persatrix login          # as alice
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

**Verification**:
- [ ] The reply references **Mira** / "your daughter" by recall — the trigger shares no keyword with Leg 1. Strictness must not narrow recall *within* a tenant.

### Leg 3 — Bob asks the same thing (the absence bar)

```bash
rm -f data/accounts.db
./bin/persatrix-server account bootstrap --username bob --participant bob-person
# restart the orchestrator, then restart the personas (registry emptied — see the warning above)
persatrix login          # as bob
echo "What would be a good present for a kid that age?" | ./bin/persatrix chat ember-owl
```

**Verification**:
- [ ] The reply makes **no** reference to Mira, to a daughter, or to a seven-year-old. It asks for context or answers generically. *A reference here is the defect this release closes.*
- [ ] `SELECT principal_id, COUNT(*) FROM episodes GROUP BY principal_id;` shows **both** `alice-person` and `bob-person` rows coexisting — isolation is a recall filter, not a delete.
- [ ] Same query against `facts` and `relationships`: alice's and bob's rows are distinct. The relationship row is the sharp one — its primary key omits `session_id`, so before this release Bob's turn would have read Alice's trust and person-facts.

> **Record the values.** Paste the `principal_id` group-by output for `episodes`, `facts` and `relationships` into the execution report. A green absence bar with every row still reading `local` proves nothing except that recall is empty; the report must show the two principals actually landed.

### Leg 4 — Alice returns, unchanged

Re-bootstrap as alice (`--participant alice-person`) and repeat the Leg 2 trigger.

**Verification**:
- [ ] Mira surfaces again. Bob's intervening turn neither erased nor polluted Alice's tenant.

### Leg 5 — The activation-day reset (accepted, and stated)

Migration v11 backfilled every pre-existing row to `'local'` and the predicate is strict equality with **no** carve-out, so a deployment that ran `auth.mode: enabled` before this release finds its accumulated memory unreachable the day emission lands. Bridging it would *be* the cross-tenant bridge the boundary forbids, so it is observed rather than fixed.

**Action**: with the stack down, tag one distinctive episode as pre-activation in the persona's `memory.db`:

```sql
UPDATE episodes SET principal_id='local' WHERE summary LIKE '%Mira%';
```

Bring the stack up, log in as alice, repeat the Leg 2 trigger.

**Verification**:
- [ ] The persona no longer surfaces Mira — the `local` row is invisible to `alice-person`.
- [ ] The row is still **present** in storage (`SELECT principal_id FROM episodes …`) — a partition, not a deletion. This is the operator remedy: the corpus is reachable by running single-tenant, or by re-tagging.
- [ ] The release notes and Known Gaps carry this statement before the tag.

### Leg 6 — `auth.mode: disabled` is byte-identical

Set `auth.mode: disabled`, restart, and chat with no credential.

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
2. **Shared-room semantics — and the bound on them.** Two authenticated people in one *group channel* get per-speaker persona memory **for each turn's own write**: neither turn recalls the other's disclosures. That is the promise applied inside a room, not a regression; room continuity is unaffected (transcript and verbatim history are not principal-scoped). It does **not** extend to the close-derived aggregate or to a persona's relayed reply — the two residuals listed under Out of Scope above ([ISSUE-0082](../issues/ISSUE-0082-orchestrator-per-request-session-principal-emission.md) R-1 / R-2), neither of which this MT can reach. The release notes must state the promise at **that** scope, not the wider one.
3. **Agent-origin turns stay `local`.** The persona fleet holds no accounts and drives the publish/convene REST seams anonymously (RFC 0039 §Non-Goals), so a persona's own reply emits no principal by design. Expect `local` rows from autonomous and agent-authored traffic even under `enabled`.
4. **Provenance.** `PERSATRIX_MEMORY_PROVENANCE=1` is a weaker instrument here than the storage read: per [ISSUE-0122](../issues/ISSUE-0122-relationship-tier-emits-no-provenance.md) the `relationship` tier charges the budget without calling `record_admission`, so provenance is silent for exactly the cross-room identity read Leg 3 cares about. Read `principal_id` off the tables.

---

## Sign-off

- [ ] All six legs pass on a live provider.
- [ ] The `principal_id` group-by output for all three tiers is pasted into the execution report.
- [ ] [MT-AUTH-001](MT-AUTH-001.md) re-run green as the auth-substrate regression.
