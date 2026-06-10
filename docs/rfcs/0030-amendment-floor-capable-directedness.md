# RFC 0030 Amendment — Directedness Requires a Floor-Capable Addressee (Tier A Mention Resolution)

**Type**: amendment to the [relevance-gated-response amendment](0030-amendment-relevance-gated-response.md) §"The graduated response gate" (Tier A — the directed-elsewhere filter), restoring its original definition of directedness against the shipped implementation
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-06-10
**Target**: v0.3.9 (two implementation PRs after this doc: orchestrator-side mention resolution + wire field; agent-side gate consumption + drift pins)
**Trigger**: Field report — a multi-persona channel answers a human prompt with one round of replies and then goes silent until the human posts again. The convergence review traced one structural cause to the Tier A filter treating **any** non-empty `mentions` list as floor-directing — including a mention of the human operator. The human is a channel member with `respond: never` by documented convention ([demo guide §3](../guides/v0.3.0-demo.md): `channel join planning --as alex --respond never`), so the moment one persona politely addresses the human ("@alex, here's our recommendation…"), every other `participant` is suppressed as `directed_elsewhere` and the room falls silent — one courteous mention ends the discussion.
**Supersedes**: the implementation reading of Tier A directedness ("`mentions` non-empty ⇒ directed") in [`agents/response_gate.py`](../../agents/response_gate.py) and [`internal/channels/floor_control.go`](../../internal/channels/floor_control.go). The [relevance amendment](0030-amendment-relevance-gated-response.md)'s own Tier A row already defines directedness as "an `@`-mention of a *different agent*" — this amendment is a spec-conformance fix plus the resolution mechanism the original text left unspecified, not a design change.

---

## Context — the spec–implementation gap

The relevance amendment's Tier A row (§"The graduated response gate") defines the suppression condition as:

> message is **directed at someone else** (an `@`-mention of a *different agent*, or a parsed "to X" recipient) and I am not also addressed.

The shipped implementation tests something weaker. The Python gate ([`response_gate.py`](../../agents/response_gate.py), `POLICY_ALWAYS` branch) suppresses iff `mentions` is non-empty, the agent is not named, and [`MENTION_EVERYONE`](../../agents/response_gate.py) is absent; the Go candidate set mirrors it ([`floor_control.go`](../../internal/channels/floor_control.go), `orderResponders`: `directed := len(msg.Mentions) > 0 && !mentioned[MentionEveryone]`). Neither side asks whether any named id **is an agent that could take the floor**. Mentions are shape-validated only ([`sqlite_messages.go`](../../internal/channels/sqlite_messages.go) — participant-ID-shaped, never membership-checked), so the filter fires identically for:

| Mentioned party | Can it take the floor? | Filter today | Result |
|---|---|---|---|
| `participant`/`always` member | yes | suppress others | intended (yield to the addressee) |
| `addressed` member | yes (it was just named) | suppress others | intended — the original pile-on defect ("how about you @ember-owl?") |
| **the human operator** (a `never` member by the [documented join convention](../guides/v0.3.0-demo.md)) | **no** | suppress others | **guaranteed total silence** |
| **an `observer`/`never` agent** | **no** | suppress others | **guaranteed total silence** |
| **a non-member** (typo, external name) | **no** | suppress others | **guaranteed total silence** |

The last three rows are the defect. Suppression exists to *yield the floor to the addressee*; when the addressee cannot take the floor, yielding produces a floor nobody holds. In the field this presents as the reported symptom: personas answer the human's open-floor prompt (round one), one of them addresses the human by name in its reply, and the channel is structurally dead until the next human message.

## A. The rule — directedness requires a floor-capable addressee

**A mention only directs the floor if the named party could actually take it.**

- **Floor-capable** ≝ a current channel member whose resolved respond policy ([`RespondPolicy.Normalize`](../../internal/channels/channels.go)) is not `never` — i.e. `always` or `when_mentioned` post-normalization. (Equivalently: a member the response gate could ever admit for this message class.)
- **Floor-directing mentions** ≝ `mentions ∩ floor-capable members`.
- Tier A's directed-elsewhere condition changes basis: suppress a non-named `participant` iff the **floor-directing** subset is non-empty (and `@everyone` is absent, unchanged). A message whose mentions name only floor-incapable parties — the human, an `observer`, a non-member — is **open floor** for suppression purposes.

Why the member-*policy* test rather than a participant-*type* (human/agent) test, which the amendment's "different agent" wording might suggest:

1. **Membership rows carry no type.** [`Member`](../../internal/channels/channels.go) is `{ParticipantID, RespondPolicy, JoinedAt, SalienceGated, Threshold}`; the `participant_type` vocabulary ([`participant_type.go`](../../internal/channels/participant_type.go)) rides per-message metadata describing the *sender* only. A type test would need a schema migration; the policy test needs data fanout already holds.
2. **The policy test subsumes the type test.** The human is floor-incapable *because* the documented convention joins humans as `never` — and an `observer` *agent* is exactly as floor-incapable as a human. "Could the gate ever dispatch a reply turn to this id?" is the question suppression actually depends on; agent-vs-human is a proxy for it.
3. **Non-members fall out for free.** A typo'd or external mention resolves to no membership row, hence not floor-capable — no extra rule needed.

Edge semantics, stated explicitly:

- A message naming **both** a floor-capable member and the human ("@iron-fox what do you think? cc @alex") is **directed** — iron-fox is floor-directing; everyone else stays suppressed. The pile-on fix is untouched.
- `@everyone` semantics are unchanged (decision D3 of the relevance amendment): the sentinel always means "do not suppress".
- The named-member admit paths are unchanged and keep reading **raw** `mentions`: a `when_mentioned` member's `mentioned` admit, an `always` member's individually-named `mentioned` admit (the Tier B TB1 lane), and the thread-reply-to-self trigger are not touched by this amendment. Only the *suppression* decision changes basis.
- A mention of an `observer` does **not** wake the observer (OQ 2). `observer` means never; the message simply does not close the floor.

## B. What does NOT change

- **The original directedness defect stays fixed.** "how about you @ember-owl?" still names an `addressed` member — floor-capable — so every non-named `participant` is still suppressed. [MT-CHANNEL-RELEVANCE-001](../manual-tests/MT-CHANNEL-RELEVANCE-001.md)'s directed-question assertion is unaffected.
- **Open-floor pile-on control is still the Tier B bid.** Messages this amendment reclassifies from directed to open-floor land in the existing open-floor lane: `participant`s reach the [salience bid](../../agents/salience_bid.py) (bias-to-silence), bare-`always` members reply unconditionally, exactly as for an unmentioned message today. The amendment moves a message class between two existing lanes; it invents no third lane, so [`is_open_floor_admit`](../../agents/response_gate.py) and the Tier B routing are structurally untouched.
- **The idle-cost invariant holds.** Tier A stays free: fanout already loads the member set it needs to resolve the floor-directing subset (no new IO), and the agent-side gate reads a pre-resolved payload field (no lookup). The reclassified messages do add Tier B bids for `participant` members that were previously suppressed for free — that is the *point* (those suppressions were the defect), and the bid is the cheap leased `fast`-model call whose cost posture the relevance amendment already accepted for exactly this traffic class.
- **No DB or schema change.** Membership rows, the `respond_policy` encoding, and the mentions persist-validation are untouched.
- Self-sender, DM override, `never` fail-closed, and unknown-policy branches of the gate are untouched.

## C. Mechanism — resolve at the orchestrator, carry on the wire

Primary enforcement belongs orchestrator-side: only the orchestrator owns membership, and it already sits on the trust boundary for exactly this shape of decision (the cascade-depth amendment's posture: primary enforcement in Go fanout, Python as defence-in-depth backstop). The precedent for the wire shape is `thread_parent_sender_id` — "pre-resolved by the router so the gate need not look the parent up itself" ([`response_gate.py`](../../agents/response_gate.py), thread branch; [`task.proto`](../../proto/task.proto) field 10).

1. **Go (PR 1).** At fanout, resolve `floorMentions := msg.Mentions ∩ {m : m.RespondPolicy.Normalize() != never}` once per publish (the member list is already in hand). `orderResponders` computes `directed` from `floorMentions` instead of raw `Mentions`; the off-floor delivery split and mentioned-first ordering keep reading raw mentions (display/admission semantics, not suppression).
2. **Wire (PR 1).** A new `repeated string floor_mentions` field on `ChannelMessageEvent` ([`task.proto`](../../proto/task.proto)), stamped per-publish (not per-recipient — the subset is recipient-independent). The GRPCMessageDispatcher lifts it into the event payload alongside `mentions`.
3. **Python (PR 2).** The `POLICY_ALWAYS` suppression branch of [`evaluate_response_gate`](../../agents/response_gate.py) reads `payload["floor_mentions"]` **when present** for the directed-elsewhere decision; the `mentioned`/`broadcast` admit checks keep reading raw `mentions`. When the field is **absent** (an old orchestrator), the gate falls back to today's raw-mentions basis — degrading toward *over*-suppression, never under-suppression, so the failure direction under version skew is the current behaviour, not a new one.
4. **Drift pins (PR 2).** [`test_cross_language_respond_policy_drift.py`](../../tests/unit/python/test_cross_language_respond_policy_drift.py) gains the field-name + semantics pin (Go resolution and Python consumption agree on "floor-capable = normalized policy ≠ never").

The gate cannot independently verify `floor_mentions` (it has no membership view — the same reason it can't compute the subset itself), so this is a deliberate, narrow trust extension to an already-trusted orchestrator-resolved field, identical in kind to `thread_parent_sender_id` and `salience_gated`. A spoofed empty `floor_mentions` on the untrusted gRPC port admits a `participant` to a directed message — the pre-v0.3.7 behaviour, bounded by the same cascade/cost layers that bounded it then.

## D. Metrics and observability

- The `channel.messages.gated{policy=always}` counter semantics narrow slightly: a Tier A directed-elsewhere drop now implies a *floor-capable* addressee existed. No label change; the [RFC 0011 §D](0011-channels-bridges.md) bounded-label set is untouched.
- The Go resolution logs (debug) when raw mentions were non-empty but `floor_mentions` is empty — the previously-silent "addressed a non-responder" case — so the reclassification is visible while the change beds in.

## E. Test strategy

- **Go**: `orderResponders` unit matrix — mention of a `never` member (the human convention), of a non-member, of an `observer`: candidate set must equal the open-floor set; mention of an `addressed`/`participant` member: unchanged directed behaviour; mixed human+agent mention: directed.
- **Python**: gate unit matrix over `floor_mentions` present/absent × empty/non-empty × named/not-named, pinning the fallback (absent ⇒ legacy basis) and the unchanged admit paths.
- **Drift pin**: the cross-language definition of floor-capable, per §C item 4.
- **Manual**: extend [MT-CHANNEL-RELEVANCE-001](../manual-tests/MT-CHANNEL-RELEVANCE-001.md) with the trigger scenario — a persona reply that @-mentions the human must draw further discussion from `participant` members (subject to the Tier B bid), not room-wide silence.

## Open questions

1. **Natural-language addressing.** Tier B treats parsed NL recipients ("question for Alex") as a bias signal; should that resolution also be member-policy-aware? Deferred to the Layer 5 bid-and-select work (v0.4.0) — NL addressing never *hard-suppresses* today, so the silence defect does not arise there.
2. **Should mentioning an `observer` wake it?** No — proposed and resolved here as "observer means never" (§A). If a future need appears it is a new disposition semantic, not a directedness question.
3. **Should `floor_mentions` eventually replace `mentions` for the admit paths too?** Default no: admission-by-name of a `never` member is already impossible (the policy gates it), so the admit paths gain nothing from the resolved subset, and keeping them on raw mentions preserves the defence-in-depth property that an agent named on the wire can see it was named.

## Related documentation

- [RFC 0030 relevance amendment](0030-amendment-relevance-gated-response.md) — defines Tier A; its "different agent" wording is what this amendment makes the implementation honour
- [RFC 0030 §B layered architecture](0030-multi-agent-conversation-governance.md) — the governance stack this filter participates in
- [Cascade-depth wire propagation amendment](0011-amendment-cascade-depth-wire-propagation.md) — the orchestrator-primary / agent-backstop enforcement posture this amendment reuses
- [`agents/response_gate.py`](../../agents/response_gate.py) — the Python Tier A implementation
- [`internal/channels/floor_control.go`](../../internal/channels/floor_control.go) — the Go candidate-set mirror
- [v0.3.0 demo guide](../guides/v0.3.0-demo.md) — the `--respond never` human-join convention that makes the human-mention case the common case
