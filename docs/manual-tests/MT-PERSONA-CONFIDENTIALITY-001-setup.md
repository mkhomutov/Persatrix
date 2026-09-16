# MT-PERSONA-CONFIDENTIALITY-001 — setup and preconditions

**Owner**: [MT-PERSONA-CONFIDENTIALITY-001](MT-PERSONA-CONFIDENTIALITY-001.md) — this is
that MT's Preconditions section, split out at **v1.2** when the six corrections from
its first five-leg live execution ([v0.3.16 report](v0.3.16-execution-report.md#mt-corrections--for-the-v12-header-bump-at-pr-2))
pushed the combined doc past the 3 000-word cap (the MT stood at 2 997 with three
words of headroom). Splitting rather than trimming follows the
[GROUP-TENANT precedent](MT-MEMORY-GROUP-TENANT-001-setup.md): these are setup
*contract*, and the ones marked **(v1.2)** were each learned by a leg failing —
or passing vacuously — without them.

**Read this before spending the arc.** The driver
(`scripts/manual_tests/mt_persona_confidentiality_001.py`, dry-run by default)
applies the run knobs, checks every gate below before a cent is spent, and
reverts the knobs after; the procedure in the MT is what it executes.

---

## Preconditions


1. The compose stack is up against a **real provider** (`make demo-anthropic` or equivalent — *not* the mock; the withhold/projection distinction needs real replies).
2. A **clean store** (`make reset`, or a fresh `PERSATRIX_EPOCH` for the whole arc) so prior facts do not steer the run.
3. A **`restricted` group channel** exists alongside the bundled `internal` one. Add to `config/channels.yaml` (all four lattice levels are declarable since v0.3.12 — see the classification note in that file) and restart the orchestrator:

   ```yaml
   - name: warroom
     description: "MT-PERSONA-CONFIDENTIALITY-001 — restricted room"
     classification: restricted
     members:
       - id: ember-owl
         respond: addressed
       - id: alex
         respond: observer
   ```

4. `agent-ember-owl` is up and a member of both `group:warroom` and `group:planning` (bundled at `respond: addressed` — the triggers @-mention it).
5. **The operator identity is a member of both rooms** — the publish path refuses a non-member sender (`403 sender is not a member of the channel`). Declare `alex` in both YAML blocks alongside ember-owl (item 3's block already does for `warroom`) — **not** `persatrix channel join` at runtime, per item 9.
6. `persatrix` CLI on `PATH` pointed at the running orchestrator.
7. **For Leg 5 only** — **`alice`** (the DM's other party, and Leg 5's asker) and a second human identity, `bob`, are **members of `group:planning`**, and `bob` is in nothing else in this arc (declare both in the `planning` block — never a runtime join, per item 9). Bob never speaks: audience is the room's *member set*, not who is talking. Item 5's `alex` cannot stand in for Alice — the publish path refuses a non-member sender, and Leg 5 turns on Alice's own tenant. Also confirm `memory.egress.audience` resolves `live` (the shipped v0.3.16 default since PR A3 — verify no overlay pins `shadow`/`off`) and note which, because it selects Leg 5's pass criterion.
8. Optional but recommended: `PERSATRIX_MEMORY_PROVENANCE=1` on the persona container, so a leg fail can be split into a **gate withhold** (fact absent from the admitted `facts` slice) vs. a **reasoning miss** — the MQ-11 discipline [MT-MEMORY-005 §Telemetry](MT-MEMORY-005-dementia-test.md#telemetry-required-for-diagnosis) established.

9. **(v1.2) Membership is declared, never joined.** Add `alex` to both rooms and
   `alice` + `bob` to `planning` in `config/channels.yaml` rather than
   `channel join` at runtime: this arc restarts the orchestrator **four times**
   (two auth flips, two account rotations), and a config-declared channel whose
   store membership has diverged **FATALs** the strict reconcile on the next
   restart. There is no safe runtime-join variant of this arc: the first
   restart after a join FATALs, and Leg 5's auth flips alone need two.
10. **(v1.2) The chat REPL, not `chat send`.** There is no `chat send` verb:
    `persatrix chat <agent> --user <id>` is a REPL that reads stdin line by line,
    so one piped line is one turn and EOF ends the session. Under
    `auth.mode: enabled` the verified claim **replaces** `--user`
    ([RFC 0039 §F](../rfcs/0039-user-accounts-authentication.md)), so bootstrap the
    account with participant `alice` — in-container, on the accounts volume
    (`/var/lib/persatrix/accounts.db`), removing `-wal`/`-shm` sidecars with the
    database when rotating — exactly as the
    [GROUP-TENANT setup](MT-MEMORY-GROUP-TENANT-001-setup.md) describes. **Then log
    the CLI in as Alice** (`printf 'PW\n' | persatrix login --username alice`,
    after `/healthz` answers) before 5a: `chat` and `channel send` carry the
    CLI's stored session. Without one both 401; with one left over from
    Legs 1–4 the claim is `alex`, Alice never asks, and Leg 5 is vacuous. Under
    `disabled` (5d) the session is ignored.
11. **(v1.2) After every orchestrator restart, wait for re-registration** before
    reading any gate or sending a turn: the fleet re-registers itself
    ([ISSUE-0125](../issues/ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md)'s
    watcher), not instantly. Poll `GET /api/v1/agents` until it lists `ember-owl`
    again; a registry read a second after the auth flip saw zero agents, and a
    dispatch into an empty registry is dropped on a green-looking stack.
12. **(v1.2) The persona container at `--log-level DEBUG`**, with
    `PERSATRIX_MEMORY_PROVENANCE=1` (item 8 promoted from optional): the
    per-entry `candidates` array of the `audience egress` record — Leg 5's
    evidence obligation 1 — rides only at `DEBUG`. The stock compose files pass
    no `--log-level`; use a compose overlay named through `COMPOSE_FILE` rather
    than editing the checked-in file. Container log lines are JSON with the text
    under `message`, and timestamps are UTC.
13. **(v1.2) Build before up.** `docker compose up --build` from cold exceeds
    the 900 s the driver waits; split `docker compose build` from `up`. The
    orchestrator image builds on `golang:1.26-alpine` since
    [#954](https://github.com/mkhomutov/Persatrix/pull/954) — on the v0.3.16 RC
    tip before it, every `make demo-*` stack was unbootable (`go.mod requires
    go >= 1.26.0`).
14. **(v1.2) Do not lower the interaction idle timeout.** Real idle-close timing
    is part of what this MT measures; every close waits the full ≥ 11 min gap at
    the 600 s default — Leg 1 once, Leg 5b once per auth mode. The close fires
    on the *next* event after the gap, so a host that sleeps mid-gap loses
    nothing.

---

## Related automated tests

The deterministic CI backbone of the MT (its Overview pointed here at v1.2):

- [`tests/integration/test_confidentiality_gate.py`](../../tests/integration/test_confidentiality_gate.py) — learn-`restricted` → act-`public` withheld / act-`restricted` verbatim / tick zero-admission / the §B single-channel-turn guard.
- [`tests/integration/test_confidentiality_projection.py`](../../tests/integration/test_confidentiality_projection.py) — the §E path: a `public`-acting turn *informed by* a `restricted` memory via its projection.
- [`tests/integration/test_confidentiality_tripwire.py`](../../tests/integration/test_confidentiality_tripwire.py) — the §G tripwire through the real event loop + executor.
- [`tests/integration/test_interaction_classification_capture.py`](../../tests/integration/test_interaction_classification_capture.py) — the §C wire→capture→stamp seam, live and catch-up-replay producers.
- [`EVAL-MEMORY-004`](../../evaluators/eval_sets/EVAL-MEMORY-004.yaml) + [`tests/integration/test_confidentiality_seed_replay.py`](../../tests/integration/test_confidentiality_seed_replay.py) — the RFC 0044 golden: the gate pinned at the request-hash level in both directions on every CI run.
- [`tests/unit/python/test_recall_tool_audience.py`](../../tests/unit/python/test_recall_tool_audience.py) + [`internal/channels/sqlite_search_audience_test.go`](../../internal/channels/sqlite_search_audience_test.go) — the §F audience clause, both sides of the wire (ISSUE-0158).
- [`EVAL-MEMORY-005`](../../evaluators/eval_sets/EVAL-MEMORY-005.yaml) — Leg 5's offline twin: teach in a DM, ask in front of Bob (*disjoint*), ask without him (*admit*). Offline it can never produce a `withhold-unknown-fetch-failed`, which is why Leg 5 reports that count live.

This live MT confirms the *operator-observable* behaviour on a real provider; the gate/projection/tripwire invariants themselves are pinned in CI.
