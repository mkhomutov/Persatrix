---
id: ISSUE-0081
summary: "`PERSATRIX_SESSION_ID` is resolved once per process from a global env var and cached at tier construction (`agents/session_id.py:52`, `episodic.py:111` et al.). The persona runtime hosts many personas in one process (`dispatch.py:71`) and a single `agent_id` can field multiple concurrent conversations, all sharing one process-global session id — so once recall filtering is live (RFC 0031 Phase 2), conversation/user A's writes bleed into conversation/user B's recall under the same `(agent_id, session_id)`. Separately, the storage scope key has no tenant dimension and the `legacy` carve-out is readable/writable from every session, which is a cross-tenant leak the moment multi-user ships. Both gaps are out of scope of RFC 0031 as written (the RFC models session = one process run, set by the Go orchestrator at boot)."
status: resolved
severity: high
area: agents/memory
created: 2026-05-29
closed: 2026-08-18
refs:
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - docs/rfcs/0031-phase2-pr-plan.md
  - agents/session_id.py
  - agents/memory/_session_filter.py
  - agents/memory/episodic.py
  - agents/memory/relationship.py
  - agents/memory/facts.py
  - agents/memory/notes.py
  - agents/dispatch.py
  - agents/server_servicers.py
  - agents/observability/grpc_logging.py
  - cmd/orchestrator/startup.go
---

## Summary

RFC 0031 closes F-3 (cross-**run** state bleed) on the assumption that
**one process == one session**: `PERSATRIX_SESSION_ID` is read once at
boot by the Go orchestrator (`cmd/orchestrator/startup.go:39`
`resolveSessionID`), exported into the persona-runtime process, and on
the Python side resolved by `resolve_session_id_silent()`
(`agents/session_id.py:52`):

```python
return os.environ.get(SESSION_ID_ENV_VAR, "").strip() or LEGACY_SESSION_ID
```

Every memory tier caches that value **once, at construction**:

- `agents/memory/episodic.py:111` — `self._active_session_id = resolve_session_id_silent()`
- `agents/memory/facts.py:179`, `agents/memory/relationship.py:86`,
  `agents/memory/store.py:161`, and `NoteStore` via the
  `active_session_id` ctor arg (`notes.py:111`)
- `agents/persona.py:124` — `PersonaAgent._session_id`

`_resolve_session_list` (`agents/memory/_session_filter.py:77`) uses that
cached snapshot whenever `sessions=None` (the default recall path), so
the active-session filter is fixed for the lifetime of the tier object.

Two facts make the process-global assumption unsafe:

1. **One process hosts many personas.** `agents/dispatch.py:71` holds
   `self._agents: dict[str, _LLMPersonaAgent]`; `agents/server.py` /
   `agents/server_persona.py` serve them all behind one gRPC server.
2. **The memory tiers are long-lived per persona.**
   `create_persona_agent` (`agents/persona.py:270`) calls
   `build_personal_tiers(agent_id, ...)` **once** per `agent_id`; the
   tier objects (and their cached `_active_session_id`) are reused across
   every event that persona handles.

So a single `agent_id` fielding two concurrent conversations (two users
in a channel, two DM threads) shares one `(agent_id, session_id)`
namespace. With recall filtering now live (Phase 2 PRs #449–#452),
conversation/user A's writes are recalled into conversation/user B's
prompt. This is the **intra-process sibling of F-3**: the RFC fixed
cross-process bleed but the same bleed re-opens across concurrent
conversations inside one process.

A second, related gap: the storage scope key is `(agent_id,
session_id)` with **no tenant/principal dimension**, and the `legacy`
carve-out (`_session_filter.py` appends `LEGACY_SESSION_ID`; the notes
mutation surface filters `session_id IN (active, legacy)`) is readable
**and writable** from every session by design. Both become cross-tenant
data-bleed surfaces the moment more than one user is served by one
deployment.

## Context

Surfaced in a design discussion after RFC 0031 Phase 2 PR 5 (#452)
landed. The RFC's threat model is explicitly *cross-run* (rerunning the
same channel test under a new `PERSATRIX_SESSION_ID`); it never modeled
(a) one process serving multiple concurrent conversations for the same
agent, or (b) multiple tenants/users in one deployment. RFC 0031 Phases
3–4 are an *operator CLI + docs* pass — neither gap is on that roadmap.

Reproduction shape (illustrative — one shared tier, two concurrent
contexts):

```python
mem = build_personal_tiers("agent-a", db_path=path).episodic  # cached _active_session_id

# Conversation 1 writes; conversation 2 reads — same process, same agent.
await mem.store_episode("user-1 told me a secret", session_id="conv-1")
notes = await mem.recall("secret")            # sessions=None → cached snapshot
# notes includes conv-1's row regardless of which conversation is asking.
```

Because the env var is process-global and the snapshot is cached, there
is no per-conversation seam to set today — even a correct caller cannot
narrow recall to the conversation in flight.

## Impact

- **Cross-conversation memory bleed (live now, latent severity rising).**
  Any deployment where one persona handles more than one conversation at
  a time leaks recall across them. The persona's LLM prompt is built
  from `recall` / `get_relationship_summary` / facts / notes — all of
  which default to the cached session, so the contamination lands
  directly in the prompt. This is the same dementia-test failure mode
  RFC 0031 was created to fix, on the concurrency axis.
- **Cross-tenant leak (blocks multi-user).** With no tenant dimension
  and a globally-shared `legacy` carve-out, user B can read and mutate
  rows user A wrote (directly for `legacy`, and via the shared
  `(agent_id, session_id)` key for any shared session). This is a
  privacy boundary, not just a correctness nit.
- **Mechanism, not policy, is the blocker.** Every downstream filter
  (the §D recall predicate, per-session supersession, per-session
  counts, the mutation surface) is already correct and stays unchanged.
  Only the *source* of the active session id — process-global env read,
  cached at construction — is too weak.

## Proposed fix / investigation path

The fix is to move the session id from **process-global, cached** to
**task-local, resolved at call time**, then propagate a per-request
session id across the gRPC boundary, then add a tenant dimension. The
storage layer (scope predicates, migrations machinery, carve-out logic)
is reused as-is. There is a strong in-repo precedent for the propagation
half: **RFC 0018 Phase 3** already binds `persatrix-*` gRPC metadata
keys to task-local `contextvars.ContextVar`s per request and resets them
after (`agents/observability/grpc_logging.py`). The session id should
ride the same rail.

Sequenced as a multi-PR workstream (all v0.3.5):

1. **PR 1 — context-local session id (enabler).** Add a
   `contextvars.ContextVar[str | None]` to `agents/session_id.py` with
   `current_session_id()` reader and a `session_scope(session_id)`
   context manager. Resolution precedence: **ContextVar → env var →
   `legacy`**. Make the tiers resolve the active session **at call
   time** (recall *and* mutation paths) as `current_session_id() or
   self._active_session_id`, keeping the construction snapshot as a
   fallback seed. Behaviour is unchanged when no ContextVar is set (the
   env var still seeds it), so single-session CLI / test / boot paths do
   not move. TDD gate: two concurrent `asyncio` tasks under different
   `session_scope(...)` get isolated recall and writes from **one**
   shared tier instance.

2. **PR 2 — gRPC session propagation + dispatch binding.** Extend the
   RFC 0018 correlation interceptor (or add a sibling) to bind a
   `persatrix-session` metadata key from incoming gRPC metadata into the
   session ContextVar for the duration of the call. The Go orchestrator
   emits `persatrix-session` per outgoing request, derived from the
   conversation/user. **Open design decision — the "session unit":**
   what defines a conversation key (channel? DM thread? `(channel,
   peer)`? user?), and whether the id is orchestrator-authoritative
   (required for the dementia-test multi-day arc to survive a process
   restart) vs. derived deterministically from a stable conversation
   key. Lands an **RFC 0031 §B/§E amendment** recording the chosen unit.

3. **PR 3 — tenant/principal dimension.** Migration adding
   `principal_id` (working name) to `episodes` / `relationships` /
   `facts` / `notes` / `interactions`; extend the scope key from
   `(agent_id, session_id)` to `(agent_id, principal_id, session_id)`
   across every recall and mutation path; propagate `principal_id` from
   the orchestrator via the same metadata rail. **RFC 0031 §C
   amendment.**

4. **PR 4 — legacy carve-out multi-tenant hardening.** Make the carve-out
   tenant-scoped (or retire it via a backfill migration) so it can no
   longer bridge tenants. **RFC 0031 §D amendment.** TDD: a foreign
   tenant can neither read nor write `legacy` rows.

A Phase closeout (RFC §C/§D status flips, ROADMAP) folds in with the
final PR.

## Notes

> 2026-05-29 — initial capture during a post-PR-#452 design review.
> Severity is high (not medium) because the cross-conversation leak
> contaminates the persona LLM prompt directly and the cross-tenant leak
> is a privacy boundary that blocks the multi-user story. The maintainer
> directed that **all gaps be closed within v0.3.5**, multi-PR. The
> contextvars enabler (PR 1) is decision-free and is being built first;
> the session-unit (PR 2) and tenant-key (PR 3) design calls are
> recorded inline in those PRs as RFC amendments for review.
>
> 2026-05-29 — **PR 3 (tenant/principal dimension) landed the Python
> vertical.** Migration v11 adds `principal_id TEXT NOT NULL DEFAULT
> 'local'` to all five persona-memory tables; the scope key is now
> `(agent_id, principal_id, session_id)` with **strict-equality** recall
> (no carve-out — `agents/memory/_principal_filter.py`) on both the read
> and write paths (including the facts supersession chain). A sibling
> `principal_scope` ContextVar + the `persatrix-principal` gRPC rail
> (`agents/principal_id.py`, bound in `on_event` via
> `agents/request_scope.py`) ship now and resolve to `'local'` until the
> verified-principal source (RFC 0039, still proposed) lands. Per the
> maintainer's recorded decisions: **strict isolation** over a
> default-principal carve-out, and **Python-vertical + rail now, Go
> orchestrator emission deferred** (mirroring PR 2). Recorded as the
> RFC 0031 §C/§D amendments. A review follow-up principal-scoped the
> procedural-reuse `refresh_confidence` (it matched `(agent_id, key)`
> only — a second tenant's re-store refreshed the first tenant's row and
> was then dropped by the refresh short-circuit). Remaining: PR 4 hardens
> the session `legacy` carve-out so it cannot bridge principals; and the
> agent-global background maintenance sweeps (episode eviction/retention,
> superseded-fact prune) plus GDPR `delete_by_subject` are not yet
> per-principal — they are capacity/erasure-policy calls deferred to the
> RFC 0039 multi-tenant work, not read-confidentiality leaks (recall
> stays principal-filtered). See the RFC 0031 §C amendment.
>
> 2026-05-29 — **PR 4 (carve-out closeout) completes the Python vertical.**
> A post-PR-3 audit confirmed PR 3's unconditional `AND principal_id = ?`
> already bounds the session `legacy` carve-out to a single principal on
> every per-request path, so PR 4 adds **no new mechanism**: it pins the
> property as the issue's explicit TDD gate — *a foreign tenant can
> neither read nor write `legacy` rows* — across all four tiers' default
> recall path, the notes mutation surface, and the facts supersession
> older-sweep (`tests/unit/python/test_principal_legacy_carveout.py`), and
> finalises the RFC 0031 §D amendment. The carve-out is **retained** (it
> stays load-bearing for the within-principal pre-RFC-upgrade dementia
> surface), not retired via backfill. **This issue stays open** because
> the per-request rail (`persatrix-session` / `persatrix-principal`) is
> armed but **not yet fed**: the Go orchestrator still resolves one
> session id per process at boot and emits no per-request headers, so the
> cross-conversation / cross-tenant fix is dormant until the orchestrator
> emits per-request ids. That activation half is tracked as its own
> issue — [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md)
> (session: a Go follow-up; principal: gated on RFC 0039) — and this
> umbrella stays open until it lands.
> Single-session-per-process deployments are unchanged and correct today.
>
> 2026-05-29 — **ISSUE-0082 fed the session half of the rail.** The Go
> orchestrator now mints + persists a `(agent, channel, user) → session_id`
> binding (`internal/channels/session_binding.go`, migration v4) and emits
> `persatrix-session` on the live dispatch path
> (`internal/channels/grpc_dispatcher.go` via
> `internal/observability/grpcmeta`), so `_session_from_context` resolves a
> real per-request id and the cross-conversation isolation this issue's
> Python vertical built is now **active**. Pinned end-to-end by
> `tests/integration/test_session_emission_isolation.py`. **This issue
> stays open** until the *principal* half is fed: `persatrix-principal`
> still emits nothing (every request resolves to `'local'`), so the
> cross-tenant boundary remains dormant pending the verified-principal
> source in [RFC 0039](../rfcs/0039-user-accounts-authentication.md) —
> tracked as ISSUE-0082 Part 2.
>
> 2026-08-05 — **The residuals are scoped: the [v0.3.14 plan](../v0.3.14-plan.md)
> is open** and carries ISSUE-0082 Part 2 (the principal emission this
> umbrella waits on), so this issue closes with that release. The
> deferred residuals named in the PR 3 note above are **split by class**
> at the plan opening: `delete_by_subject`
> (`agents/memory/_facts_erasure.py`) is **in scope** — both DELETEs are
> `agent_id`-scoped only, so the day emission ships, one person's
> erasure would delete another person's facts about the same subject; a
> privacy boundary that breaks on the same day, fixed by two predicates
> and the PR-4-idiom gate (*a foreign principal can neither count nor
> delete another principal's rows*). The **agent-global capacity sweeps**
> (episode TTL + size-cap eviction, procedural decay, superseded-fact
> prune, note prune) are **cut to v0.4.0** and ship as a named Known Gap:
> they are capacity/retention policy rather than read-confidentiality
> (recall stays principal-filtered either way), and per-principal quota
> semantics is a design question, not a predicate.
>
> 2026-08-11 — **The erasure residual is closed (v0.3.14 PR 3).** Both
> DELETEs in `agents/memory/_facts_erasure.py` now carry
> `AND principal_id = ?`, resolved through the same
> `resolve_active_principal` seam the recall and write paths use, so a
> caller can erase exactly the rows it could read. The gate is
> `tests/unit/python/test_facts_erasure_principal_scope.py` in the PR 4
> idiom — *a foreign principal's erasure call can neither count nor
> delete another principal's rows* — on **both** traversal columns, plus
> the count half (the return map is RFC 0013's audited `records_deleted`,
> so a foreign row in the tally discloses that another tenant holds facts
> about the subject even when the DELETE is correctly scoped). The
> `session_id` / `epoch_id` axes are deliberately **not** scoped and are
> pinned that way: a right-to-erasure traversal must reach every row the
> principal ever wrote, so narrowing it to the caller's active room / run
> would be a silent GDPR miss. The **caller audit** found no in-tree
> caller at all — the primitive still waits on RFC 0013's
> `SubjectErasure` (v0.5.0) — so the operator-erasure question is filed
> rather than answered here:
> [ISSUE-0127](ISSUE-0127-cross-principal-erasure-verb.md) (an
> operator traversal has no principal of its own, and the predicate has
> no `"*"`; write-side sibling of ISSUE-0086), slotted v0.5.0 with the
> RFC that owns the decision. This umbrella still stays open until the
> live arc verifies the emission at v0.3.14 release-prep.

> **2026-08-18 — RESOLVED.** The live arc ran at v0.3.14 release-prep PR 1
> ([execution report](../manual-tests/v0.3.14-execution-report.md)) and the
> umbrella condition above — "stays open until the live arc verifies the
> emission at v0.3.14 release-prep" — is met. `MT-MEMORY-MULTIUSER-001`
> executed on a real provider under `auth.mode: enabled` with two
> bootstrapped accounts against one persona in one process, and the two
> emitted principals were read off storage and recorded verbatim:
> `episodes [('alice-person', 2), ('bob-person', 1), ('local', 2)]`, with
> `alice-person` and `bob-person` likewise distinct on `facts` and
> `relationships`. Alice's disclosure landed under her own principal (never
> `local`), Bob's turn surfaced none of it, and Alice's arc still read back
> after his. The process-global half shipped 2026-05-29; the tenant half is
> now fed and verified live.
>
> Scope of the closure: this is the **per-turn** boundary on the live
> dispatch. Three residuals on the emission half remain open and are owned
> by [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md)
> — R-1 and R-2 (v0.3.15), and R-3
> ([ISSUE-0130](ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md),
> the catch-up replay re-deriving under the default principal), whose
> leak-stopper lands inside v0.3.14.

