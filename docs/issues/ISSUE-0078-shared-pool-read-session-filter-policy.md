---
id: ISSUE-0078
summary: "`SharedMemoryPool.read` calls `self._episodic.recall` with no `sessions=` kwarg, so the pool's read view silently collapses to the pool's *init-time* `_active_session_id` + the `legacy` carve-out. A row written under a session different from the pool's init session (canonical: pool constructed in `legacy` env, then writer publishes via the facade with `session_id='run-a'`) is invisible to every reader. Bypasses RFC 0008 §H's cross-agent / cross-session sharing intent. Open policy question for PR 4 (facade read-path extension) — fix should thread `sessions` through `read_from_pool` → `read_via_facade` → `pool.read` → `pool._episodic.recall`, and decide the default-mode policy for shared pools."
status: resolved
severity: medium
area: agents/memory
created: 2026-05-28
refs:
  - docs/rfcs/0008-agent-memory-context-optimization.md
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - docs/rfcs/0031-phase2-pr-plan.md
  - agents/memory/shared_pool.py
  - agents/memory/shared_pool_facade.py
---

## Summary

RFC 0031 Phase 2 PR 2 ([#449](https://github.com/mkhomutov/Persatrix/pull/449))
made `EpisodicMemory.recall` default to filtering by
`(_active_session_id, legacy)` and resolved `_active_session_id`
once at tier construction.  `SharedMemoryPool` wraps its own
`EpisodicMemory` instance (one per pool, namespaced by
`pool-{name}` — see [`shared_pool.py:167`](../../agents/memory/shared_pool.py#L167)).
That wrapping pulled the pool's read path into the new default
without an explicit decision.

[`SharedMemoryPool.read`](../../agents/memory/shared_pool.py#L255):

```python
episodes = await self._episodic.recall(query, limit=recall_limit)
```

No `sessions=` kwarg — so the read defaults to `[pool._active_session_id,
"legacy"]`, where `pool._active_session_id` is whatever the env
held when the pool was *constructed* (typically once at process
startup).  Writes still tag with the *caller's* session at write
time (`pool.write(session_id=…)` at [`shared_pool.py:334`](../../agents/memory/shared_pool.py#L334)
forwards to `EpisodicMemory.store_episode`).  The two sides are
out of step.

## Reproduction (2026-05-28, PR #449 branch)

Two corner cases reproduce empty-result reads where the caller
should have seen rows:

**Case 1 — pool init in `legacy`, write under `run-a`:**

```python
os.environ.pop("PERSATRIX_SESSION_ID", None)
pool = SharedMemoryPool(cfg, db_path=...)
await pool.initialize()
# pool._active_session_id == "legacy"
await pool.write("alice", "apple", confidence=0.9, session_id="run-a")
entries = await pool.read("bob", "apple", limit=10)
# entries == []  ← row exists, but invisible
```

**Case 2 — pool init in `run-a`, multi-tenant writes:**

```python
os.environ["PERSATRIX_SESSION_ID"] = "run-a"
pool = SharedMemoryPool(cfg, db_path=...)
await pool.initialize()
await pool.write("alice", "apple", confidence=0.9, session_id="run-a")
await pool.write("alice", "banana", confidence=0.9, session_id="run-b")
e1 = await pool.read("bob", "apple",  limit=10)  # ['apple']
e2 = await pool.read("bob", "banana", limit=10)  # []  ← invisible
```

Both runs match the trace expectation: `_resolve_session_list(None,
"legacy")` → `["legacy"]`; `_resolve_session_list(None, "run-a")`
→ `["run-a", "legacy"]`.  The "run-a" row in Case 1 and the
"run-b" row in Case 2 are not in those lists and never reach the
result set.

## Impact

* **Single-tenant single-session deployments are unaffected** — the
  pool is initialised under the same env as the agent's facade, and
  every write tags with that same session, so the read filter is a
  superset of every tag and surfaces everything.
* **Cross-session sharing is silently broken** — the canonical
  RFC 0008 §H "curated cross-agent knowledge" use case (agent A
  publishes under one session, agent B reads under another) does
  not surface rows.  The breakage is silent: no error, no log line,
  reads just return `[]` or a partial slice.
* **Long-lived multi-tenant process** — multiple agents in one
  process under different `PERSATRIX_SESSION_ID` values share one
  pool registry.  The pool's `_active_session_id` is whichever was
  set when the pool was constructed (typically the first one), and
  every agent reads through that lens.

No existing test exercises this shape — every shared-pool test in
the tree runs under the autouse `_isolate_session_env` fixture that
deletes `PERSATRIX_SESSION_ID`, so every write tags `"legacy"` and
the legacy carve-out makes the filter a no-op.  The regression is
real but not observable from the current suite.

## Proposed fix / investigation path

This is open policy for PR 4 (`feature/v035-rfc0031p2-facade-callsites`
— [`0031-phase2-pr-plan.md` PR 4 scope row](../rfcs/0031-phase2-pr-plan.md#pr-4-featurev035-rfc0031p2-facade-callsites--facade-read-path-extension--call-site-threading)).
The PR 4 row already lists `read_from_pool (L141) gains sessions`,
but only at the outermost facade layer; the threading actually
needs three layers, and the default policy needs to be picked.

1. **Thread `sessions` through the read chain end-to-end:**
   * `MemoryStore.read_from_pool(sessions=…)` (already listed)
   * → `read_via_facade(sessions=…)` ([`shared_pool_facade.py:77`](../../agents/memory/shared_pool_facade.py#L77))
   * → `SharedMemoryPool.read(sessions=…)` ([`shared_pool.py:221`](../../agents/memory/shared_pool.py#L221))
   * → `pool._episodic.recall(sessions=…)` ([`shared_pool.py:255`](../../agents/memory/shared_pool.py#L255))

2. **Pick the default-mode policy** for shared pools when the
   caller passes `sessions=None`.  Two coherent choices:

   * **A — Cross-session by design** (matches RFC 0008 §H): default
     to `sessions="*"` on the pool's underlying recall.  Justification:
     shared pools are curated cross-agent knowledge whose value is
     precisely that they are not session-scoped.  The session-
     isolation guarantee already lives at the per-agent
     `EpisodicMemory` tier — the pool is the explicit override.

   * **B — Session-scoped, caller-overridable**: default to the
     caller's facade `_session_id` (as `MemoryStore.retrieve_relevant`
     will under PR 4).  Justification: consistency with every other
     recall surface.  An operator who wants cross-session reach
     passes `sessions="*"`.

   Recommend **A** for the *underlying* recall (pool entries are
   curated and the cross-agent intent is part of the RFC 0008 §H
   contract) while still letting the facade signature accept
   `sessions=` so an operator can narrow when they want to.  Whichever
   PR 4 picks, document the choice in the PR 4 description and pin
   it with a test.

3. **Tests to add in PR 4** (mirror the reproductions above):
   * Pool init under one session, write under another → reader
     surfaces the row under the chosen default policy.
   * Multi-tenant write pattern (two writers, two sessions, one
     pool) → reader's `sessions=None` / `sessions="*"` /
     `sessions=[…]` modes return the expected subsets.
   * Pre-existing `tests/unit/python/test_session_id_facade_surfaces.py::TestPublishViaFacadeSessionID`
     covers the write side; the read-side mirror is missing.

## Notes

> 2026-05-28 — initial capture during PR #449 deep review (finding
> M1).  PR 449 is "Episodic + Notes **recall** filtering" at the
> tier layer; the shared-pool surface inherits the new tier default
> without an explicit decision.  PR 4 owns the facade read-path
> extension and is the right place to thread + decide.
>
> 2026-05-28 — **resolved** by PR #451 (RFC 0031 Phase 2 PR 4).
> Initial PR threaded `sessions` end-to-end and picked Policy A by
> defaulting `read_from_pool(sessions=None)` to `"*"` at the facade.
> Deep-review finding M2 then moved the default down to the data
> layer itself: `SharedMemoryPool.read` defaults `sessions="*"`, and
> `read_via_facade` / `read_from_pool` are pass-throughs.  Single
> source of truth — a direct caller of the pool tier cannot
> accidentally trigger session narrowing.  Pinned by
> `tests/unit/python/test_shared_memory_pool.py::test_pool_read_default_is_cross_session`
> and the three facade-layer pins in
> `test_session_recall_default_path.py::TestFacadeReadFromPoolSessionForwarding`.
