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
        respond: addressed         # advisor — replies only when @-mentioned
      - id: alice                 # human users may join here too
        respond: addressed

  - name: code-review
    description: "Tight-loop pair channel"
    members:
      - id: code-writer
        respond: participant       # active collaborator — joins the open floor
      - id: code-reviewer
        respond: participant
```

Run `make validate` after any edit. The schema is
[`schemas/channel.schema.json`](../../schemas/channel.schema.json) — note its
top-level disclaimer: *"Internal-only schema until v1.0; `$id` may break across
v0.x bumps without notice."*

### Per-membership `respond` dispositions

The response gate fires per-event in the persona runtime (RFC 0011 §D). As of
v0.3.7 the `respond` field is a **disposition** — the member's role in the
conversation — not a mechanical trigger (the [RFC 0030 relevance amendment](../rfcs/0030-amendment-relevance-gated-response.md),
Tier A + Tier B):

- **`addressed`** *(default)* — replies only when `@`-mentioned or replied to
  in-thread. The quiet advisor role.
- **`participant`** — joins the **open floor**: replies to un-addressed
  messages, but a message `@`-mentioning *someone else who could take the
  floor* — another member whose disposition is not `observer` (not a
  broadcast, not the sender's own name) — **draws no reply from it** — the
  Tier A directedness fix (`reason="directed_elsewhere"` in
  [`agents/response_gate.py`](../../agents/response_gate.py)). Since v0.3.8
  (the [floor-capable-directedness amendment](../rfcs/0030-amendment-floor-capable-directedness.md)),
  a mention of a party that *cannot* reply — the human operator (joined
  `respond: never` by the demo convention), an `observer`, a non-member —
  does **not** close the floor: "@alex, here's our recommendation…" no
  longer silences the other `participant`s; the message stays open floor.
  On an open-floor message a `participant` then runs the **Tier B salience bid**
  (below) and stays out unless it has something genuinely new to add.
- **`chair`** *(v0.3.8)* — a `participant` with a **low salience `threshold`**
  (the facilitator). It clears the salience bid readily and so keeps a
  discussion moving, where a default `participant` would more often stay silent.
  A `chair` is **not a moderator** in v0.3.8: it *cannot* close, wrap up, or
  terminate a conversation — that is Layer 5, deferred to v0.4.0. Convergence
  comes from the governance layers (§4), not the chair.
- **`observer`** — ingests history into memory but never replies. Listener role.

**Tier B — the salience bid (v0.3.8, opt-in).** On the open-floor remainder Tier
A leaves, a `participant`/`chair` runs one cheap `fast`-model bid ("do I have
something worth adding that hasn't already been said?", reading the in-round
transcript) and speaks only if the score clears its `threshold`
([`agents/salience_bid.py`](../../agents/salience_bid.py)). This is the
**no-pile-on** win — a redundant follow-up draws silence instead of every
`participant` repeating the point.

- **`threshold`** *(per-member, `[0, 1]`, now live in v0.3.8)* — the salience
  score floor. **Unset → bias-to-silence**: only a *decisive* score speaks
  (conservative by default). A `chair` with no explicit value picks up the low
  default (~`0.15`). A `threshold` on a non-open-floor disposition
  (`addressed`/`observer`) is a config error (`ErrThresholdNotApplicable`) — the
  bid never runs there. A bid that fails (parse failure, denied/exhausted wallet
  lease, unresolvable `fast` alias) **fails closed** to silence.
- **Natural-language addressing** — a free-text invitation ("let's hear from
  Iron Fox") *biases* the bid (lowers the bar for the named persona, raises it
  for others). It is a **signal, never a hard filter**: only structured
  `@`-mentions deterministically drop a member (Tier A).
- **`salience_max_channel_members`** *(channel-level, default `20`)* — above this
  member count the bid is skipped and the channel falls back to `addressed`-only,
  so bid fan-out stays small on large channels.

> **Back-compat + scope.** The legacy `always` / `when_mentioned` / `never`
> values still load (normalized to `participant` / `addressed` / `observer` at
> the Go config boundary), so existing configs keep working; an unknown value is
> a loud error. The bid is keyed on the **declared vocabulary**: a member written
> with the new `participant`/`chair` disposition runs the salience bid (so a
> brainstorm stops piling on), while a member written with the **literal `always`
> keyword keeps replying unconditionally** as in v0.3.7 — a *bare* `always` is
> never bid-governed (it opts into the bid only if you also give it an explicit
> `threshold`). So a config that never adopted the disposition vocabulary
> behaves exactly as before; one that uses `participant` gets no-pile-on
> dampening, biased to silence until you tune the `threshold`. Acceptance:
> [MT-CHANNEL-RELEVANCE-001](../manual-tests/MT-CHANNEL-RELEVANCE-001.md) (Tier A)
> and [MT-CHANNEL-RELEVANCE-002](../manual-tests/MT-CHANNEL-RELEVANCE-002.md)
> (Tier B salience + `chair`).

Channel-level patterns (Quiet group / Tight-loop pair / Broadcast /
Incident) compose from member-level dispositions — see RFC 0011 §H for the
table. RFC 0011 §H labels the all-`participant` pattern "Always-respond /
incident"; this guide uses **Incident** consistently to name the role rather
than the implementation.

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
PATCH  /api/v1/channels/{id}/members/{participant_id}    # update member config (RFC 0050)
DELETE /api/v1/channels/{id}/members/{participant_id}    # remove one member
GET    /api/v1/channels/{id}/members/{participant_id}/history # membership stints, oldest-first (RFC 0035)
POST   /api/v1/channels/{id}/messages                    # publish
GET    /api/v1/channels/{id}/messages                    # history (newest-first)
GET    /api/v1/channels/{id}/activity                    # console presence (RFC 0048)
GET    /api/v1/channels/{id}/messages/{message_id}/thread # thread under a parent
GET    /api/v1/channels/{id}/config                      # read governance config (RFC 0050)
PATCH  /api/v1/channels/{id}/config                      # update governance config (RFC 0050)
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
      - {id: alex, respond: participant}
      - {id: jordan, respond: participant}
```

A zero or negative `max_cascade_depth:` row is ignored — the backstop
cannot be silently disabled from config.

### Conversation governance (RFC 0030 Layers 1/2/4) — v0.3.8

The cascade-depth backstop above is **Layer 0** of the [RFC 0030 governance
model](../rfcs/0030-multi-agent-conversation-governance.md#b-layered-architecture):
the always-on net that stops runaway loops. v0.3.8 adds three deterministic
layers that let a multi-persona brainstorm *converge, stay bounded, and
terminate* — without a moderator. Each is scoped to an **interaction**: the
orchestrator resolves one open interaction per channel and stamps its id on
**every publish** (the [interaction-id producer](../rfcs/0030-interaction-id-producer-pr-plan.md)
— inbound id claims are overridden, so only orchestrator-minted ids ever key
governance state), rotating to a fresh interaction after the channel sits
quiet past `interaction_idle_timeout_seconds` (default **600**; explicit `0`
disables idle rotation; thread channels never rotate — a thread *is* its
interaction). Layers 1 and 2 **default to uncapped** (`0`); Layer 4 is
**live by default**: personas carry the vote vocabulary in their system
prompt (the `end-interaction-vote` snippet) and the default K=`2`/W=`3`
quorum closes the conversation when two of them say they're done. **A
conversation now ends because its participants said so** — or because the
channel went idle — with the depth cap demoted to the regression backstop
[RFC 0030 §D](../rfcs/0030-multi-agent-conversation-governance.md#d-layer-0--cascade-depth-backstop-shipped)
intends: a `governance_drop{layer=depth}` on a governed channel is a signal
something upstream failed, not business as usual.

**Stalls escalate to the chair** (the
[chair-stall-escalation amendment](../rfcs/0030-amendment-chair-stall-escalation.md),
a minimal Layer 5 slice). A floor round that ends with **zero replies** on
the open interaction — every participant honestly bid "nothing new to add"
with the question unresolved — would previously just stand until idle
rotation buried it, outcome unrecorded. With `escalation_chair_id` set, the
orchestrator dispatches **one forced turn per interaction** to that member:
its prompt forbids silence for the turn and steers it to either cast its
end-of-discussion vote with the **synthesis in the vote's content** (one
more concurring vote then closes with the synthesis on the record) or to
@-mention the member best placed to resolve what remains. Closing still
flows through the Layer 4 quorum alone — the chair proposes, the quorum
disposes; no new close path, no new trust grant. The knob must name a
non-`observer` member and the channel must not disable floor control
(detection lives at the round's tail) — both validated loudly at load.
Every detected stall emits `chair_escalation{outcome}` (`dispatched` /
`no_chair` / `already_escalated` / `self_stimulus` / `dispatch_error`), so
stalls are visible even on channels with no chair configured. Acceptance:
[MT-CHANNEL-GOV-004](../manual-tests/MT-CHANNEL-GOV-004.md).

| Layer | Knob | Default | What it bounds |
|-------|------|---------|----------------|
| **1 — cost ceiling** | `interaction_budget_tokens` (per-channel) · `default_interaction_budget_tokens` (fleet) | `0` (uncapped) | Total LLM tokens leased across one interaction. Once the running total would cross the budget, further leases are **denied** (`INTERACTION_BUDGET_EXHAUSTED`) — **fail-closed**: the LLM call does not happen, so the persona produces no reply. Enforced in the wallet on the lease path (RFC 0023), upstream of the channel publish. |
| **2 — reply budget** | `max_replies_per_participant_per_interaction` (per-channel) · `default_max_replies_per_participant` (fleet) | `0` (uncapped) | How many times one participant may publish in one interaction. The `(K+1)`th publish is rejected **pre-persistence** (HTTP **429**, `ErrParticipantBudgetExhausted`) so an over-budget message never enters channel history and never pollutes future recall. Human principals are exempt — see `governance.exempt_principals` below. |
| **4 — end-of-interaction vote** | `end_vote_threshold` (K) · `end_vote_window` (W), per-channel | K=`2`, W=`3` | A persona emits an `END_INTERACTION_VOTE` action when it judges its contribution complete. When **K distinct** participants vote within **W consecutive** turns, the interaction **closes** and stops drawing new replies. Votes are deduped per `(participant, interaction)`; an end-vote is exempt from the Layer 2 reply budget so a budget-saturated participant can still cast the terminating signal. |
| *interaction scope — idle rotation* | `interaction_idle_timeout_seconds` (per-channel) · `default_interaction_idle_timeout_seconds` (fleet) | `600` (seconds) | Not a layer — the lifetime of the **unit the layers count against**. Once the channel sits quiet past the window, the next publish retires the open interaction (`interaction_closed{trigger=idle}`, emitted lazily — see the telemetry note below) and mints a fresh one, resetting every per-interaction count above. Explicit `0` disables idle rotation; thread channels never rotate — a thread *is* its interaction. |

**Composition + failure-down ([RFC 0030 §B](../rfcs/0030-multi-agent-conversation-governance.md#b-layered-architecture)).**
A publish proceeds only if **every active layer admits it**; a lower-layer drop
short-circuits the higher layers (no point asking later layers once the cost
ceiling or reply budget has already said no), and higher layers fail safely down
to the always-on Layer 0 cap. Concretely: Layer 1 fails closed before a reply is
even generated; Layer 2 rejects before persistence; Layer 4 and Layer 0 suppress
fanout after the message persists.

**Human exemption.** `governance.exempt_principals: [human]` (fleet-wide) exempts
human participants from the Layer 2 reply budget, so a person steering the
conversation is never throttled. The end-vote (Layer 4) has no such exemption — a
human who explicitly votes that the interaction is done counts toward the quorum.

```yaml
# config/channels.yaml
default_interaction_budget_tokens: 0       # fleet default (uncapped)
default_max_replies_per_participant: 0     # fleet default (uncapped)
default_interaction_idle_timeout_seconds: 600  # fleet default idle window (seconds)
governance:
  exempt_principals: [human]               # humans bypass the Layer 2 reply budget
channels:
  - name: brainstorm
    interaction_budget_tokens: 50000       # Layer 1: ~50k tokens per interaction
    max_replies_per_participant_per_interaction: 3   # Layer 2: at most 3 turns each
    end_vote_threshold: 2                  # Layer 4: 2 distinct votes …
    end_vote_window: 3                     # … within 3 consecutive turns closes it
    interaction_idle_timeout_seconds: 600  # quiet this long → next publish opens a fresh interaction
    escalation_chair_id: jordan            # a stalled round forces one turn from this member
    members:
      - {id: alex, respond: participant}
      - {id: jordan, respond: participant}
      - {id: sam, respond: participant}
```

**Operator-facing telemetry** (RFC 0019 naming; all under `channel.conversation.*`):

- `governance_drop{channel_type, layer}` — one increment per publish a layer
  dropped. `layer ∈ {depth, reply_budget, end_vote}` (the channel-owned layers;
  the `cost` label is reserved for the wallet-side Layer 1 drop counter, **not yet
  emitted** — it lands with the budget-stamping follow-up). Anomalous drops
  (reply-budget exhaustion, duplicate vote) also carry a Warn line with
  `channel_id` / `interaction_id` / `participant_id`; expected suppression
  (post-close traffic) is metered without a log.
- `interaction_closed{channel_type, trigger}` — one per closed interaction;
  `trigger ∈ {end_votes, idle}` (`idle` is emitted lazily, on the publish that
  rotates past the window — it can lag the semantic close by the gap to the
  channel's next message). `cost` remains reserved.
- `end_vote_emitted{channel_type}` — one per vote action (vote volume vs. the
  quorum the close counter measures).
- `chair_escalation{channel_type, outcome}` — one per **detected stall** (a
  fully-silent floor round on the open interaction; the chair-stall-escalation
  amendment above), labelled with its disposition: `dispatched`, `no_chair`
  (knob unset), `already_escalated` (the interaction's one ration is spent),
  `self_stimulus` (the chair authored the stalled message itself, so the
  forced turn is withheld — the ration stays **unspent**, unlike the spent-ration
  `dispatch_error` branch), or `dispatch_error`.
- `close_notification{channel_type, outcome}` — one per **notified member** of
  an `end_votes` close (the
  [end-vote-close-propagation amendment](../rfcs/0030-amendment-end-vote-close-propagation.md)):
  the closing vote re-dispatched as a marked control event so each agent-local
  tracker closes at close time instead of idling out. `outcome ∈ {dispatched,
  dispatch_error}`. The closing sender and `respond: never` members are not
  recipients by contract, so no data points after a close reads "nobody to
  notify", never "notification lost". Note for delivery dashboards: like the
  chair's forced turn, every notification also counts on
  `channel.messages.delivered` — that stream includes orchestrator-authored
  control dispatches, not only stimulus fanout.
- `reply_budget_remaining{channel_type}` — histogram of each **replying**
  participant's leftover allowance at interaction close (one sample per
  participant who consumed reply budget; members who stayed silent or only cast
  an end-vote are not sampled, so full-headroom samples never mask the tail); a
  tail near zero says the budget is too tight.
- Trace correlation: every drop stamps `conversation.governance.layer=<layer>` on
  the inbound publish span, so "all publishes dropped by Layer 2 in #planning
  today" is one trace query, not a log grep.

> **Calibration.** No normative non-zero defaults ship in v0.3.8 — sensible
> per-workload budgets need observed-usage data ([§OQ-5](../rfcs/0030-multi-agent-conversation-governance.md#open-questions)).
> Start uncapped, watch `reply_budget_remaining` / `cost_tokens_per_interaction`,
> then set bounds. The `chair` disposition ships in v0.3.8 as a low-threshold
> **facilitator** (§2) — it keeps a discussion moving but **cannot** close it.
> The Layer 5 **moderator** (a persona that reads the transcript and actively
> wraps up / terminates) is **v0.4.0** — v0.3.8 convergence is deliberately
> deterministic (Layers 1/2/4), so it needs no moderator.

### Editing governance config at runtime — `channel config` (RFC 0050 Phase 1)

The governance knobs above are declared in `config/channels.yaml` and resolved at
startup. RFC 0050 Phase 1 adds an **operator surface to read and edit a channel's
governed knobs at runtime**, without a redeploy — over
`GET`/`PATCH /api/v1/channels/{id}/config` and the `persatrix channel config`
verb group:

```bash
# Show effective values, provenance ([channel] override vs [default] inherited),
# and the channel's config revision.
persatrix channel config get planning
persatrix channel config get planning --json

# Override one or more knobs (space-separated key=value). Knob names match the
# YAML fields above (floor_control, end_vote_threshold, end_vote_window,
# escalation_chair_id, interaction_idle_timeout_seconds, interaction_budget_tokens,
# max_replies_per_participant_per_interaction, salience_max_channel_members).
persatrix channel config set planning floor_control=true end_vote_window=4

# Clear one or more knobs back to inherit.
persatrix channel config unset planning floor_control

# Regenerate a channel's YAML override block from the store, stamped
# `revision: store + 1` — emits ONLY the explicitly-overridden knobs (inherited
# knobs are not frozen). To stdout, or to a file with `--out`.
persatrix channel config export planning
persatrix channel config export planning --out planning.patch.yaml

# Apply each declared channel block in a YAML file (the config/channels.yaml
# shape) through the same optimistic-concurrency PATCH path. The whole file is
# parsed and validated before the first write, so a typo aborts before any
# channel is touched.
persatrix channel config import planning.patch.yaml

# Compare a channel's declared YAML block against its effective store config and
# surface per-knob drift plus a revision comparison (default file:
# config/channels.yaml; override with `--file`).
persatrix channel config diff planning
```

- **Dark by default.** The whole surface — read and write — is gated behind the
  operator-authored `panels.channel_timeline.config_edit_enabled` toggle in
  [`config/ui.yaml`](../../config/ui.yaml). A `403` means it is off.
- **Optimistic concurrency.** `set`/`unset` read the current revision and carry it
  back as an `If-Match` guard; a concurrent edit surfaces as a conflict with a
  re-read steer (re-run `config get` and retry). A successful write echoes the
  bumped revision and new effective config, so no second round-trip is needed.
- **Closed knob set, client-validated.** A typo'd knob or wrong-typed value
  (`floor_control=maybe`) fails fast with the vocabulary listed, before any
  round-trip; the server owns value *ranges*. `escalation_chair_id=` (empty
  string) is the explicit "disable escalation" override — distinct from
  `unset escalation_chair_id` (clear back to inherit).
- **`interaction_budget_tokens`** is router-held and **live-enforced** in the
  wallet on the lease path (RFC 0050 interaction-budget-enforcement amendment) —
  the `get` view resolves a concrete effective value for both the overridden and
  the inherited case, so it no longer carries a deferral note or reads as `—`.
- **Nested dotted knobs.** Beyond the flat knobs above, the surface carries two
  nested blocks edited with dotted keys: `reasoning.*` (RFC 0051) and, since
  v0.3.11, `autonomous.*` (RFC 0052) — `autonomous.enabled`, `.topic`,
  `.agenda` (a comma-separated list → a `[]string`), `.convener`, `.goal`,
  `.max_rounds`. Each `set`/`unset` nests under its block (`set planning
  autonomous.enabled=true autonomous.agenda='Cost, Coupling'`); `get` renders them
  as `autonomous.<sub>` rows. `validate` rejects an `autonomous.enabled` channel
  without a positive `interaction_budget_tokens` cap and a convener that is not a
  declared, floor-capable member distinct from `escalation_chair_id`. Since v0.3.11
  (RFC 0052 PR 3) an armed channel can be **convened** — see
  [§13 Autonomous channels](#13-autonomous-channels-rfc-0052). `export`/`import`/`diff`
  defer both nested blocks (the boot loader applies a declared `autonomous:` block).

- **Export-first, revision-stamped.** `export` regenerates the YAML from the
  store stamped `revision: store + 1`, so the hand-edit loop (export → edit →
  commit/`import`) carries a fresh, higher revision without the operator
  remembering to bump it (the RFC 0050 foot-gun mitigation). `import` is the
  **live CLI writer** — it is `If-Match` guarded like `set`/`unset`, *not* the
  revision-gated boot loader, so it does not gate on the file's `revision:`
  (that field is what the boot loader consumes once the file is committed to
  `config/channels.yaml`). `diff` resolves `interaction_budget_tokens` like any
  other knob (its effective value is now router-resolvable), so it reports
  `InSync` / `Drift` / `Inherited` rather than a budget-specific carve-out. A
  knob the file omits but the store overrides
  (`source == "channel"`) reads as `DRIFT (store-only)`, **not** `inherited`:
  the boot reconcile replaces the whole override blob with the declared set, so
  an undeclared live override would be cleared on boot — that is real drift the
  file does not capture.

- **`import` is sparse-additive, not a reconcile.** It applies only the knobs each
  block *declares* and never clears a store override the file omits — so it is
  **not** equivalent to the boot reconcile, which rewrites the whole override blob.
  This means `import` does not resolve `DRIFT (store-only)`: a live override the
  file omits stays in the store after `import` and is only cleared by committing
  the file and rebooting, or by an explicit `unset`. `import` is also best-effort,
  not atomic — there is no cross-channel transaction, so a 409 or wire error on a
  later block leaves the earlier blocks applied; the error names the channels that
  already landed so the remainder can be re-run after re-reading.

> **Scope.** `get`/`set`/`unset` ride purely on the REST endpoints;
> `export`/`import`/`diff` additionally read or write the declared
> `config/channels.yaml`. As with the other verbs, the authoritative flag grammar
> lives in the CLI source — the REST-only core in
> [`cli/src/commands/channel_config.rs`](../../cli/src/commands/channel_config.rs)
> and the YAML verbs in
> [`cli/src/commands/channel_config_yaml.rs`](../../cli/src/commands/channel_config_yaml.rs)
> — not this guide.

**From the web console (RFC 0050 Phase 2).** The same `get`/`set`/`unset` surface
is available in the browser as a **Channel settings** panel nested in the
Channels tab — each governed knob with its effective value, an
overridden-vs-inherited provenance badge, and an inherit/override control. It is
gated behind the **same** `config_edit_enabled` toggle (which the shipped
`config/ui.yaml` now sets on) and rides the same `If-Match` revision, so a value
set in the browser is the value the
CLI `channel config get` reads back. See the
[web-console guide § Channel settings](web-console.md#channel-settings--edit-governance-from-the-browser).

### The interaction-summary surface (RFC 0020) — v0.3.8

Governance makes a brainstorm *converge and terminate*; the **summary surface**
turns "terminated" into "here's the result". When an interaction closes — by an
end-vote (Layer 4), by the cost ceiling (Layer 1), or by going idle — the persona
persists a one-per-interaction summary to its `episodes` row
([RFC 0020 §C/§D](../rfcs/0020-interaction-lifecycle.md#c-interaction-lifecycle-states)),
and v0.3.8 **surfaces that already-persisted summary** so a converged
conversation hands back something a human can read. The summariser itself is
unchanged — this is a read surface, not a new synthesis step.

**Where it appears:**

- **Web console** — the conversation view renders an "interaction closed"
  affordance below the live turns, carrying the summary and the close trigger
  (see [web-console.md §"The conversation panel"](web-console.md#the-conversation-panel)).
- **CLI** — read a persona's closed-interaction summaries newest-first:

  ```bash
  # newest closed interactions for a persona (any scope)
  persatrix agent interactions iron-fox --limit 5

  # restrict to one conversation scope; emit JSON for scripting
  persatrix agent interactions iron-fox --scope group:planning --limit 1 --json

  # a single interaction by id
  persatrix agent interactions iron-fox --interaction-id <id>
  ```

  Both surfaces read `GET /api/v1/agents/{id}/interactions/closed` — the summary
  is **per-agent** (each participating persona persists its own row), so the web
  surface merges across the channel's participants and shows one affordance.

> **Two interaction-id namespaces (ISSUE-0102).** The `interaction_id` on a
> closed-interaction row is the persona's **agent-side** RFC 0020 memory-episode
> id, minted on the persona's own idle clock. It is **not** the orchestrator's
> RFC 0030 **governance** interaction id — the one stamped on channel messages,
> printed in the escalation/close logs, and used for the end-vote quorum. The two
> producers segment on independent clocks, so a single governance interaction can
> map to **several** agent-side episode ids (e.g. an agent-side idle boundary
> splitting one governance arc into two episodes). To keep the channel-side id
> cross-referenceable, each row also carries `governance_interaction_id` (shown
> on the CLI's dimmed `governance:` line when present); it is empty for a DM /
> thread / non-channel interaction that never carried a governance id. The
> `--interaction-id` filter matches **either** id space: pass an agent-side
> episode id to get that one interaction, or paste the end-vote-closed
> **governance** id straight from the logs to get every episode of that arc
> (one governance interaction can return several rows, newest-first). So the
> natural diagnostic — take the closed id from the escalation/close log and run
> `agent interactions <agent> --interaction-id <that-id>` — just works,
> whichever namespace the id came from.

**When the row appears.** The orchestrator's close (quorum / idle rotation) and
the per-agent row are produced by different processes, and the closing publish's
fanout is deliberately suppressed — so the channel-side close reaches each
agent's local interaction record through two seams
(`agents/persona_runtime/interaction_boundary.py`):

- **A voter closes when its vote lands.** Emitting `END_INTERACTION_VOTE` is
  the persona's own "my contribution is complete", so its local record of the
  conversation closes (and summarises) as soon as the vote *publish succeeds*
  — query a *voter* right after the close and the row is already there,
  labelled *ended*. This is the persona's judgement, not the quorum: a lone
  voter's record closes even if the quorum never forms and the room talks on
  (its next turn simply opens a fresh local interaction). A vote whose
  publish *fails* (timeout, channels disabled) closes nothing — the vote
  never reached the orchestrator, so the record stays open for the ordinary
  closes (PR 607 review finding 5; `agents/persona_runtime/vote_close.py`).
  Re-votes mirror the orchestrator's dedup: voting *again* on the same
  still-open conversation closes nothing further (the orchestrator counts a
  participant once per interaction), so one discussion never fragments into
  multiple *ended* rows on the voter.
- **Everyone else closes on the id rotation.** A non-voting member's record
  closes the moment it receives the channel's next publish carrying the
  rotated `interaction_id` (the new topic) — or by its own idle window if the
  room stays quiet. Until one of those happens, its row for the closed
  discussion does not exist yet; that lag is inherent to the lazy rotation.

Two boundary notes on those seams:

- **Threads are exempt.** A threaded reply rides the *parent* channel's
  interaction id (the orchestrator resolves per channel), so neither seam
  applies inside a thread: a floor close never splits a live thread's record,
  and a vote cast *from* a threaded turn closes nothing at vote time — the
  voter's floor record closes on the floor's rotation like any non-voter's.
  Thread records close by their own idle window or an explicit session end,
  mirroring the resolver's "the thread IS the interaction" rule.
- **A vote close is announced, not inferred** (the
  [end-vote-close-propagation amendment](../rfcs/0030-amendment-end-vote-close-propagation.md),
  v0.3.8). The closing quorum vote's ordinary fanout is suppressed (the room
  must stop), so the orchestrator re-dispatches it to every dispatch-served
  member as a marked close notification
  (`ChannelMessageEvent.interaction_close_notification`): the receiver's gate
  refuses it pre-LLM — no turn, no salience bid, no spend — the closing
  message still lands in the window as the record's final turn, and the
  member's record closes **immediately** as *ended*. Before the amendment a
  converged-then-quiet room buried its own decision: every member idled out
  up to a full `memory.interaction_idle_timeout_sec` later as *went idle*
  (found live by [MT-CHANNEL-GOV-004](../manual-tests/MT-CHANNEL-GOV-004.md)).
  Fail-open: a dropped notification degrades to exactly that pre-amendment
  idle-out, observable per recipient on
  `channel.conversation.close_notification{outcome}`. A notification that
  arrives only *after* the member's own idle window already closed the
  record degrades the same way — the agent's boundary rules outrank the
  late signal, and no local record is invented to mirror the
  orchestrator's (which stands regardless).
- **The rotation carries its cause.** Every publish of the successor
  interaction names the retired id and what closed it
  (`ChannelMessageEvent.previous_interaction_id` +
  `previous_interaction_close_trigger`, [producer plan OQ 5](../rfcs/0030-interaction-id-producer-pr-plan.md#open-questions)),
  so a rotation-closed record is labelled truthfully: *went idle* for a
  channel idle rotation (even one shorter than the agent's
  `memory.interaction_idle_timeout_sec`), *ended* for the end-vote quorum.
  The fields are absent from an old orchestrator and after an orchestrator
  restart (the resolver re-mints its in-memory ids with no retiree to
  attribute) — there the record keeps the legacy *ended* label, and the
  cause is applied only when the retired id matches the one the record was
  opened under (an agent that missed a whole generation falls back too).

**Close-trigger labels** (the RFC 0020 `close_reason`, rendered identically on
both surfaces):

| `close_reason` | Label | Meaning |
|----------------|-------|---------|
| `idle_gap` | *went idle* | the conversation went quiet past an idle window — the agent's own, or the channel's (carried on the wire as the rotation cause, producer plan OQ 5) |
| `structural` | *ended* | an explicit end — the Layer 4 end-vote close routes through the structural close; the row does not distinguish a vote-close from a plain structural close, so "ended" is the honest label |
| `cost` | *cost limit reached* | the Layer 1 per-interaction cost ceiling tripped |

**Honest failure.** When the on-close summariser fails, the persisted
`"[interaction summary unavailable]"` sentinel is surfaced as an explicit
"summary unavailable" state — never a blank, never a fabricated synthesis. A
single-turn interaction degenerates to its per-event summary. Acceptance:
[`MT-INTERACTION-SUMMARY-001`](../manual-tests/MT-INTERACTION-SUMMARY-001.md).

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

Before v0.3.6, a message landing in a channel with two or more responders was
fanned out to all of them **concurrently and fire-and-forget**. Each persona
composed against a transcript containing **none** of its peers' replies — N
overlapping, mutually-blind replies to one prompt. Cascade depth and reply
budgets bounded the *volume* but never the *order*, so the channel read as a
shout, not a conversation.

**Floor control** serializes the responders into a deterministic speaker round.
For a message with ≥2 candidate responders on a floor-controlled channel:

1. The responders are ordered **mentioned-first, then existing member order**
   (frozen at round start — no mid-round promotion).
2. They take the floor **one at a time**. Each is dispatched only after the
   previous speaker's reply has landed in history, so every persona composes
   against a transcript that already contains its predecessors' replies.
3. If a speaker does not reply within **`floor_turn_timeout_seconds`**
   (default **45 s** — distinct from the 5 s per-recipient fanout timeout; a
   floor turn waits for a full LLM-composed reply), the loop advances rather
   than stalling the round.

Non-responders this round (un-mentioned `when_mentioned` members) are still
delivered the message concurrently for memory ingestion — off the floor, no
added latency.

**The trade is latency.** Responders that used to compose in parallel now go
serial: a round of three costs roughly three reply-compositions end-to-end
instead of one — the intended cost of a coherent, mutually-aware conversation,
bounded per turn by the 45 s timeout and per round by the responder count.

**Configuration.** Floor control is resolved **on by default for every group
channel** — both those declared in `config/channels.yaml` and those created at
runtime (`POST /api/v1/channels` / the console "New channel" form) — and is a
no-op below two responders (a DM is single-responder),
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
v0.3.6 contract is "set before traffic." **Opt-out is config-only:** a
runtime-created channel (`POST /api/v1/channels` / the console form) always
resolves ON — there is no create-time field and the store persists no floor
flag, so a restart re-forces it ON; create-time opt-out plus persistence is one
post-v0.3.6 follow-up.

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
- **The Layer 5 moderator** (the `chair`'s *active* half — a persona that reads
  the transcript and decides to wrap up / terminate) → v0.4.0. v0.3.8 ships the
  `chair` as a low-threshold **facilitator** only (§2); its moderator seam is
  present but inert — a typed attach point no runtime path calls
  ([RFC 0030 §"Layer 5"](../rfcs/0030-multi-agent-conversation-governance.md)).
- **Declarative conversation types** (Layer 6) → v0.5.0+.
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
orchestrator + four agents via the channel manual-test series:

- [MT-CHANNEL-001](../manual-tests/MT-CHANNEL-001.md) — `list` / `join` CLI
- [MT-CHANNEL-002](../manual-tests/MT-CHANNEL-002.md) — `send` / `reply` / `history` CLI
- [MT-CHANNEL-003](../manual-tests/MT-CHANNEL-003.md) — `watch` polling + dedup
- [MT-CHANNEL-004](../manual-tests/MT-CHANNEL-004.md) — human-mentions-agent end-to-end (live LLM)
- [MT-CHANNEL-005](../manual-tests/MT-CHANNEL-005.md) — DM canonicalization round-trip
- [MT-CHANNEL-006](../manual-tests/MT-CHANNEL-006.md) — channel deletion + cascade
- [MT-CHANNEL-GOV-002](../manual-tests/MT-CHANNEL-GOV-002.md) — floor control: ordered, mutually-aware multi-persona replies (live LLM)
- [MT-INTERACTION-SUMMARY-001](../manual-tests/MT-INTERACTION-SUMMARY-001.md) — a closed interaction hands back a readable summary on web + CLI, on every close trigger (idle / end-vote / cost), with honest failure rendering (live LLM)

---

## 13. Autonomous channels (RFC 0052)

An **autonomous channel** runs a discussion with **no human in the loop**: no
human seeds the topic, no human keeps it alive. It is an ordinary group channel
carrying an `autonomous` block (configured exactly like the governance knobs in
[§4](#4-the-response-gate-who-replies-and-when) — YAML, `channel config`, or the
web panel) plus one operator action — **convene** — that opens the discussion.

```yaml
# config/channels.yaml — an armed channel
autonomous:
  enabled: true
  topic: "Should we adopt a monorepo? Lay out the tradeoffs."
  agenda: ["Build tooling cost", "Cross-team coupling", "Migration effort"]
  convener: nova-sparrow          # authors the opening turn; a DISTINCT role from
                                  # escalation_chair_id (RFC 0052 OQ #1)
  goal: "A synthesized recommendation with the strongest argument on each side."
  interaction_budget_tokens: 200000   # MANDATORY — validate rejects uncapped autonomy
```

**The safety contract (enforced at config-validation, RFC 0052 PR 1).** An
unattended channel has no human circuit-breaker, so an `autonomous.enabled`
channel is **un-creatable** without a positive resolved `interaction_budget_tokens`
cap; arming is **group-only** (a DM/thread cannot be made autonomous); and the
`convener` must be a declared, floor-capable member (not an `observer`) distinct
from `escalation_chair_id`.

**Convening.** Convening = the convener authors the **opening turn** under a
fresh interaction, with no human message; from that publish the ordinary
[response gate](#4-the-response-gate-who-replies-and-when) + `InboundEventWake`
chain carries the discussion. Under the hood the orchestrator dispatches a
directed **convene forced turn** to the convener (the sibling of the chair-stall
escalation — same directed-lane admission, so the opener is never silenced by the
bias-to-silence salience bid). The operator-supplied `topic`/`agenda`/`goal` are
wrapped in the RFC 0009 `<external_data>` envelope before they reach the
convener's prompt — operator config is a distinct trust class, the one genuinely
new injection surface this opens. The opening turn resolves **uncapped** (the
wallet snapshots the per-interaction cap at the interaction's first commit, so
the lease that *produces* the opener predates its own snapshot); the always-on
RFC 0030 Layer-0 depth cap bounds that first call.

Convene is reachable on all three RFC 0050 surfaces, each gated behind the **same**
`config_edit_enabled` toggle as the config surface. Be aware of what that toggle
actually is: the bundled `config/ui.yaml` ships it **`true`** (and it is loaded
even without `--enable-ui`), so in a default deployment convene is reachable as
soon as a channel is armed — it is **not** a dark, dedicated convene opt-in.
Because convene shares the config-edit gate, the same `config_edit_enabled: false`
that lands the config surface dark also disables convene; the deliberate human
steps that gate an unattended discussion are *arming* the channel (a config edit)
and pressing convene. Convening does trigger real LLM spend on an unattended
channel, so treat enabling the operator surface as also enabling convene:

```bash
# CLI — POST /api/v1/channels/{id}/convene
persatrix channel convene group:planning
persatrix channel convene planning --json     # {channel_id, convener, status}
```

```text
# REST
POST /api/v1/channels/{id}/convene      → 202 {channel_id, convener, status:"convening"}
                                          403 toggle off · 404 no such channel
                                          409 not autonomous.enabled · 409 already has a
                                              live interaction · 409 no open-floor responder
                                              besides the convener · 409 no topic/agenda/goal
                                              to convene on
                                          400 convener drifted out of the roster
```

> **The audience must answer an *open-floor* opener.** The convener's opening
> turn addresses the room as a whole (it names no one), and only `participant`
> (`always`) members reply to an open-floor message — a `when_mentioned` member
> stays silent until @-mentioned. Note an unspecified member defaults to
> `when_mentioned`, so give the intended discussants `respond: always` (the
> `participant` disposition), or convene 409s with *no open-floor responder
> besides the convener*.

Convening targets an **idle** channel: a channel that already has a live
interaction is refused (`409`) rather than silently joined — the convener opens
one discussion, not a second one over a running one (forcing-fresh on a standing
re-convene is a later RFC 0052 PR). Note the convene ack is `202 Accepted` —
"the convener was woken", not "the discussion ran". Repeated convening is bounded
by the §E aggregate ceiling: once a channel has been convened `autonomous.max_convenings`
times, a further convene is refused with `429 Too Many Requests` (the count is
process-lifetime — a restart resets it — and cleared when the channel is deleted).
A channel with `max_convenings` unset (`0`) is not count-bounded, but a positive
`standing_budget_tokens` bounds it by *cost* instead: each interaction close folds
its settled discussion spend into a per-channel running total, and once that total
reaches `standing_budget_tokens` a further convene is likewise refused with `429`
— the aggregate-*spend* twin of the count ceiling, process-lifetime and
delete-cleared in the same way (the async per-persona close summaries settle after
the close, so the folded total tracks the discussion spend; the co-declared count
bound caps how far it can overrun). Only the timer that fires the schedule
automatically remains a later RFC 0052 PR, so treat convene as an
operator-initiated, not a scripted-loop, action until the schedule lands.

How much of the aggregate allowance is spent is visible on the config **read**
surface: `GET …/config` carries an `autonomous_runtime` block —
`convening_count` (openers dispatched this process lifetime) and
`convenings_remaining` (the `max_convenings` allowance left, or `null` when
unbounded; clamped at zero if a lowered bound sits below the spent count). The web
*Autonomous channel* panel renders it as a **Convenings: _N_ used, _M_ remaining**
line, and `persatrix channel config get` prints a trailing `convenings … (runtime)`
row. It is read-only observability — the count itself is enforced by the `429`
ceiling above.

- **Web console** — a **Convene** button in the Channel-settings panel's
  *Autonomous channel* section, shown only when the channel is armed per the
  *saved* config and disabled while there are unsaved edits (convening reads the
  persisted block, so save first). See the
  [web-console guide § Channel settings](web-console.md#channel-settings--edit-governance-from-the-browser).

### Try it offline — `make demo-autonomous`

To watch the whole arc with **no API key and zero spend**, run the offline demo:

```bash
make demo-autonomous
```

It boots the society on the `mock` provider (the RFC 0033 offline alias →
`provider: mock`, priced at $0), **arms** the bundled `roundtable` channel
(which ships *disarmed* for safety — see the `config/channels.yaml` template),
and **convenes** it: `nova-sparrow` opens the "Should we adopt a monorepo?"
topic, `ember-owl` and `iron-fox` discuss it through the governed wake chain,
and the chair `ember-owl` closes with a synthesized recommendation — **no human
types anything**. Watch it live in the web console (`http://localhost:8080/ui`
→ *Channels → roundtable*), or read the closing synthesis + each persona's
RFC 0020 summary with `persatrix agent interactions ember-owl` once it closes;
the channel is left re-convenable, so the web **Convene** button re-runs it.
Stop with `make docker-down`.

The convener/chair/participant turns come from the curated
`config/offline_responses.yaml` fixtures, so the offline discussion is
deterministic — it demonstrates the *shape* of a human-free brainstorm, not a
live model's reasoning. Swap `provider: mock` for a keyed vendor (`make
demo-anthropic` / `demo-openai`, or the four-vendor headline once RFC 0053
lands) and convene the same channel for a real run. The deterministic pin that
the offline face yields a non-empty, on-topic synthesis at $0 is
[`tests/integration/test_autonomous_offline_smoke.py`](../../tests/integration/test_autonomous_offline_smoke.py);
the live acceptance is [MT-AUTONOMOUS-001](../manual-tests/MT-AUTONOMOUS-001.md).

> **Scope in v0.3.11.** PR 3 ships convening + the opening turn. PR 4 adds the
> mechanisms that make the discussion *bounded and artifact-bearing*: a
> deterministic **bounded close** terminates the interaction when it crosses
> `autonomous.max_rounds` or the wallet's soft budget (the cap minus a
> roster-scaled synthesis reserve), and — on a chaired channel — first asks the
> `escalation_chair_id` for a goal-directed **closing synthesis** against
> `autonomous.goal`: the chair's reply is delivered to every member as the
> discussion's final message, each member's RFC 0020 interaction summary is
> produced (and, on the autonomous close, metered against the cost cap), and
> the channel is left re-convenable. A chair that never replies falls back to
> an immediate close after a timeout, so termination never waits on a model.
> The remaining mechanisms — the anti-collapse cadence and standing/scheduled
> convening — land in the subsequent RFC 0052 PRs (see the
> [PR plan](../rfcs/0052-pr-plan.md)).

---

## Related documentation

- [RFC 0011](../rfcs/0011-channels-bridges.md) — full channel spec
- [RFC 0052](../rfcs/0052-autonomous-agent-channels.md) — autonomous agent-only channels (§13 above)
- [RFC 0011 amendment — chat-as-DM](../rfcs/0011-amendment-chat-as-dm.md) — v0.2.1 chat unified under channels
- [RFC 0008](../rfcs/0008-agent-memory-context-optimization.md) — `MemoryFacade` / `MemoryBudget` (used by channel-history injection)
- [RFC 0020](../rfcs/0020-interaction-lifecycle.md) — `InteractionTracker` (channel turns route through `add_turn`)
- [Persona agents user guide](persona-agents.md) — persona-side concepts the channel surface integrates with
- [System overview diagram](../diagrams/system-overview.md) — where the channels package sits in the runtime
