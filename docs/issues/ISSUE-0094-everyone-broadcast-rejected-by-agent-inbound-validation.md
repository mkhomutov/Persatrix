---
id: ISSUE-0094
summary: "F-8 — the `@everyone` broadcast sentinel (RFC 0030 relevance amendment Tier A, decision D3) is rejected by the agent-side inbound channel-message validator. The orchestrator special-cases `@everyone` everywhere it matters server-side (`channels.MentionEveryone`, the floor-control `orderResponders` directed-filter bypass, `sqlite_messages.go` mention persistence) and the Python receiver gate special-cases it (`response_gate.MENTION_EVERYONE`), but `agents/channel_validation.py` validates every element of the inbound `mentions` list against the participant-id regex `^[a-z0-9][a-z0-9-]*[a-z0-9]$` with NO sentinel carve-out. `@everyone` fails the regex, so the whole inbound CHANNEL_MESSAGE is rejected at the envelope boundary and dropped BEFORE the response gate runs. Every persona on a broadcast therefore stays silent; with floor control on, the publish blocks for N×45s (one per candidate turn timeout) and returns zero replies. Surfaced live during v0.3.7 release-prep MT execution (MT-CHANNEL-RELEVANCE-001 Step 4). The unit/integration gates pass because they exercise `orderResponders` / `should_respond` with synthetic mentions lists and never push an `@everyone` envelope through `validate_channel_message`."
status: open
severity: high
area: agents
created: 2026-06-06
closed:
closed_pr:
refs:
  - docs/manual-tests/MT-CHANNEL-RELEVANCE-001.md
  - docs/manual-tests/v0.3.7-execution-report.md
  - docs/rfcs/0030-amendment-relevance-gated-response.md
  - agents/channel_validation.py
  - agents/response_gate.py
  - agents/participant.py
  - internal/channels/channels.go
  - internal/channels/floor_control.go
  - internal/channels/sqlite_messages.go
---

## Context

RFC 0030 relevance amendment **Tier A** (v0.3.7) makes a directed `@`-mention
draw a reply from exactly the addressee. **Decision D3** is the broadcast escape
hatch: a message carrying the `@everyone` sentinel disables the
directed-elsewhere filter so every `participant` is admitted again. This is the
contract [MT-CHANNEL-RELEVANCE-001](../manual-tests/MT-CHANNEL-RELEVANCE-001.md)
Step 4 asserts and that the `--mention-all` CLI flag exists to drive.

Found live during the v0.3.7 release-prep manual-test execution
([v0.3.7-execution-report.md](../manual-tests/v0.3.7-execution-report.md), PR 1):
**an `@everyone` broadcast draws zero persona replies and blocks the publish for
~135 s.**

## What the investigation found

The `@everyone` sentinel is special-cased on **every** path *except* the agent's
inbound envelope validator:

| Layer | File | `@everyone` handled? |
|-------|------|----------------------|
| Sentinel constant (Go) | `internal/channels/channels.go` — `MentionEveryone = "@everyone"` | ✅ |
| Floor-control candidate set | `internal/channels/floor_control.go` — `directed := len(Mentions) > 0 && !mentioned[MentionEveryone]` | ✅ admits all on broadcast |
| Mention persistence | `internal/channels/sqlite_messages.go` — `if mention == MentionEveryone { … }` | ✅ |
| Receiver response gate (Py) | `agents/response_gate.py` — `MENTION_EVERYONE` in both the `POLICY_ALWAYS` and mentioned branches | ✅ admits all on broadcast |
| **Agent inbound validation (Py)** | **`agents/channel_validation.py`** — `for i, m in enumerate(request.mentions): if not _CHANNEL_PARTICIPANT_ID_RE.match(m): return "mentions[i] is not a valid participant id…"` | ❌ **no carve-out** |

`_CHANNEL_PARTICIPANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")`
(`agents/channel_validation.py`, mirrors `agents/participant.py`). `@everyone`
starts with `@` and does not match, so `validate_channel_message` returns a
rejection tuple and the persona runtime drops the inbound CHANNEL_MESSAGE
**before** the response gate is consulted. The gate that would correctly admit
the message (D3) never runs.

Because the validator rejects on the **first** offending element, a broadcast is
dropped for **every** recipient — including a persona that is *also* explicitly
named in the same `mentions` list (e.g. `["ember-owl", "@everyone"]`): the
`@everyone` element trips the validator before the persona's own id is honoured.

### Live repro (v0.3.7 RC tip, Anthropic stack, `group:planning`)

```
mentions=["ember-owl","@everyone"]  → 0 persona replies; POST /messages latency 135.08 s (201)
mentions=["ember-owl","iron-fox","nova-sparrow"] (explicit, no @everyone) → 3 replies in ~5 s
mentions=[] (open floor)            → 2 replies (iron-fox, nova-sparrow) in ~6 s
```

The 135 s = three floor-round candidate turns each consuming the canonical
`DefaultFloorTurnTimeoutSeconds = 45` because every dispatched persona silently
dropped the message. No orchestrator-side `channels: dispatch failed` warning
fires — the gRPC dispatch itself succeeds; the message is discarded agent-side
at validation.

## Impact

- **The D3 broadcast escape hatch is non-functional end-to-end.** `@everyone` /
  `--mention-all` produces silence, the opposite of the documented contract.
- **A floor-controlled broadcast hangs the publish path for N×45 s** (N =
  candidate count) and returns 201 with zero replies — a latent
  availability/UX problem for any operator or console that posts a broadcast.
- This is a **v0.3.7 release-gate miss**: MT-CHANNEL-RELEVANCE-001 Step 4 fails.

## Why the automated gates did not catch it

The directedness gates assert the *decision*, not the *delivery*:
`internal/channels` `TestOrderResponders_BroadcastDisablesDirectedFilter` and
`tests/unit/python/test_response_gate_relevance.py` build a `mentions` list in
memory and call `orderResponders` / `should_respond` directly. Neither pushes an
`@everyone` envelope through `agents/channel_validation.validate_channel_message`,
which is the layer that rejects it. There is **no test that delivers an
`@everyone` broadcast through the agent inbound boundary.**

## Suggested fix (for a follow-up `fix/v037-` PR — not this report PR)

Skip the participant-id regex for the broadcast sentinel in
`agents/channel_validation.py` (e.g. `if m == MENTION_EVERYONE: continue` before
the regex check, importing the existing `response_gate.MENTION_EVERYONE`
constant so the carve-out stays single-sourced), and add the missing coverage:

1. A unit test in `tests/unit/python/` asserting `validate_channel_message`
   accepts a `mentions` list containing `@everyone` (alone and alongside a real
   id), and still rejects a genuinely malformed id.
2. An end-to-end / integration test that delivers an `@everyone` broadcast
   through the inbound path and asserts the gate admits (the missing
   delivery-layer assertion).

Forward-only; no schema or migration involvement.
