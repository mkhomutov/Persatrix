# Sessions — Operator Guide

A practical walkthrough of the `persatrix session …` operator surface: what a
session is, how to create / list / switch / archive one, how the active session
is resolved for a run, and the operational footguns to avoid. Sessions scope
persona-memory recall so that one room's conversation does not bleed into
another's.

> **Spec-level detail** lives in [RFC 0031](../rfcs/0031-per-session-namespacing-channels.md)
> (§E operator surface, §D recall semantics, §B lifecycle). The scope-axes model
> that reframes "session" as room-continuity — and moves run/test isolation to a
> separate `epoch` axis — is [Memory Scope Axes](../memory-scope-axes.md). This
> guide is deliberately non-exhaustive and points into both for rationale.

> **A session is room continuity, not a clean slate.** A session is one room's
> ongoing memory, keyed `(agent, channel)`. It **accumulates** across runs and
> restarts — that is the point. Switching sessions changes *which* room's memory
> a run reads and writes; it does **not** wipe anything. If you want a rerun that
> inherits *nothing* (test isolation), that is the forthcoming **epoch** axis
> ([ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md)); until it
> ships, `make reset` (§6) is the supported clean-slate path.

---

## 1. What a session is

A **session** is a named, operator-visible namespace under which channels are
created and persona-memory rows (episodes, relationships, facts, notes) are
tagged. Each session has:

- a stable **`session_id`** — a UUIDv7 minted at creation (so ids sort
  lexicographically by creation time);
- a human-readable **label** (e.g. `demo-2026-05-30`);
- a **created** timestamp and a **status** (`active` / `archived`).

Default persona-memory recall is **session-scoped**: a run reads rows tagged
with its active session plus the always-visible `legacy` carve-out (§5), and
nothing else. Reaching across sessions is an explicit opt-in, not a default
(§7). This is what closes the F-3 cross-run state bleed at the root — a run
under a *different* session id surfaces none of another session's participants,
topics, or facts.

The registry of sessions lives with channels in the orchestrator-owned
`channels.db`; per-agent `memory.db` files carry the `session_id` on each row.
Two sessions coexist side by side in one store — switching is a logical
re-pointing, not a data migration.

## 2. The verbs

```
persatrix session new --label LABEL [--activate] [--json]
persatrix session list [--include-archived] [--json]
persatrix session use <id-or-label>
persatrix session current
persatrix session archive <id-or-label> [--json]
```

- **`new`** registers a new session. `--label` is **required** (a session
  without a human-readable name is hard to operate). `--activate` additionally
  writes it to the active-session pointer (§4) — equivalent to a follow-up
  `session use <new-id>`. The reserved label `legacy` is **rejected** (§5).
- **`list`** prints the registry as a table, active sessions only by default;
  `--include-archived` adds archived rows. `--json` emits the raw response for
  scripting. Auto-minted sessions (created on the dispatch path, §3) appear here
  alongside operator-created ones.
- **`use`** sets the active session by id *or* label. The target is resolved
  against the registry first, so a typo or an archived target fails **before**
  the pointer is written — never after, when new channels would silently
  misroute. The active id is logged at `INFO`.
- **`current`** prints the active session (label-enriched), or
  `no active session — using legacy` when no pointer is set.
- **`archive`** marks a session inactive. Archive is **one-way** (RFC 0031 §B):
  rows are retained and stay resolvable, there is no `delete` and no
  `unarchive`. Compliance erasure is separate (RFC 0013) territory.

### Example: a fresh room for a demo

```bash
persatrix session new --label demo-2026-05-30 --activate
persatrix session current          # → demo-2026-05-30
persatrix chat ember-owl --user alex
# … run the demo …
persatrix session archive demo-2026-05-30
```

## 3. The per-request auto-binding

Beyond the operator verbs, the orchestrator **auto-mints a session per
`(agent, channel)`** on the live channel-dispatch path and emits it as the
`persatrix-session` gRPC header, so concurrent conversations stay isolated
without any operator action. (The unit was `(agent, channel, sender)` until
[ISSUE-0083](../issues/ISSUE-0083-session-binding-sender-axis-fragments-multiparty-rooms.md)
dropped the sender axis, so a multi-party room is one shared room memory rather
than one-per-speaker.) Auto-minted sessions land in the same registry, so
`session list` surfaces them too.

The operator verbs are therefore **inspection + lifecycle + override**, not a
setter of one global session. An explicit `--session` (§4) overrides the
auto-binding for one invocation; absent it, the auto-binding stands.

## 4. How the active session is resolved

Three mechanisms can set the process-lifetime session. They resolve in this
**precedence order** (RFC 0031 OQ #6):

| Precedence | Mechanism | Set by |
|------------|-----------|--------|
| 1 (highest) | `--session <id-or-label>` flag on `chat` / `channel publish` / `channel list` | per invocation |
| 2 | `PERSATRIX_SESSION_ID` env var | operator, at boot |
| 3 | `~/.persatrix/active-session` pointer file | `session use` / `new --activate` |
| 4 (fallback) | built-in `legacy` | — |

- The **`--session` flag** wins for the one invocation it accompanies, *above*
  the per-request auto-binding (§3). It accepts an id or a label; a label that
  resolves to an archived session warns but proceeds (you named it explicitly).
- The **active-session pointer file** lives at `~/.persatrix/active-session`,
  overridable with `PERSATRIX_ACTIVE_SESSION_FILE` (handy for tests and
  multiple parallel checkouts). It is **CLI-local**: writing it changes which
  session the CLI defaults `--session` to and which the *next* orchestrator boot
  seeds from. It does **not** live-rebind in-flight processes — they continue
  under the session they started with.

## 5. The `legacy` carve-out

Rows written before this RFC shipped (and any row whose session resolves to the
fallback) carry `session_id = 'legacy'`. Legacy rows are **always visible**,
from every session — the carve-out that let sessions ship without backfilling
old data. Two consequences:

- `legacy` is a **reserved label**: `session new --label legacy` is rejected
  (server-authoritative) so an operator session can never silently merge into
  the always-visible namespace.
- Orphaned rows (see §6) degrade to "always visible" rather than disappearing.

Pruning legacy rows once they stop mattering is a deferred follow-up
(`persatrix memory legacy-prune`, not built).

## 6. `make reset` and the split-volume footgun

`make reset` (`docker compose down -v`) wipes **every** named volume in the
compose project — the orchestrator `channels.db`, the per-persona `memory.db`
volumes, and the agent scratch `workspace`. It is the **nuclear option** for a
clean rerun and remains the supported run-isolation tool until the `epoch` axis
([ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md)) ships.

Because the session **registry** (`channels.db`) and the per-agent
**memory.db** files live in *different* volumes that no single transaction spans
(RFC 0031 §Security Considerations), a **partial** reset is a footgun:

- Reset the orchestrator volume but **not** a persona's memory volume, and that
  persona keeps memory rows tagged with `session_id`s that no longer exist in
  the registry. The `legacy` carve-out keeps them visible rather than orphaned,
  but `session list` (which reads the registry only) will not show them.
- Conversely, `session archive` on the orchestrator side does **not** propagate
  to per-agent stores, so default recall on the persona side keeps surfacing
  rows under an archived session id until the active pointer changes.

**Reset both volumes together, or neither.** A future single-transaction society
store (RFC 0029 Phase 3) closes this gap structurally.

## 7. Cross-session recall

Reading across sessions (`sessions="*"`) is a library/debug capability with
**no operator entry point** — deliberately. The only proposed route, a
`persatrix memory recall --all-sessions` verb, is carved out as a follow-up
([ISSUE-0086](../issues/ISSUE-0086-operator-all-sessions-recall-verb.md)).
Leaving it unbuilt is the stronger posture: the all-sessions sentinel cannot
reach a prompt context, so it cannot re-introduce cross-run bleed against the
fix sessions ship.

## 8. Security and operational notes

- **Not a permissions boundary.** A process that can read a `memory.db` can read
  every session in it. Session ids namespace; they do not isolate against an
  in-process reader. Auth (RFC 0009) and erasure (RFC 0013) own that.
- **No secrets in labels.** Labels are operator-supplied and surface in logs and
  traces (the session id is treated as a low-cardinality, non-sensitive
  dimension). Keep credentials, tokens, and PII out of session labels.
- **Stale pointers misroute.** A `~/.persatrix/active-session` pointing at an
  archived session makes new channels attach to a session you thought was done.
  `use` validates the target before writing, and activation logs the id at
  `INFO` — check `session current` when a run surfaces unexpected memory.

## Related documentation

- [RFC 0031 — Per-Session Namespacing for Channels and Persona Memory](../rfcs/0031-per-session-namespacing-channels.md) — the spec; §E operator surface, §D recall semantics, §B lifecycle.
- [Memory Scope Axes](../memory-scope-axes.md) — the reframing: session = room continuity, epoch = run isolation, relationship = cross-room, principal = tenant.
- [Channels — User Guide](channels.md) — channels are created under a session; §10 covers `make reset`.
- [Persona Agents — User Guide](persona-agents.md) — persona memory is what sessions scope.
- [ISSUE-0051](../issues/ISSUE-0051-per-session-memory-namespacing-channels.md) — the F-3 root-cause issue this surface closes.
- [ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md) — the epoch axis that carries run/test isolation forward.
