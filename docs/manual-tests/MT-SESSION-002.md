# Manual Test MT-SESSION-002: session operator surface, live

**Test ID**: `MT-SESSION-002`
**Feature Area**: Sessions (RFC 0031 Phase 3 — `persatrix session` operator verbs + the resolution chain)
**Version**: 1.0
**Created**: 2026-06-01
**Last Updated**: 2026-06-01
**Status**: Active

---

## Overview

**Purpose**: Verify the `persatrix session new / use / list / archive / current`
operator verbs round-trip live against the orchestrator `/api/v1/sessions`
registry, and that the **four-level resolution chain** (`--session` flag >
`PERSATRIX_SESSION_ID` env > `~/.persatrix/active-session` pointer file >
built-in `legacy`, RFC 0031 [OQ #6](../rfcs/0031-per-session-namespacing-channels.md#open-questions))
resolves end-to-end in the documented precedence order.

This is the **primary v0.3.5 operator-surface gate** — the live counterpart to
the `test_session_operator_surface.py` integration gate (which is skipped
without built binaries). MT-SESSION-001 covered the Phase 1 *write* contract;
this MT covers the Phase 3 *operator* surface that mints, switches, and inspects
sessions.

**Scope**:
- The five registry/pointer verbs against a live orchestrator.
- Each of the four resolution mechanisms, exercised so the winner at each
  precedence level is observable.
- The reserved-`legacy` rejection (§5 of the [sessions guide](../guides/sessions.md)).
- `use` validating its target against the registry *before* writing the pointer.

**Out of Scope**:
- Recall-side isolation (the F-3 promise) — that is [MT-SESSION-003](MT-SESSION-003.md).
- The per-`(agent, channel)` auto-mint on the dispatch path
  ([sessions guide §3](../guides/sessions.md#3-the-per-request-auto-binding)) —
  exercised incidentally here, asserted structurally by
  `tests/integration/test_session_emission_isolation.py`.
- The epoch axis — [MT-EPOCH-001](MT-EPOCH-001.md).

---

## Related Documentation

- [docs/guides/sessions.md](../guides/sessions.md) — operator guide (§2 verbs, §4 resolution chain, §5 `legacy`).
- [docs/rfcs/0031-per-session-namespacing-channels.md](../rfcs/0031-per-session-namespacing-channels.md) — §E operator surface, OQ #6 precedence.
- [docs/rfcs/0031-phase3-pr-plan.md](../rfcs/0031-phase3-pr-plan.md) — Phase 3 PR sequence (REST registry + CLI verbs).
- [cli/src/commands/session.rs](../../cli/src/commands/session.rs) — the `persatrix session` subcommands (thin REST client).
- [cli/src/session_resolve.rs](../../cli/src/session_resolve.rs) — the `--session` / env / pointer precedence resolver.
- [cli/src/active_session.rs](../../cli/src/active_session.rs) — the `~/.persatrix/active-session` pointer file.

**Related Automated Tests**:
- `tests/integration/test_session_operator_surface.py` (end-to-end against built orchestrator + CLI — `pytest -m requires_orchestrator`).
- `cli/src/session_resolve.rs` unit tests (precedence) + `cli/src/active_session.rs` (pointer round-trip) + `cli/src/validation.rs` (`--label` validation).
- `tests/unit/python/test_session_id_session_filter.py` / `test_session_recall_default_path.py` (default resolution path).

---

## Preconditions

Same baseline as [MT-CHANNEL-001 § Preconditions](MT-CHANNEL-001.md#preconditions):
a built `bin/persatrix` (`make build-cli`) and a running orchestrator. The
fastest way to a live registry is the offline society (no API key, no spend) —
the session verbs are pure REST + pointer operations and need **no** LLM:

```bash
make demo-offline          # orchestrator + agents up on the mock provider
```

To keep the pointer file out of `~/.persatrix` during the test (and make
cleanup a single `rm`), point it at a scratch path:

```bash
export PERSATRIX_ACTIVE_SESSION_FILE="$(mktemp -d)/active-session"
```

This MT is **non-destructive**: it creates and archives sessions but never
purges data. Archive is one-way by design (RFC 0031 §B) — the sessions minted
here stay in the registry afterward.

---

## Test Procedure

### Step 1: `current` with no pointer reports `legacy`

**Action**:

```bash
rm -f "$PERSATRIX_ACTIVE_SESSION_FILE"
./bin/persatrix session current
```

**Expected**: `no active session — using legacy` (the level-4 fallback — no
pointer, no env).

**Verification**:
- [ ] Output names the `legacy` fallback, not an error.

---

### Step 2: `new --activate` mints and activates a session

**Action**:

```bash
./bin/persatrix session new --label mt-session-002-a --activate --json
./bin/persatrix session current
```

**Expected**:
- `new` returns a JSON row with a UUIDv7 `id`, `label="mt-session-002-a"`,
  a `created_at`, and `archived=false`.
- `current` now prints the new session (label-enriched) — `--activate` wrote
  the pointer file (equivalent to a follow-up `session use`).

**Verification**:
- [ ] `new --json` echoes the minted `id` + `label`.
- [ ] `cat "$PERSATRIX_ACTIVE_SESSION_FILE"` contains that id.
- [ ] `current` reports `mt-session-002-a`.

---

### Step 3: `list` surfaces the registry

**Action**:

```bash
./bin/persatrix session new --label mt-session-002-b
./bin/persatrix session list
```

**Expected**:
- A table with `ID / LABEL / CREATED / ARCHIVED` columns.
- Both `mt-session-002-a` and `mt-session-002-b` appear, `ARCHIVED=false`.
- (Any auto-minted dispatch-path sessions may also appear — they share the
  registry, [sessions guide §3](../guides/sessions.md#3-the-per-request-auto-binding).)

**Verification**:
- [ ] Both operator-created labels are listed, active.

---

### Step 4: `use <label>` switches the active pointer by label

**Action**:

```bash
./bin/persatrix session use mt-session-002-b
./bin/persatrix session current
```

**Expected**:
- `use` resolves the label against the registry, then writes the pointer; the
  active id is echoed on success.
- `current` now reports `mt-session-002-b`.

**Verification**:
- [ ] `current` flips from `-a` to `-b`.
- [ ] The pointer file now holds `-b`'s id.

---

### Step 5: Resolution precedence — `current` is pointer-only; the chain governs the dispatch verbs

**Action**:

A subtle but important distinction. `session current` is a **pointer-inspection**
verb — it reads *only* the active-session file
([cli/src/commands/session.rs](../../cli/src/commands/session.rs) `cmd_session_current`),
not the env var or any flag. The four-level precedence chain lives in
[`session_resolve.rs`](../../cli/src/session_resolve.rs) and is what the
**dispatch verbs** (`chat` / `channel send` / `channel reply`) apply to decide
the `session_id` they send. So:

```bash
# current reflects the POINTER (level 3) only — env does NOT change it
PERSATRIX_SESSION_ID=mt-session-002-a ./bin/persatrix session current   # still -b
```

To observe the dispatch-path chain (`--session` flag > `PERSATRIX_SESSION_ID`
env > pointer), run a dispatch verb and read back the `session_id` the resulting
memory/channel row carries — that observation is the substance of
[MT-SESSION-003](MT-SESSION-003.md) (it boots the persona under
`PERSATRIX_SESSION_ID` and reads `episodes.session_id`). Note `--session`
resolves its argument against the registry first, so it only accepts a
**registered** id/label; an ad-hoc string must come via `PERSATRIX_SESSION_ID`
(which passes through unresolved — [`session_resolve.rs`](../../cli/src/session_resolve.rs)).

**Expected**:
- `session current` reports `mt-session-002-b` (the pointer) **even with**
  `PERSATRIX_SESSION_ID=mt-session-002-a` exported — `current` is not env-aware.

**Verification**:
- [ ] `current` reflects the pointer (`-b`), not the env (`-a`).
- [ ] The full dispatch-path precedence is exercised by `session_resolve.rs`
  unit tests + observed via row tags in [MT-SESSION-003](MT-SESSION-003.md).

---

### Step 6: Reserved-`legacy` rejection + `use` validates before writing

**Action**:

```bash
./bin/persatrix session new --label legacy            # expect rejection
./bin/persatrix session use no-such-session-xyz       # expect failure, pointer unchanged
./bin/persatrix session current
```

**Expected**:
- `session new --label legacy` is **rejected** server-authoritatively (the
  reserved carve-out, [sessions guide §5](../guides/sessions.md#5-the-legacy-carve-out)) —
  non-zero exit, no row minted.
- `session use no-such-session-xyz` fails (the target is resolved against the
  registry *first*), and the pointer is **not** rewritten — `current` still
  reports the Step 4 value (`-b`).

**Verification**:
- [ ] `new --label legacy` exits non-zero with a reserved-label message.
- [ ] `use` on an unknown target fails and leaves the pointer at `-b`.

---

### Step 7: `archive` is one-way; archived target is `use`-rejected but `--session`-permitted

**Action**:

```bash
./bin/persatrix session archive mt-session-002-a --json
./bin/persatrix session list                       # -a hidden
./bin/persatrix session list --include-archived    # -a shown, ARCHIVED=true
./bin/persatrix session use mt-session-002-a        # use refuses an archived target
echo "explicit archived" | ./bin/persatrix chat ember-owl --session mt-session-002-a  # warns but proceeds
```

**Expected**:
- `archive` marks `-a` inactive (one-way; no `unarchive`, no `delete`).
- Plain `list` no longer shows `-a`; `--include-archived` shows it with
  `ARCHIVED=true`.
- `session use mt-session-002-a` is **refused** (you cannot silently re-activate
  an archived session via the pointer).
- An explicit `--session mt-session-002-a` on a dispatch verb **warns but
  proceeds** — you named the archived session explicitly
  ([sessions guide §4](../guides/sessions.md#4-how-the-active-session-is-resolved)).

**Verification**:
- [ ] `-a` disappears from plain `list`, reappears under `--include-archived` with `ARCHIVED=true`.
- [ ] `use` on the archived `-a` is refused; the pointer is unchanged.
- [ ] `--session <archived>` warns yet completes the turn.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | No pointer → `current` reports `legacy` (level 4) | ☐ |
| 2 | `new --activate` mints + writes the pointer; `current` reflects it | ☐ |
| 3 | `list` surfaces both operator-created sessions | ☐ |
| 4 | `use <label>` re-points by label; `current` flips | ☐ |
| 5 | `current` is pointer-only (env does not change it); the chain governs dispatch verbs | ☐ |
| 6 | `legacy` label rejected; `use` validates target before writing | ☐ |
| 7 | `archive` one-way; archived target `use`-refused, `--session`-permitted | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Non-canonical `--label`

`session new --label "my session"` (a space) is rejected client-side by
`validate_session_label` ([cli/src/validation.rs](../../cli/src/validation.rs))
before any REST call — the hard validation Phase 1 deferred (MT-SESSION-001
Edge Case 1 only *warned* on a non-canonical env value). Expect a non-zero exit
naming the canonical `[A-Za-z0-9_-]` shape.

### Edge Case 2: Pointer file at a custom path

With `PERSATRIX_ACTIVE_SESSION_FILE` exported, `use` / `new --activate` /
`current` all read and write that path instead of `~/.persatrix/active-session`
— the mechanism this MT uses to keep the host pointer pristine, and the same one
parallel checkouts / CI use to avoid cross-run pointer collisions.

### Edge Case 3: Orchestrator unreachable

A registry verb against a down orchestrator (`new` / `list` / `archive` / the
registry-resolution half of `use`) fails with a connection error, not a panic.
`current` with a pointer already written still prints the cached id (it reads the
local file; only the label-enrichment lookup needs the server).

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-06-01 | Claude (Opus 4.8) | macOS + Docker (OpenAI stack) | Pass | All 7 steps live against the `/api/v1/sessions` registry on `main` `3ceb400`. Reserved-`legacy` rejected (exit 1), `use` registry-validates before writing (404, pointer unchanged), one-way archive + `--include-archived`, archived-target `use` refused. Step 5 corrected: `current` is pointer-only (not env-aware). See [v0.3.5-execution-report.md](v0.3.5-execution-report.md#mt-session-002--operator-surface-live-openai-stack). |

---

## Notes

- The session verbs are a **thin REST client** over `/api/v1/sessions`
  ([cli/src/commands/session.rs](../../cli/src/commands/session.rs)); the
  registry is orchestrator-owned (`channels.db`). The pointer file is
  **CLI-local** and does not live-rebind in-flight processes
  ([sessions guide §4](../guides/sessions.md#4-how-the-active-session-is-resolved)).
- A session is **room continuity, not a clean slate** — it accumulates across
  runs. For a rerun that inherits *nothing*, that is the epoch axis
  ([MT-EPOCH-001](MT-EPOCH-001.md)).
- This MT needs no API key — the offline society is sufficient because the verbs
  never call an LLM.
