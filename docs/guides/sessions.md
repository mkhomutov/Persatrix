# Sessions — Operator Guide

A practical walkthrough of the `persatrix session …` operator surface: what a
session is, how to create / list / switch / archive one, how the active session
is resolved for a run, and the operational footguns to avoid. Sessions name
rooms — they bind channels and persona-memory rows to one room's continuity.
(Since v0.3.12, room boundaries *rank and gate* persona recall rather than
hard-walling it — see §7.)

> **Spec-level detail** lives in [RFC 0031](../rfcs/0031-per-session-namespacing-channels.md)
> (§E operator surface, §D recall semantics, §B lifecycle). The scope-axes model
> that reframes "session" as room-continuity — and moves run/test isolation to a
> separate `epoch` axis — is [Memory Scope Axes](../memory-scope-axes.md). This
> guide is deliberately non-exhaustive and points into both for rationale.

> **A session is room continuity, not a clean slate.** A session is one room's
> ongoing memory, keyed `(agent, channel)`. It **accumulates** across runs and
> restarts — that is the point. Switching sessions changes *which* room's memory
> a run reads and writes; it does **not** wipe anything. If you want a rerun that
> inherits *nothing* (test isolation), that is the **epoch** axis — a sibling of
> this one, shipped in v0.3.5
> ([ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md)); see the
> [epochs guide](epochs.md). `make reset` (§6) stays the whole-stack nuke, not
> the everyday run-isolation tool.

---

## 1. What a session is

> **Not a login session.** Since v0.3.12, RFC 0039 auth also has "sessions" —
> the revocable server-side records behind `persatrix login` and the console
> cookie. Those are **account** artifacts (an account binds to one
> *participant*, the chat identity); the sessions in this guide are **rooms**
> (memory continuity). Logging in or out never changes which room a run reads
> and writes. See the [auth guide](auth.md) for account ≠ session ≠
> participant in full.

A **session** is a named, operator-visible namespace under which channels are
created and persona-memory rows (episodes, relationships, facts, notes) are
tagged. Each session has:

- a stable **`session_id`** — a UUIDv7 minted at creation (so ids sort
  lexicographically by creation time);
- a human-readable **label** (e.g. `demo-2026-05-30`);
- a **created** timestamp and a **status** (`active` / `archived`).

How much recall the session boundary scopes is **tier-dependent since v0.3.12**
(§7): fact recall is cross-room by default and episodic recall is room-first
*ranked* rather than walled — every cross-room candidate passing the RFC 0037
classification gate — while the notes tier and the in-room conversation window
stay room-scoped, and the always-visible `legacy` carve-out (§5) applies
throughout. Cross-**run** isolation (the F-3 bleed fix) is owned by the
**epoch** axis, not the session id — a fresh epoch inherits nothing
([epochs guide](epochs.md)).

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
  misroute. The active id is echoed on success.
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
| 1 (highest) | `--session <id-or-label>` flag on `chat` / `channel send` / `channel reply` | per invocation |
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

> **Scoping a whole arc vs. a single call.** A `--session` (or `--epoch`) on one
> `chat` / `channel send` call governs the *recall query* and channel-binding for
> that invocation. But a persona's episode is written asynchronously at
> *interaction close* in its background loop, tagged with the session the
> persona-runtime **snapshotted at boot** — so a per-invocation override does
> **not** retag that close-path write in a long-running persona. To scope an
> entire arc (e.g. a dementia-test run across calls), set the session at the
> persona's boot (`PERSATRIX_SESSION_ID`), not per invocation. The structural
> isolation itself is intact either way; this is a write-attribution nuance, not
> a recall leak.

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
volumes, and the agent scratch `workspace`. It is the **nuclear option** — the
whole stack, all epochs across all sessions. For everyday run/test isolation,
reach for the `epoch` axis instead (a fresh `PERSATRIX_EPOCH` / `--epoch`
inherits *nothing*; [ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md),
[epochs guide](epochs.md)); `make reset` is for when you want the volumes
themselves gone.

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

## 7. Cross-room recall — the v0.3.12 posture

Since v0.3.12 ([RFC 0049](../rfcs/0049-memory-consolidation-gradient.md)
Phases 0–1, live), the session boundary is a **ranking and provenance axis**
for persona memory, not a recall wall. Per tier:

- **Facts** cross rooms by default — a project fact taught in a DM is a recall
  candidate in every room the persona belongs to (topic knowledge included,
  via the `topic.*` predicates). Knob: `memory.facts.cross_room:
  live | shadow | off` (default `live`; `shadow` computes + traces without
  injecting — the rollback lever).
- **Episodic** recall is **room-first ranked**: same-room episodes are boosted,
  other-room episodes admissible but demoted. Knob:
  `memory.episodic.cross_room` (same values).
- **Relationship** was always cross-room (trust follows the person).
- **Notes** and the in-room conversation window stay room-scoped.

Every cross-room candidate passes the deterministic
[RFC 0037 §D classification gate](../rfcs/0037-memory-confidentiality-channel-classification.md#d-the-hard-gate-at-memory-injection)
before injection — a fact learned in a `restricted` room never surfaces in an
`internal` one. What stays absolute: the **epoch** (run/test) and **principal**
(tenant) walls; cross-room recall ranges over rooms, never across those.
[MT-MEMORY-CROSSROOM-001](../manual-tests/MT-MEMORY-CROSSROOM-001.md) is the
live acceptance arc for the carry half;
[MT-PERSONA-CONFIDENTIALITY-001](../manual-tests/MT-PERSONA-CONFIDENTIALITY-001.md)
for the withhold half.

An *operator* recall verb across sessions remains unbuilt
([ISSUE-0086](../issues/ISSUE-0086-operator-all-sessions-recall-verb.md) —
the widened *runtime* read above is classification-gated and budgeted; a raw
dump verb would be neither).

## 8. Security and operational notes

- **Not a permissions boundary.** A process that can read a `memory.db` can read
  every session in it. Session ids namespace; they do not isolate against an
  in-process reader. Auth ([RFC 0039](../rfcs/0039-user-accounts-authentication.md)
  for humans, RFC 0009 for agents) and erasure (RFC 0013) own that.
- **No secrets in labels.** Labels are operator-supplied and surface in logs and
  traces (the session id is treated as a low-cardinality, non-sensitive
  dimension). Keep credentials, tokens, and PII out of session labels.
- **Stale pointers misroute.** A `~/.persatrix/active-session` pointing at an
  archived session makes new channels attach to a session you thought was done.
  `use` validates the target before writing, and activation echoes the active
  id — check `session current` when a run surfaces unexpected memory.

## Related documentation

- [RFC 0031 — Per-Session Namespacing for Channels and Persona Memory](../rfcs/0031-per-session-namespacing-channels.md) — the spec; §E operator surface, §D recall semantics, §B lifecycle. Its [fact-scope amendment](../rfcs/0031-amendment-fact-scope-by-consolidation-level.md) is the §7 L2 widening.
- [RFC 0049 — Memory Consolidation Gradient](../rfcs/0049-memory-consolidation-gradient.md) — the one law behind §7: recall scope follows consolidation level; the [L1 amendment](../rfcs/0049-amendment-l1-cross-room-availability.md) is the room-first episodic ranking.
- [RFC 0037 — Memory Confidentiality](../rfcs/0037-memory-confidentiality-channel-classification.md) — the classification gate every cross-room candidate passes.
- [Memory Scope Axes](../memory-scope-axes.md) — the reframing: session = room continuity, epoch = run isolation, relationship = cross-room, principal = tenant.
- [Channels — User Guide](channels.md) — channels are created under a session; §10 covers `make reset`.
- [Persona Agents — User Guide](persona-agents.md) — persona memory is what sessions scope.
- [Accounts & Auth — Operator Guide](auth.md) — RFC 0039 *login* sessions (account → participant binding); unrelated to the memory sessions here despite the shared word.
- [ISSUE-0051](../issues/ISSUE-0051-per-session-memory-namespacing-channels.md) — the F-3 root-cause issue this surface closes.
- [ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md) — the epoch axis that carries run/test isolation forward.
