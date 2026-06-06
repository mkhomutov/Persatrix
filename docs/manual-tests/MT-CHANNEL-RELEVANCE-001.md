# Manual Test MT-CHANNEL-RELEVANCE-001: Relevance gate Tier A — addressing-aware directedness

**Test ID**: `MT-CHANNEL-RELEVANCE-001`
**Feature Area**: Channels (conversation governance — RFC 0030 Layer 3, relevance amendment Tier A)
**Version**: 1.0
**Created**: 2026-06-06
**Last Updated**: 2026-06-06
**Status**: Active

---

## Overview

**Purpose**: Verify the v0.3.7 user-facing promise of **addressing-aware
directedness** (RFC 0030 Layer 3, the [relevance-gated-response amendment](../rfcs/0030-amendment-relevance-gated-response.md)
Tier A): a message that `@`-mentions **one** persona no longer draws a reply
from **every** `participant` member of the channel. Before this slice, the
response gate had no notion of *directedness* — `always` (now `participant`)
literally meant "reply to every message" — so the v0.3.6 manual probe of a
multi-persona channel hit the [Trigger defect](../rfcs/0030-amendment-relevance-gated-response.md#context):
`"how about you @ember-owl?"` drew replies from every responder, including one
that protested *"I'm Nova Sparrow, not Ember Owl, but…"* and answered anyway.
The visible contract is **directedness**: a `@`-mention of one persona is
answered by **exactly that persona**, while an un-addressed open-floor message
still admits everyone.

**Scope**: the default `planning` group channel (three personas — `ember-owl`
is `addressed`, `iron-fox` and `nova-sparrow` are `participant`), one human
prompt per case, and the Tier A directed-elsewhere filter the response gate
applies in [`agents/response_gate.py`](../../agents/response_gate.py)
(`reason="directed_elsewhere"`), with the candidate-set parity replicated in
[`internal/channels/fanout.go`](../../internal/channels/fanout.go) /
[`floor_control.go`](../../internal/channels/floor_control.go) so a
directed-elsewhere member is never queued into the floor round only to be
suppressed. Observed through the embedded web console Channel-timeline panel
with a REST cross-check for the per-sender reply counts.

**Out of Scope** — explicitly **v0.3.8 (Tier B)**, documented here but **not
asserted**:

- **No pile-on / "nothing to add" on open-floor messages.** Tier A admits *all*
  `participant`s on an un-addressed message and forwards them straight to the
  turn — there is no cheap salience bid yet, so a `participant` cannot choose to
  stay out because *"someone already said that"* or *"that's not my lane."* That
  suppression is the [Tier B salience bid](../rfcs/0030-amendment-relevance-gated-response.md#scope--v037--v038--v040)
  (v0.3.8), which depends on the in-round transcript RFC 0034 Phase 2 supplies
  this same release but is consumed only in v0.3.8.
- **Natural-language addressing** ("question only to Iron Fox" with no
  `@`-mention). v0.3.7 keys directedness on the structured `mentions` list only;
  free-text addressing is a v0.3.8 Tier-B salience signal.
- The `chair` disposition and per-disposition salience thresholds (the schema
  `threshold` field is reserved/no-op until v0.3.8).

Tier A also leaves the **floor-control** ordering (RFC 0030 Layer 2.5, v0.3.6)
and the **cascade-depth** backstop unchanged — they govern *order* and *volume*
of the members Tier A admits, not *who* is admitted; the mutually-aware ordered
round is covered by [MT-CHANNEL-GOV-002](MT-CHANNEL-GOV-002.md).

---

## Related Documentation

- [RFC 0030 Amendment — Relevance-Gated Response](../rfcs/0030-amendment-relevance-gated-response.md) — design; Tier A + the `respond_policy → disposition` reframe is the v0.3.7 slice, Tier B is v0.3.8
- [RFC 0030 relevance-gate PR plan](../rfcs/0030-amendment-relevance-gated-response-pr-plan.md) — this MT is PR 3's acceptance test
- [docs/guides/channels.md §"Per-membership `respond` dispositions"](../guides/channels.md) — the disposition vocabulary this MT exercises
- [docs/guides/web-console.md](../guides/web-console.md) — the `--enable-ui` console surface

**Related Automated Tests**:
- [`tests/unit/python/test_response_gate_relevance.py`](../../tests/unit/python/test_response_gate_relevance.py) — directed-elsewhere suppresses other `participant`s; the mentioned member is admitted; open-floor admits all `participant`s; `@everyone` disables the filter (D3); self-sender and `observer` always filtered; `addressed` still mention-gated
- [`tests/unit/python/test_response_gate_disposition.py`](../../tests/unit/python/test_response_gate_disposition.py) — disposition aliases resolve to the legacy gate constants (PR 1)
- [`tests/integration/test_channel_relevance.py`](../../tests/integration/test_channel_relevance.py) — the Trigger repro end-to-end: `@ember-owl` draws exactly one reply; open-floor admits all participants; a directed-elsewhere member still **ingests** (silent ≠ amnesiac)
- [`internal/channels/config_test.go`](../../internal/channels/config_test.go) — the disposition vocabulary loads and normalizes to the legacy values; an unknown value errors

---

## Preconditions

Same as [MT-CHANNEL-GOV-002 § Preconditions](MT-CHANNEL-GOV-002.md#preconditions)
(a valid API key in `.env` — the persona replies are real LLM calls; all three
demo personas up on the default compose stack), **plus**:

- ☐ The default [`config/channels.yaml`](../../config/channels.yaml) is
  unedited: the `planning` channel carries `ember-owl: addressed`,
  `iron-fox: participant`, `nova-sparrow: participant` (the mixed-disposition
  demo pattern). `floor_control: true` is the resolved group default (so the
  admitted members reply in order — orthogonal to this MT's directedness check).
- ☐ The orchestrator is started with `--enable-ui` so the console is served at
  `http://localhost:8080/ui`.
- ☐ State is clean (`make reset` or a fresh `PERSATRIX_EPOCH`) so prior-run
  participants do not steer the conversation.

Bring the stack up:

```bash
make reset
ENABLE_UI=1 docker compose up --build   # or `make run-ui` for the local-binary path
```

---

## Test Procedure

### Step 1: Confirm the channel's dispositions

**Action**: in the web console (`http://localhost:8080/ui`), open the
**Channels** panel and select **`group:planning`**. (Or via REST:
`Invoke-RestMethod http://127.0.0.1:8080/api/v1/channels/group:planning`.)

**Expected**:
- The channel exists with members `ember-owl` (`addressed`), `iron-fox`
  (`participant`), `nova-sparrow` (`participant`).

**Verification**:
- [ ] `group:planning` lists all three personas with the dispositions above.

---

### Step 2: A directed `@`-mention draws **exactly one** reply (the Trigger repro)

Post a question addressed to a single persona by `@`-mention. The two other
`participant` members must **not** answer — this is the defect Tier A fixes.

**Action** (console composer in the `group:planning` timeline, or CLI; the human
sender must be a member first — join once, then send):

```bash
./bin/persatrix channel join planning --as operator
./bin/persatrix channel send planning \
  "We need to pick a datastore for the v0.4 event log. @ember-owl what's your read?" \
  --as operator --mention ember-owl
```

**Expected**:
- **Exactly one** persona reply appears: `ember-owl` (it was the addressed
  member; `addressed` admits on a direct `@`-mention).
- `iron-fox` and `nova-sparrow` — both `participant` — stay **silent**: the gate
  suppresses them with `reason="directed_elsewhere"` (the message names a
  specific recipient that is not them, and it is not a broadcast).
- No persona protests "I'm not Ember Owl, but…" and answers anyway (the
  pre-amendment shape).

**Verification**:
- [ ] Only `ember-owl` replies; `iron-fox` and `nova-sparrow` do not.

---

### Step 3: An open-floor message admits all `participant`s

Post a question with **no** `@`-mention. With Tier B deferred to v0.3.8, every
`participant` reaches the turn (no salience suppression yet); the `addressed`
member stays out because it was not mentioned.

**Action**:

```bash
./bin/persatrix channel send planning \
  "Open question for the room: what's our biggest risk shipping v0.4?" \
  --as operator
```

**Expected**:
- Both `participant` members (`iron-fox`, `nova-sparrow`) reply — in a
  floor-controlled ordered round (Layer 2.5), but **both** reach the turn.
- `ember-owl` (`addressed`) stays **silent** — an un-addressed open-floor
  message does not mention it.
- **v0.3.8 expectation (NOT asserted here)**: under Tier B, a `participant` with
  nothing distinctive to add would *choose* to stay out — so this case would
  eventually draw *fewer* than two replies. In v0.3.7 it correctly draws both;
  do not treat two replies here as a defect.

**Verification**:
- [ ] `iron-fox` and `nova-sparrow` both reply; `ember-owl` does not.

---

### Step 4: An explicit broadcast disables the directed filter (D3)

`@everyone` is an explicit broadcast — Tier A must **not** suppress the
un-named `participant`s on it (adopted default, amendment OQ #5).

**Action** (mention `ember-owl` *and* broadcast, so all three are eligible):

```bash
./bin/persatrix channel send planning \
  "Standup, everyone: one line on what you're blocked on. @ember-owl you too." \
  --as operator --mention-all
```

> `--mention-all` resolves the channel roster and emits the `@everyone`
> broadcast sentinel; the gate treats its presence as "do not suppress."

**Expected**:
- All three personas reply (`ember-owl` because it is mentioned; `iron-fox` and
  `nova-sparrow` because the broadcast disables the directed-elsewhere filter).

**Verification**:
- [ ] All three personas reply on the broadcast (no `directed_elsewhere`
  suppression).

---

### Step 5: Confirm reply counts via REST

**Action**: pull the channel history newest-first after each case and count the
distinct persona senders that replied to each human message.

```pwsh
(Invoke-RestMethod `
  "http://127.0.0.1:8080/api/v1/channels/group:planning/messages?limit=20") `
  | Select-Object timestamp, sender_id, @{n='content';e={$_.content.Substring(0,[Math]::Min(60,$_.content.Length))}} `
  | Format-Table -Auto
```

**Expected**:
- The directed `@ember-owl` prompt (Step 2) is followed by **one** persona
  message (`ember-owl`).
- The open-floor prompt (Step 3) is followed by **two** persona messages
  (`iron-fox`, `nova-sparrow`).
- The broadcast prompt (Step 4) is followed by **three** persona messages.

**Verification**:
- [ ] Reply counts are 1 / 2 / 3 for the directed / open-floor / broadcast cases.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | `group:planning` lists `ember-owl: addressed`, `iron-fox`/`nova-sparrow`: `participant` | ☐ |
| 2 | A directed `@ember-owl` prompt draws **exactly one** reply (`ember-owl`); the two `participant`s stay silent | ☐ |
| 3 | An open-floor prompt admits both `participant`s; `ember-owl` stays silent (Tier B no-pile-on is v0.3.8, not asserted) | ☐ |
| 4 | An `@everyone` broadcast disables the directed filter — all three reply (D3) | ☐ |
| 5 | REST history confirms reply counts 1 / 2 / 3 (directed / open-floor / broadcast) | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: A directed-elsewhere persona still **remembers**

The gate decides *whether to respond*, not *whether to remember*. A persona
gated `directed_elsewhere` is silent but **still ingests** the message into its
conversation window / memory — so a later turn that *does* address it can build
on what it saw. (Deterministically pinned by
`test_channel_relevance.py::test_directed_elsewhere_member_still_remembers`.)
To spot-check live: after Step 2, `@`-mention `iron-fox` with a referential
follow-up ("what did you make of that datastore question?") and confirm it
recalls the Step-2 prompt it never answered.

### Edge Case 2: Back-compat — legacy `respond` values still load

The disposition vocabulary is additive. Editing a member back to the legacy
`always` / `when_mentioned` / `never` must load unchanged (normalized to
`participant` / `addressed` / `observer` at the Go config boundary) and behave
identically. Asserted at the config layer by `internal/channels/config_test.go`;
useful as a side-check that an existing operator's `config/channels.yaml` is not
broken by the reframe.

### Edge Case 3: A DM is unaffected

A `dm:` channel is single-responder by construction — there is no second
`participant` to suppress, so Tier A is a no-op. A direct chat with one persona
answers every turn exactly as before.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| _pending_ | | | ☐ | To be executed live at v0.3.7 release-prep (master-plan Phase 3) on the RC tip. |
| 2026-06-06 | Claude (Opus 4.8) | macOS (Darwin 24.6.0) | ⚠️ **Steps 1–3 + idle-cost Pass; Step 4 (D3) Fail** | Live on Anthropic, RC tip `65303d7`. Directed→1, open→2, suppressed members zero-cost. **Step 4 `@everyone` broadcast draws 0 replies + 135 s publish block** — root-caused to `agents/channel_validation.py` rejecting the `@everyone` sentinel before the gate: [ISSUE-0094](../issues/ISSUE-0094-everyone-broadcast-rejected-by-agent-inbound-validation.md). See [v0.3.7-execution-report.md](v0.3.7-execution-report.md). |

---

## Notes

- Tier A is **free**: no LLM call, no memory recall — the directed-elsewhere
  decision is a list membership check on `mentions` in the response gate, run
  *before* any provider call. A suppressed persona costs **zero** tokens (the
  RFC 0023/0024 idle-cost invariant, re-asserted by the cost-regression gate).
- The deterministic guarantees (which dispositions are admitted/suppressed in
  each case) are pinned by the unit + integration suites listed above; this MT
  validates that the mechanism produces the intended *conversational* effect
  end-to-end with live personas.
- The "no pile-on" half of the realism story is **Tier B (v0.3.8)** — recorded
  in Step 3's expected-results note so a future tester does not mistake the
  v0.3.7 open-floor admit-all for a regression.
