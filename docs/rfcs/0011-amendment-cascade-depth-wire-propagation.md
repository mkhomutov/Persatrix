# RFC 0011 Amendment — Cascade-Depth Wire Propagation

**Type**: amendment to [RFC 0011](0011-channels-bridges.md) §D
**Status**: ✅ Adopted
**Date**: 2026-05-11
**Trigger**: Manual end-to-end testing of the v0.3.0 channels stack with two `always`-respond personas in a shared `group:planning` channel — a single user prompt produced ~60 persona replies in ~10 minutes and was still firing when halted ([docs/v0.3.0-test-findings-pr-plan.md](../v0.3.0-test-findings-pr-plan.md) finding F-1).
**Supersedes**: RFC 0011 §D "Composition with `cascade_depth` and rate limiting" → *"Outbound `SEND_CHANNEL_MESSAGE` actions carry `cascade_depth + 1`, exactly as the dispatcher already does for `MESSAGE_RECEIVED`. **No change to the cascade mechanism is required.**"*

---

## Context

RFC 0011 §D positions [`EventDispatcher.max_cascade_depth=5`](../../agents/dispatch.py#L43) as a defense-in-depth backstop layered behind the per-membership response gate. The cited mechanism is `metadata["cascade_depth"]`, incremented by the agent-side dispatcher as it forwards events into the next action loop. The RFC concluded *"No change to the cascade mechanism is required"* because the existing in-process dispatcher already incremented and enforced the field.

That conclusion was wrong: the cascade chain is no longer in-process once a publish leaves the agent. The publish→fanout→dispatch path crosses two wire boundaries:

1. **Agent → orchestrator (REST `POST /api/v1/channels/{id}/messages`)** — body is [publishMessageRequest](../../internal/server/channel_types.go#L36); its `metadata` field is the only general-purpose seat for cross-cutting fields.
2. **Orchestrator → agent (gRPC `AgentService.ReceiveChannelMessage` carrying [`ChannelMessageEvent`](../../proto/task.proto#L123))** — fields 1–10 are all typed scalars; the message has no metadata map.

Neither boundary carries `cascade_depth`. The agent sets `cascade_depth=depth+1` on the executor kwarg ([agents/dispatch.py:162](../../agents/dispatch.py#L162)) and the executor writes it into the REST publisher's call args ([agents/action_executor.py:336](../../agents/action_executor.py#L336)), but the field is then dropped: `HTTPChannelPublisher.publish` does not forward it into the POST body, the Go orchestrator's publish handler has no place to read it from, and the outbound gRPC `ChannelMessageEvent` has no field to put it in. On every cross-process hop the chain resets to depth `0`. The agent-side dispatcher's `max_cascade_depth=5` check then never fires across that boundary, and the cascade is bounded only by the per-membership response gate — which `always` members structurally defeat.

This amendment pins the wire contract that closes that loop. It does **not** change the agent-side dispatcher's cap or the response gate; it adds the wire fields the existing logic was already assuming were present.

## The amended contract

The wire-level encoding of `cascade_depth` is deliberately asymmetric across the two boundaries, mirroring the existing [`timestamp` divergence at task.proto:141-148](../../proto/task.proto#L141-L148):

### REST (orchestrator publish)

`POST /api/v1/channels/{id}/messages` uses the existing `metadata: map[string]any` seat on [publishMessageRequest](../../internal/server/channel_types.go#L36) — **no struct-field change.** Publishers set the key `metadata.cascade_depth` (integer); the seat already round-trips into [ChannelMessage.Metadata](../../internal/channels/channels.go#L89). The JSON-schema contract for this key is pinned in [`schemas/channel.schema.json`](../../schemas/channel.schema.json) under `definitions.messageMetadata.cascade_depth`.

### gRPC (orchestrator → agent fanout)

`ChannelMessageEvent` gains a new typed scalar:

```protobuf
int32 cascade_depth = 11;  // proto/task.proto
```

Typed rather than carried in an ad-hoc metadata map because `ChannelMessageEvent` has no metadata map in v0.3.0 (fields 1–10 are all typed scalars); adding the map purely for one integer is a strictly larger surface than adding the integer. The REST/proto asymmetry is intentional and mirrors the `timestamp` divergence already documented at [task.proto:141-148](../../proto/task.proto#L141-L148).

### Trust model

`cascade_depth` is **partially untrusted** input from a (possibly misbehaving) publisher.

- **Inbound clamp.** The orchestrator MUST clamp inbound `cascade_depth` to `[0, max_cascade_depth]` at the publish boundary (PR 2 of this plan). This defends against over-cap poisoning — a publisher cannot bypass the cap by setting `cascade_depth=99` to force an early drop on a downstream branch it dislikes, nor can it skip its own depth accounting by emitting a value the orchestrator would treat as past-cap.
- **What the clamp does not defend against.** A publisher can emit `cascade_depth=0` on every publish, resetting the chain to depth `0` indefinitely. There is no orchestrator-side signal to detect that without correlating the publish to its parent message. Closing this hole requires authoritative depth derivation — see *Future work* below. The amendment is explicit that the clamp's scope is over-cap poisoning, not reset-to-0; the latter is a separate workstream gated on the parent-message lookup landing.

### Primary vs. defense-in-depth enforcement

RFC 0011 §D's "Composition with `cascade_depth`" framing called the agent-side dispatcher the cap's enforcement point. With the cross-process gap closed, the conceptual primary enforcement moves to the orchestrator:

- **Primary** — Go orchestrator's fanout path drops events when inbound `cascade_depth >= max_cascade_depth`. Sits on the trust boundary (agents are downstream consumers; the orchestrator is the only point that sees every cascade hop in one place).
- **Defense-in-depth** — Python `EventDispatcher.max_cascade_depth=5` check at [agents/dispatch.py:108-114](../../agents/dispatch.py#L108-L114) remains. It catches the legacy in-process mention cascade (which never crosses the orchestrator) and any wire-side regression that lets a depth-violating event reach an agent. The [response_gate.py:43-49](../../agents/response_gate.py#L43-L49) docstring is updated in PR 3 of this plan to reflect this re-framing.

The two enforcement points share one conceptual cap — the default `max_cascade_depth=5` must stay aligned between [agents/dispatch.py:43](../../agents/dispatch.py#L43) and the orchestrator-side `internal/defaults/defaults.go` knob landed in PR 2 — so the backstop fires only on a genuine primary-enforcement failure, not on routine cap-bound traffic.

### Increment site

The `+1` stays **agent-side, on outbound** ([agents/dispatch.py:162](../../agents/dispatch.py#L162) and the executor → publisher chain). The orchestrator forwards the inbound depth as-is into child fanout events. A naive "orchestrator increments on fanout" implementation would fire the cap one hop earlier than RFC §D's stated "5" — a future reviewer must not restore the double-increment.

This means: a publish arriving at the orchestrator with `cascade_depth=D` represents *"this event lives at depth D"*. The orchestrator's cap check is `inbound_depth >= max_cascade_depth → drop`; child events emitted into fanout carry `cascade_depth=D` unchanged. The next agent's executor, on its outbound, will increment to `D+1`.

## What this amendment does NOT change

- The per-membership response gate (RFC 0011 §D) remains the **primary structural** defense against runaway cascades. This amendment only closes the wire gap that lets the cascade backstop function across processes — it does not replace the gate.
- The `max_cascade_depth=5` value is unchanged. Both enforcement points use the same cap.
- The `EventType` / `ActionType` rename from the [chat-as-DM amendment](0011-amendment-chat-as-dm.md) is unaffected.
- Cost-ceiling enforcement (the *"single user prompt = ~160 LLM calls"* tail of finding F-1) is **out of scope.** Even at cap=5, a fully populated channel still pays `members × depth` LLM calls per publish. That gate is its own follow-up — see [v0.3.0 test-findings PR plan §"Cost-ceiling enforcement"](../v0.3.0-test-findings-pr-plan.md#future-prs).

## Implementation sequencing

Land in the PR sequence pinned by [docs/v0.3.0-test-findings-pr-plan.md](../v0.3.0-test-findings-pr-plan.md):

1. **PR 1 (this PR)** — amendment doc + schema/proto changes. No behavior change: Go still ignores the field; Python still does not emit it. Wire compatibility only.
2. **PR 2** — Go orchestrator reads, clamps, propagates, enforces the cap on fanout. Surfaces `max_cascade_depth` as a config knob and emits the structured-log drop line.
3. **PR 3** — Python publisher emits `metadata.cascade_depth` on POST, gRPC servicer ingests the typed field into `AgentEvent.metadata["cascade_depth"]`. Python dispatcher cap demoted to defense-in-depth in its docstring.
4. **PR 4** — cross-process integration test that fails if any of PRs 1–3 silently regresses.

Splitting PR 1 from PR 2/3 is deliberate: the schema and proto changes are the load-bearing contract reviewers need to evaluate against the design, and both subsequent PRs cite this amendment from their commit messages.

## Future work

- **Authoritative depth derivation.** PRs 1–4 bound the cooperative-path cascade but do not defend against a publisher resetting `cascade_depth=0` on every publish. Complete fix: extend the publish surface with `published_in_reply_to: message_id`, look up the parent message in `ChannelStore`, use `parent.depth + 1` regardless of publisher claim. Same lookup shape as the existing `thread_parent_sender_id` resolution at [router.go:222-236](../../internal/channels/router.go#L222-L236). Tracked in [v0.3.0 test-findings PR plan §Future PRs](../v0.3.0-test-findings-pr-plan.md#future-prs).
- **Cost-ceiling enforcement.** See above. Independent of this amendment; cap=5 still permits `members × depth` LLM calls per publish on a fully populated channel.

## Glossary

See [`docs/ai-glossary.md`](../ai-glossary.md) → `Cascade Depth` (existing entry; the amendment's wire-propagation rule lands in PR 2's docs update).
