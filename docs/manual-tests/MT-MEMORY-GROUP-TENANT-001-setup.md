# MT-MEMORY-GROUP-TENANT-001 — setup and preconditions

**Owner**: [MT-MEMORY-GROUP-TENANT-001](MT-MEMORY-GROUP-TENANT-001.md) — this is
that MT's Preconditions section, split out at **v1.2** when the corrections from
its first live execution pushed the combined doc past the 3 000-word cap (the
MT stood at 2 948 with 52 words of headroom). Splitting rather than trimming
follows the v0.3.15 cycle's practice: these are setup *contract*, and every one
of them was learned by an arc failing without it.

**Read this before spending the arc.** Seven of these steps were wrong or absent
in v1.1, which had never been executed against a compose deployment — the full
list, with the symptom each produces, is in the
[v0.3.15 execution report](v0.3.15-execution-report.md#mt-corrections--ten-defects-in-the-procedure-itself).

---

## Preconditions

1. `make build-orchestrator build-cli`; a live provider
   (`ANTHROPIC_API_KEY`) — the personas must produce real replies and
   land real rows. `make demo-anthropic`, `export
   PERSATRIX_SERVER=http://127.0.0.1:8080`.
2. `make reset` first — prior rows mask results.
3. `auth.mode: enabled` in `config/security.yaml`; no `data/accounts.db`
   (Leg 0 bootstraps it).
3b. **Declare `alice-person` and `bob-person` as members** in
   `config/channels.yaml` (`respond: observer`). **Never `persatrix channel
   join`** — a join writes only the *store*, and `ReconcileConfig` FATALs at
   startup on a store/config membership divergence (RFC 0011 §B). This MT
   restarts at Legs 0, 7 and 8, so a joined member is a crash loop one restart
   later; the check is symmetric, so both go in together, before the first
   boot (or `make reset` after adding). [v0.3.15 report](v0.3.15-execution-report.md#mt-corrections--ten-defects-in-the-procedure-itself), correction 3.

3c. **Rebuild the HOST binaries** — `make build-orchestrator build-cli`.
   `--build` refreshes the containerised orchestrator, not `bin/persatrix`,
   which drives Legs 1–8.

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

> **Verify auth on the RUNNING orchestrator, not in the file.** Enabling it
> needs a restart, so file and process disagree whenever the stack booted
> first — and the file misleads: a comment in the auth block contains the
> literal `mode: enabled` above a setting reading `disabled`, so a substring
> check passes on prose. That cost a live leg: the arc ran green with **every
> dispatch tenant-less**, which reads exactly like R-2 failing and was auth
> being off. Probe instead — an unauthenticated `GET` on a
> `policyAuthenticated` route must answer **401**.
> `scripts/manual_tests/mt_group_tenant_preflight.py` runs this and the other
> vacuity gates.

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
