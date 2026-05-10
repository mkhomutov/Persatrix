# Channels — User Guide

A practical walkthrough of the v0.3.0 internal channels surface: declaring a
channel in config, joining it as a human user, posting and reading messages,
and the response policies that shape who replies to what.

> **Spec-level detail** lives in [RFC 0011](../rfcs/0011-channels-bridges.md)
> (channel model + REST + memory integration) and the
> [chat-as-DM amendment](../rfcs/0011-amendment-chat-as-dm.md) that unified
> v0.2.1 chat under the channels wire model. This guide is deliberately
> non-exhaustive and points into the RFC for design rationale.

> **Chat is a DM in v0.3.0.** `POST /api/v1/agents/{id}/chat` and
> `persatrix chat <agent>` no longer ride a separate `SendChatMessage` gRPC
> path — both ride the channels publish-and-await loop with a canonical
> `dm:<lex-sorted>` channel id. The JSON contract is preserved, but the
> persistence and memory paths are now the same as for any other channel
> message — see the [chat-as-DM amendment](../rfcs/0011-amendment-chat-as-dm.md).

> **On-startup catch-up replay.** Persona agents fetch the last 50 messages
> per channel they are a member of after self-registration and ingest them
> as `CHANNEL_MESSAGE` events with `metadata["replay_mode"] = True`. Replay
> events ingest into memory but **suppress outbound `SEND_CHANNEL_MESSAGE`**
> so a restart does not blast everyone with stale responses. The
> `channel.messages.replayed{channel_id}` counter is the contract pin —
> separate from `channel.messages.gated` so a startup catch-up burst does
> not mask a real gate-suppression spike. See §6 *Missed-message recovery*
> below and [RFC 0011 OQ #8](../rfcs/0011-channels-bridges.md#open-questions).

> **Rust CLI subcommand reference.** Every `persatrix channel …` flag is
> sourced from [`cli/src/commands/channel.rs`](../../cli/src/commands/channel.rs).
> Use that file (not this guide) when you need the authoritative flag
> grammar, exit-code contract, or canonicalisation rule for a bare channel
> name.

---

## 1. The shape of a channel

A channel is a named, persistent message bus between participants — agents,
human users, or a mix. Three channel types cover the v0.3.0 use cases:

| Type | Address | Membership | Use case |
|------|---------|------------|----------|
| `group` | `group:<name>` | Configured in `config/channels.yaml` (or created via REST) | Multi-party discussion, shared logs, planning channels |
| `dm` | `dm:<a>:<b>` (lexicographically sorted) | Implicit — the two named participants only | Point-to-point human↔agent or agent↔agent exchanges |
| `thread` | `thread:<message_id>` | Inherits from the parent channel | Threaded reply branches under a top-level message |

Persona-to-persona DMs and human-to-agent chats both ride this surface — chat
is just a DM with a `UserParticipant` on one end (see
[chat-as-DM amendment](../rfcs/0011-amendment-chat-as-dm.md)).

Caps are conservative on purpose — 50 named group channels (`max_channels`),
10 000 messages per channel (oldest pruned first), and 10 mentions per publish.
Tune via [`config/channels.yaml`](../../config/channels.yaml).

---

## 2. Declaring channels in config

Channels can be declared up-front in [`config/channels.yaml`](../../config/channels.yaml)
or created at runtime via REST. Declared channels are loaded at orchestrator
startup; runtime divergence between config and store is **loud failure**
(orchestrator refuses to start, listing the divergent participant ids — RFC
0011 §B).

```yaml
max_channels: 50

channels:
  - name: planning
    description: "Strategy and planning discussions"
    members:
      - id: ember-owl
        respond: when_mentioned
      - id: alice                 # human users may join here too
        respond: when_mentioned

  - name: code-review
    description: "Tight-loop pair channel"
    members:
      - id: code-writer
        respond: always
      - id: code-reviewer
        respond: always
```

Run `make validate` after any edit. The schema is
[`schemas/channel.schema.json`](../../schemas/channel.schema.json) — note its
top-level disclaimer: *"Internal-only schema until v1.0; `$id` may break across
v0.x bumps without notice."*

### Per-membership `respond` policies

The response gate fires per-event in the persona runtime (RFC 0011 §D). Pick
the policy that matches each member's role in the channel:

- **`when_mentioned`** *(default)* — agent replies only when its id appears
  in `mentions[]`. Quiet by default; cuts through on `@`-mention.
- **`always`** — agent replies to every message it ingests. Reserve for
  tight-loop pairs (e.g. writer ↔ reviewer); each `always` member multiplies
  fanout linearly.
- **`never`** — agent ingests history into memory but does not reply.
  Listener role for broadcast / announcement channels.

Channel-level patterns (Quiet group / Tight-loop pair / Broadcast /
Incident) compose from member-level policies — see RFC 0011 §H for the
table. RFC 0011 §H labels the all-`always` pattern "Always-respond / incident";
this guide uses **Incident** consistently to name the role rather than the
implementation.

---

## 3. Joining and posting as a human user

### CLI

```bash
# Discovery
persatrix channel list
persatrix channel list --json

# Membership
persatrix channel join planning --as alice
persatrix channel join planning --as alice --respond when_mentioned

# Posting
persatrix channel send planning "Anything blocking the Q3 plan?" \
    --as alice --mention ember-owl

# Threaded replies
persatrix channel reply planning <parent-message-id> "Got it, will draft" \
    --as alice

# Reading
persatrix channel history planning --limit 20
persatrix channel history planning --limit 20 --json

# Live tail (polling, 5 s default — see [OQ #4](../rfcs/0011-channels-bridges.md#open-questions))
persatrix channel watch planning --interval 2
persatrix channel watch planning --interval 2 --json
```

Bare names (e.g. `planning`) canonicalize to `group:<name>` client-side
(`canonicalize_channel_id` in [channel.rs](../../cli/src/commands/channel.rs)).
Fully-qualified ids (`group:planning`, `dm:alice:bob`) pass through unchanged.

`--mention <id>` is repeatable; `--mention-all` resolves the channel's current
member list locally and drops the sender — useful when you want every other
member to see the message but do not want to type each id.

`--json` everywhere matches `schemas/channel.schema.json` so
`persatrix channel history --json | jq` is portable.

### REST

The CLI is a thin wrapper around the REST surface. Direct REST is exactly
equivalent — useful for automation, CI smoke tests, and operators who prefer
`curl` / `Invoke-RestMethod`.

```http
POST   /api/v1/channels                                  # create channel
GET    /api/v1/channels                                  # list
GET    /api/v1/channels/{id}                             # one channel + members
POST   /api/v1/channels/{id}/members                     # add member
DELETE /api/v1/channels/{id}                             # delete + cascade
DELETE /api/v1/channels/{id}/members/{participant_id}    # remove one member
POST   /api/v1/channels/{id}/messages                    # publish
GET    /api/v1/channels/{id}/messages                    # history (newest-first)
GET    /api/v1/channels/{id}/messages/{message_id}/thread # thread under a parent
```

Endpoint shapes match the [RFC §C endpoint table](../rfcs/0011-channels-bridges.md#c-message-routing-and-delivery).

---

## 4. The response gate: who replies, and when

When a message is published, the orchestrator fans it out via gRPC
`ReceiveChannelMessage` to every channel member except the sender. Each
receiving agent runs the event through the **response gate** (RFC 0011 §D)
*before* memory recall or any LLM call:

```
event arrives → cascade-depth check (drop if depth ≥ max_cascade_depth)
              → respond_policy lookup
              → admit if (policy == "always") OR
                       (policy == "when_mentioned" AND agent.id ∈ mentions)
              → suppressed events still ingest into memory (Phase 3)
                but do not trigger an LLM action
```

The cascade-depth check is **not policy-conditional** — it fires before the gate
in `EventDispatcher.dispatch` and applies to every event regardless of
`respond_policy` ([agents/response_gate.py:43-49](../../agents/response_gate.py#L43-L49)).
The "Cascade-depth backstop" subsection below covers the operator-facing detail.

Suppressed events increment `channel.messages.gated{policy=…}` — that counter
is the primary signal for an under- or over-tuned policy.

### Human gate-bypass — option (b) in v0.3.0

A casual human message in a multi-agent channel produces no agent reply unless
explicitly `@`-mentioned. This is **option (b)** from
[RFC 0011 OQ #7](../rfcs/0011-channels-bridges.md#open-questions): humans are
subject to the same gate as agents and must `@`-mention. The reasoning:

- silent agents in a DM-shaped channel are recoverable (the human can
  re-prompt with `--mention`);
- flooding every agent on every casual message is not.

For broadcast cases, use `persatrix channel send … --mention-all` — the CLI
expands client-side to every member except the sender.

### Cascade-depth backstop

`always` members can theoretically loop indefinitely. The dispatcher caps any
single chain of reactive replies at `max_cascade_depth` (default 5) — beyond
that, the next event is dropped silently (debug log only). External re-triggers
(a tick, a non-channel event, a human message) reset the counter. RFC 0011 §H
elaborates on the patterns this enables.

---

## 5. Memory integration

Channel messages route through `InteractionTracker.add_turn` (RFC 0020 §G)
rather than per-event episodic writes. On interaction close, exactly one
episodic entry is written per *interaction* — not per message. The episode's
tags carry the channel id and participant ids so later recall can scope by
either dimension.

The persona-runtime memory injector then feeds channel history into
`MemoryBudget.try_add` between the relationship and episodic tiers (RFC 0011 §E
+ RFC 0008). The cross-RFC priority order is pinned by
[`tests/unit/python/test_memory_context_priority_order.py`](../../tests/unit/python/test_memory_context_priority_order.py)
— if you change the order, that test must change too.

Per-channel recall scoping uses a tag filter on the shared
`recall_with_scope_filter` helper, so an agent in many channels does not pull
unrelated history into its prompt for a single-channel turn.

---

## 6. Missed-message recovery

At-most-once delivery means an offline agent misses messages while down. v0.3.0
ships **on-startup catch-up fetch** (RFC 0011 [OQ #8](../rfcs/0011-channels-bridges.md#open-questions)):

- After startup + self-registration, each persona agent fetches the last 50
  messages per channel it is a member of via REST.
- Fetched messages flow through the same ingest path (`_store_event_episode`)
  so they land in memory with the correct interaction shape — but the agent
  runs in **replay mode** for those events: outbound `SEND_CHANNEL_MESSAGE`
  actions are suppressed so the agent does not blast everyone with stale
  responses on restart.
- The `channel.messages.replayed{channel_id=…}` counter pins the contract.

Watermark-based catch-up (`?since=<message_id>` per-channel, per-subscriber)
and per-tick recovery are deferred to v0.3.x once operational data justifies —
the on-startup last-50 form is the v0.3.0 best-effort recovery contract.

---

## 7. Concurrent publish ordering

If two agents publish to the same channel within milliseconds, the SQLite
serial write guarantees a **consistent history** — both messages persist in
some order — but each agent may generate a response without seeing the other's
message (the race window between write and gRPC fanout). This is acceptable
for v0.3.0 conversational use; if your workflow requires strict
read-your-writes-before-replying ordering, treat the channel as advisory and
gate the action externally. RFC 0011 [OQ #5](../rfcs/0011-channels-bridges.md#open-questions)
documents this.

---

## 8. Trust boundary (v0.3.0)

The channels REST surface is **unauthenticated** in v0.3.0. The orchestrator
emits a one-shot `WARN` at startup whenever the channels subsystem is enabled
([cmd/orchestrator/channels.go](../../cmd/orchestrator/channels.go)):

```
channels: REST surface is UNAUTHENTICATED in v0.3.0 — sender_id is
body-trusted; firewall the port or front with an authenticating reverse
proxy. Auth lands in RFC 0009 Phase 4.
```

The warning is intentionally **not suppressible from config** — an opt-out
would defeat its purpose. It fires once per startup so the trust boundary is
impossible to miss in the operator's first log scrape.

Token auth lands in [RFC 0009 Phase 4](../rfcs/0009-security-sandboxing.md)
(deferred to v0.4.0). Until then:

- Run the orchestrator behind your own ingress / auth proxy on shared
  networks.
- Bind the listener to `127.0.0.1` or firewall the port if exposing it
  externally is not strictly required.
- Rate-limit middleware is wired generically (RFC 0009 PR 2 / PR #244) but
  the channels publish endpoint runs on the startup-WARN path for v0.3.0 —
  see [RFC 0011 PR plan PR 2](../rfcs/0011-pr-plan.md#pr-2-featurev030-rfc0011-rest-routing--phase-1b-rest--router--config).

---

## 9. Common channel patterns

The four canonical patterns from RFC 0011 §H, each one configuration shape:

| Pattern | Members + policies | Why |
|---------|---------------------|-----|
| Quiet group | All `when_mentioned` | Default for general-purpose channels (3+ agents). Lowest fanout. |
| Tight-loop pair | Both `always` (N=2) | Two collaborators in continuous exchange (writer ↔ reviewer). |
| Broadcast | Senders publish, listeners `never` | One-way log channel; listeners ingest history but do not reply. |
| Incident | All `always` | Multi-agent channel where every message warrants attention; reserve for the rare case. |

Mixed-policy channels are legitimate — e.g. a planning channel with two
`always` collaborators plus a `when_mentioned` advisor.

---

## 10. What's deferred to v0.4.0+

Documented here so the gap is visible — these are RFC 0011 non-goals or
deferrals, not implementation oversights:

- **External bridges** (Slack, Discord, email) → v0.5.0.
- **SSE streaming** for `channel watch` (vs. 5 s polling) → v0.3.x; see
  [OQ #6](../rfcs/0011-channels-bridges.md#open-questions).
- **Token auth** on the REST surface → RFC 0009 Phase 4 (v0.4.0).
- **Watermark + per-tick catch-up** → v0.3.x once usage data justifies; see
  [OQ #8](../rfcs/0011-channels-bridges.md#open-questions).
- **Per-channel `cascade_depth` overrides** → v0.3.x; see
  [OQ #11](../rfcs/0011-channels-bridges.md#open-questions).
- **Persona name discovery / dynamic membership** → v0.4.0 (RFC 0011 OQ #1).

---

## 11. Manual tests

The channels surface is exercised end-to-end against a docker-composed
orchestrator + four agents via the MT-CHANNEL series:

- [MT-CHANNEL-001](../manual-tests/MT-CHANNEL-001.md) — `list` / `join` CLI
- [MT-CHANNEL-002](../manual-tests/MT-CHANNEL-002.md) — `send` / `reply` / `history` CLI
- [MT-CHANNEL-003](../manual-tests/MT-CHANNEL-003.md) — `watch` polling + dedup
- [MT-CHANNEL-004](../manual-tests/MT-CHANNEL-004.md) — human-mentions-agent end-to-end (live LLM)
- [MT-CHANNEL-005](../manual-tests/MT-CHANNEL-005.md) — DM canonicalization round-trip
- [MT-CHANNEL-006](../manual-tests/MT-CHANNEL-006.md) — channel deletion + cascade

---

## Related documentation

- [RFC 0011](../rfcs/0011-channels-bridges.md) — full channel spec
- [RFC 0011 amendment — chat-as-DM](../rfcs/0011-amendment-chat-as-dm.md) — v0.2.1 chat unified under channels
- [RFC 0008](../rfcs/0008-agent-memory-context-optimization.md) — `MemoryFacade` / `MemoryBudget` (used by channel-history injection)
- [RFC 0020](../rfcs/0020-interaction-lifecycle.md) — `InteractionTracker` (channel turns route through `add_turn`)
- [Persona agents user guide](persona-agents.md) — persona-side concepts the channel surface integrates with
- [System overview diagram](../diagrams/system-overview.md) — where the channels package sits in the runtime
