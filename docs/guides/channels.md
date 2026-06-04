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

`always` members can theoretically loop indefinitely. v0.3.0 manual testing
(finding F-1 in [`docs/v0.3.0-test-findings-pr-plan.md`](../v0.3.0-test-findings-pr-plan.md))
showed that a single user prompt in a two-`always`-member channel produced
~60 persona replies in ~10 minutes — the cascade backstop was being reset
to 0 on every cross-process publish boundary. The [RFC 0011 amendment
'Cascade-depth wire propagation'](../rfcs/0011-amendment-cascade-depth-wire-propagation.md)
closes that gap with two enforcement points sharing one conceptual cap:

- **Primary — orchestrator-side, on the publish trust boundary.** The
  Go router (`internal/channels`) reads `metadata.cascade_depth` on
  every inbound publish, clamps it to `[0, max_cascade_depth]`, and
  drops the fanout when depth ≥ cap. The publish itself still
  succeeds (the publisher sees a 2xx); only the cascade chain is
  terminated. Default cap is **5**; operators override via
  `max_cascade_depth:` in `channels.yaml`.
- **Defense-in-depth — agent-side, in the Python dispatcher.** The
  `EventDispatcher.max_cascade_depth=5` check at
  [`agents/dispatch.py`](../../agents/dispatch.py) remains as a
  backstop for the legacy in-process mention cascade and any wire-side
  regression that lets a depth-violating event reach an agent.

The two caps MUST stay aligned — they are one conceptual ceiling with
two enforcement points, not two independent budgets.

External re-triggers (a tick, a non-channel event, a human message)
reset the counter to 0. RFC 0011 §H elaborates on the patterns this
enables.

**Operator-facing telemetry**

- `channel.messages.cascade_capped{channel_type}` — one increment per
  per-recipient dispatch the cap suppressed. Directly comparable to
  `channel.messages.delivered{channel_type, status}`; a cap-rate panel
  reads `cascade_capped / (cascade_capped + delivered)`.
  `channel_id` is intentionally **not** a label (cardinality discipline) —
  it appears on the structured log line below so operators can pivot
  on a specific channel from the log surface.
- Log line on cap-drop:
  `channels: cascade limit reached channel_id=… sender_id=… depth=… max_cascade_depth=… suppressed_recipients=…`
  (level=Warn). One line per capped publish.

**Configuration**

```yaml
# config/channels.yaml
max_cascade_depth: 5          # default; matches agents/dispatch.py
max_channels: 50
channels:
  - name: planning
    members:
      - {id: alex, respond: always}
      - {id: jordan, respond: always}
```

A zero or negative `max_cascade_depth:` row is ignored — the backstop
cannot be silently disabled from config.

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

> The closely related but distinct problem — a **single** stimulus fanned out
> to several responders, each replying blind to its peers — is solved by
> **floor control** (below), on by default for group channels as of v0.3.6.

### Floor control (RFC 0030 Layer 2.5) — on by default in v0.3.6

Before v0.3.6, a message landing in a channel with two or more responders
(`always` members, or mentioned `when_mentioned` members) was fanned out to all
of them **concurrently and fire-and-forget**. Each persona composed against a
transcript that contained **none** of its peers' replies — producing N
overlapping, mutually-blind replies to one prompt. Cascade depth and reply
budgets bounded the *volume* but never the *order*, so a multi-persona channel
read as a shout rather than a conversation.

**Floor control** serializes the responders into a deterministic speaker round.
For a message with ≥2 candidate responders on a floor-controlled channel:

1. The responders are ordered **mentioned-first, then existing member order**
   (the order is frozen at the start of the round — no mid-round promotion).
2. They take the floor **one at a time**. Each is dispatched only after the
   previous speaker's reply has landed in history, so every persona composes
   against a transcript that already contains its predecessors' replies.
3. If a speaker does not reply within **`floor_turn_timeout_seconds`**
   (default **45 s** — distinct from the 5 s per-recipient fanout timeout; a
   floor turn waits for a full LLM-composed reply), the loop advances to the
   next responder rather than stalling the round.

Members that are *not* responders this round (`when_mentioned` members who were
not mentioned) are still delivered the message concurrently for memory
ingestion — off the floor, adding no latency.

**The trade is latency.** Responders that used to compose in parallel now go
serial: a round of three responders costs roughly three reply-compositions
end-to-end instead of one. That is the intended cost of a coherent,
mutually-aware conversation; it is bounded per turn by the 45 s timeout and per
round by the responder count (itself bounded by cascade depth and reply budgets).

**Configuration.** Floor control is resolved **on by default for every group
channel** — both those declared in `config/channels.yaml` and those created at
runtime (`POST /api/v1/channels` / the console "New channel" form, resolved on
restart too) — and is a no-op below two responders (a DM is single-responder),
so the default is free for one-on-one conversations. Per channel:

```yaml
channels:
  - name: planning
    floor_control: true            # resolved group default; omit to inherit
    floor_turn_timeout_seconds: 60 # optional; default 45
    members: [ember-owl, iron-fox, nova-sparrow]
  - name: firehose
    floor_control: false           # explicit opt-out — keep concurrent fanout
    members: [a, b, c]
```

Omitting the key inherits the on-by-default; an explicit `false` opts the channel
back out (the knob is a tri-state internally so a deliberate opt-out is
distinguishable from "said nothing"). It is resolved once at startup — the
v0.3.6 contract is "set before traffic."

> **Single-replica only.** Floor state lives in-process (like the chat
> reply-waiter, §3); a horizontally-scaled orchestrator would not serialize
> correctly. v0.3.x ships single-replica; a cross-process primitive is a
> post-v0.3.6 follow-up.

See the [floor-control amendment](../rfcs/0030-amendment-floor-control-speaker-serialization.md)
for the design and locked decisions (D1–D5).

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

## 10. Resetting state between test runs

Channel history and persona memory persist across `docker compose down`
via named volumes (`orchestrator-data` for the channels SQLite store,
`ember-owl-data` for persona memory, and `workspace` for files agents
wrote under `/workspace` during the run). A second test run with the
same channel name and the same `--user` identity inherits prior content
unless those volumes are explicitly purged — personas surface old
participants and topics from prior runs and steer the next conversation
off-topic within ~2 turns.

For manual testing, use `make reset`:

```bash
make reset
make docker-up
```

`make reset` runs `docker compose down -v`, which stops the stack and
removes **every** volume declared in this compose project — currently
the three above. Any agent-written files under `/workspace` are dropped
along with the SQLite stores; if you need to keep scratch artefacts
from a prior run, copy them out before resetting. The target is
idempotent — running it twice in a row succeeds cleanly (the second
invocation finds nothing to remove).

> **`make reset` is the volume-wipe nuke, not the everyday run-isolation tool.**
> For a clean rerun — one that inherits *nothing* from the prior run — set a
> fresh **epoch** instead: `PERSATRIX_EPOCH=<id>` or the `--epoch <id>` flag
> isolates a run across *all* persona-memory tiers (episodes, relationship
> trust, person-facts) at once. See the [epochs operator guide](epochs.md).
> `make reset` runs `docker compose down -v`: it wipes **every** volume — all
> epochs across all sessions — so it cannot express the isolated-but-coexisting
> worlds an epoch gives you (CI keeps prior runs' data on disk under their own
> epoch). Reach for `make reset` only when you want the whole stack gone.
>
> Note the two axes are orthogonal: a **session is room continuity** (keyed
> `(agent, channel)`, *accumulating*) — `persatrix session new` switches to a
> fresh continuity room but does **not** purge participant-keyed state; **epoch**
> is the per-run/test isolation axis (strict equality, no carve-out). v0.3.5
> ships both: session-scoped recall + the `persatrix session …` CLI
> ([RFC 0031](../rfcs/0031-per-session-namespacing-channels.md) Phases 2–4, see
> the [sessions operator guide](sessions.md)) and the epoch axis
> ([ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md), Phase 3b). The
> original F-3 finding is closed as
> [ISSUE-0051](../issues/ISSUE-0051-per-session-memory-namespacing-channels.md)
> (surfaced as F-3 in
> [docs/v0.3.0-test-findings-pr-plan.md](../v0.3.0-test-findings-pr-plan.md)).

---

## 11. What's deferred to v0.4.0+

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
- ~~**Whole-world run/test isolation** (the `epoch` axis)~~ → **shipped in
  v0.3.5** ([ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md),
  [RFC 0031](../rfcs/0031-per-session-namespacing-channels.md) Phase 3b): a
  rerun reusing the same channel name under a fresh `PERSATRIX_EPOCH` / `--epoch`
  inherits *nothing*. See the [epochs guide](epochs.md). (Per-session recall
  namespacing also shipped in v0.3.5; see §10 and the
  [sessions guide](sessions.md). `make reset` is now the whole-stack nuke, not
  the run-isolation tool — epoch is.)

---

## 12. Manual tests

The channels surface is exercised end-to-end against a docker-composed
orchestrator + four agents via the MT-CHANNEL series:

- [MT-CHANNEL-001](../manual-tests/MT-CHANNEL-001.md) — `list` / `join` CLI
- [MT-CHANNEL-002](../manual-tests/MT-CHANNEL-002.md) — `send` / `reply` / `history` CLI
- [MT-CHANNEL-003](../manual-tests/MT-CHANNEL-003.md) — `watch` polling + dedup
- [MT-CHANNEL-004](../manual-tests/MT-CHANNEL-004.md) — human-mentions-agent end-to-end (live LLM)
- [MT-CHANNEL-005](../manual-tests/MT-CHANNEL-005.md) — DM canonicalization round-trip
- [MT-CHANNEL-006](../manual-tests/MT-CHANNEL-006.md) — channel deletion + cascade
- [MT-CHANNEL-GOV-002](../manual-tests/MT-CHANNEL-GOV-002.md) — floor control: ordered, mutually-aware multi-persona replies (live LLM)

---

## Related documentation

- [RFC 0011](../rfcs/0011-channels-bridges.md) — full channel spec
- [RFC 0011 amendment — chat-as-DM](../rfcs/0011-amendment-chat-as-dm.md) — v0.2.1 chat unified under channels
- [RFC 0008](../rfcs/0008-agent-memory-context-optimization.md) — `MemoryFacade` / `MemoryBudget` (used by channel-history injection)
- [RFC 0020](../rfcs/0020-interaction-lifecycle.md) — `InteractionTracker` (channel turns route through `add_turn`)
- [Persona agents user guide](persona-agents.md) — persona-side concepts the channel surface integrates with
- [System overview diagram](../diagrams/system-overview.md) — where the channels package sits in the runtime
