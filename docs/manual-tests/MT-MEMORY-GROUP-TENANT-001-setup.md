# MT-MEMORY-GROUP-TENANT-001 — setup and preconditions

**Owner**: [MT-MEMORY-GROUP-TENANT-001](MT-MEMORY-GROUP-TENANT-001.md) — this is
that MT's Preconditions section, split out at **v1.2** when the corrections from
its first live execution pushed the combined doc past the 3 000-word cap (the
MT stood at 2 948 with 52 words of headroom). Splitting rather than trimming
follows the v0.3.15 cycle's practice: these are setup *contract*, and every one
of them was learned by an arc failing without it.

**Read this before spending the arc.** Nine of these steps were wrong or absent
in v1.1, which had never been executed against a compose deployment — the full
list, with the symptom each produces, is in the
[v0.3.15 execution report](v0.3.15-execution-report.md#mt-corrections--ten-defects-in-the-procedure-itself).

---

## Preconditions

1. `make build-orchestrator build-cli`; a live provider
   (`ANTHROPIC_API_KEY`) — the personas must produce real replies and
   land real rows. `make demo-anthropic`.

   > **There is no `PERSATRIX_SERVER`.** The CLI's `--server` is a `global`
   > clap argument with no `env` binding (`cli/src/main.rs`), so exporting that
   > variable — as this doc and four sibling MTs did — changes nothing, and the
   > CLI silently uses its own `http://localhost:8080` default. It reaches the
   > same stack here, so the export looked like it worked. Against any other
   > host, pass `--server` on every invocation; the driver now does.
2. `make reset` first — prior rows mask results.
3. `auth.mode: enabled` in `config/security.yaml`; no `data/accounts.db`
   (Leg 0 bootstraps it).
3b. **Declare `alice-person` and `bob-person` as members** in
   `config/channels.yaml` (`respond: observer`). **Never `persatrix channel
   join`** — a join writes only the *store*, and `ReconcileConfig` FATALs at
   startup on a store/config membership divergence (RFC 0011 §B). This MT
   restarts the orchestrator at Legs 0, 7, 8 **and twice more in Leg 9**, so a
   joined member is a crash loop one restart later; the check is symmetric, so both go in together, before the first
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
> which close trigger fires, which is the variable Legs 4 and 6 turn on.

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
> second principal means destroying the container's accounts store
> (see § Rotating the accounts store) and restarting the orchestrator. That restart rotates the wire
> interaction id, which **structurally closes** the open records — so a
> single record holding two *humans'* turns is not reachable on shipped
> verbs. It is not needed: with one human, the room's other speakers are
> the personas, whose turns are `'local'`, and `'local'`-vs-`alice-person`
> is the same strict-equality boundary. The two-human aggregate is pinned
> deterministically instead (see Sign-off).

## Rotating the accounts store — used by Legs 0 and 7

`account bootstrap` refuses once an account exists, so creating a second
principal means destroying the store and starting it again. **Both legs run
exactly this procedure**, with `<user>` / `<participant>` substituted — Leg 0
for `alice` / `alice-person`, Leg 7 for `bob` / `bob-person`. It lives here in
one piece because corrections 1, 2 and 4 were first applied to Leg 0's copy
and not to Leg 7's, which left the second rotation carrying all three defects.

```bash
# 1. Remove the database AND its sidecars, IN THE CONTAINER. The host default
#    `data/accounts.db` is not what the orchestrator reads (correction 1), and
#    a bare `rm` of the database leaves `-wal`/`-shm` beside a fresh empty one,
#    so the next bootstrap dies `read user_version: disk I/O error (522)`
#    (correction 2).
docker compose exec -T orchestrator rm -f \
  /var/lib/persatrix/accounts.db /var/lib/persatrix/accounts.db-wal /var/lib/persatrix/accounts.db-shm

# 2. Bootstrap where the orchestrator actually reads. Both prompts go over the
#    provisioning pipe, so the password never reaches argv (RFC 0039 §J).
printf 'PW\nPW\n' | docker compose exec -T orchestrator persatrix-server account bootstrap \
  --accounts-db /var/lib/persatrix/accounts.db --username <user> --participant <participant>

# 3. Restart — NOT optional. The orchestrator holds the deleted inode until it
#    reopens, so a login before this fails `invalid credentials` against an
#    account that plainly exists on disk.
docker compose restart orchestrator

# 4. POLL /healthz — do not sleep a fixed interval; the login races the restart
#    and fails `connection failed`.
printf 'PW\n' | ./bin/persatrix login --username <user>
./bin/persatrix whoami   # MUST read: <user> (participant <participant>)
```

Every subsequent `channel send` needs **`--as <participant>`** (correction 4):
the sender defaults to the **OS username**, not the authenticated principal.
Omitting it gives `403 … sender is not a member` — or, if that username
happens to be a member, a green run whose turns name a speaker who never
spoke, which is worse.

## Leg 3 fallback — forcing a close when the quorum does not form

Correction 7. Leg 3 asks for an end-vote quorum — two personas signalling
end-of-interaction inside the W=3 window — and on a stock roster it does not
arrive: the personas answer the wrap-up prompt without voting, and the arc
stalls with nothing derived. That happened on **both** attempts of the
2026-09-02 run (F-4), so treat the quorum as the hoped-for path, not the
expected one, and budget the fallback into the arc's pacing.

The deterministic fallback is to lower the channel's idle timeout and let
lazy retirement close the interaction:

```bash
./bin/persatrix channel config set group:planning interaction_idle_timeout_seconds=30
# …wait out the window, then RAISE IT BACK before Leg 4 reads:
./bin/persatrix channel config set group:planning interaction_idle_timeout_seconds=1800
```

Two things to hold on to:

- **Put the value back.** The preflight gate `interaction_idle_timeout_seconds
  >= 1800` exists so that no *later* leg closes an interaction by accident —
  the variable Legs 4 and 6 turn on. This fallback deliberately steps outside
  that gate for one window, and leaving it low silently changes Leg 4.
- **Record the deviation.** An idle close reports `structural`, not
  `end_votes`, so Leg 3's stated bar is not met and the report must say so
  rather than scoring it a pass. Leg 6 then carries more weight than planned:
  it becomes the only leg whose close descends from a trigger the arc chose.
