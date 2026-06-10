# RFC 0030 Amendment — Directedness Requires a Floor-Capable Addressee (Tier A Mention Resolution)

**Type**: amendment to the [relevance-gated-response amendment](0030-amendment-relevance-gated-response.md) §"The graduated response gate" (Tier A — the directed-elsewhere filter), restoring its original definition of directedness against the shipped implementation
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-06-10
**Target**: v0.3.9 (two implementation PRs after this doc: orchestrator-side mention resolution + wire field + presence flag; agent-side gate consumption + drift pins)
**Trigger**: Field report — a multi-persona channel answers a human prompt with one round of replies and then goes silent until the human posts again. The convergence review traced one structural cause to the Tier A filter treating **any** non-empty `mentions` list as floor-directing — including a mention of the human operator. The human is a channel member with `respond: never` by documented convention ([demo guide §3](../guides/v0.3.0-demo.md): `channel join planning --as alex --respond never`), so the moment one persona politely addresses the human ("@alex, here's our recommendation…"), every other `participant` is suppressed as `directed_elsewhere` and the room falls silent — one courteous mention ends the discussion.
**Supersedes**: the implementation reading of Tier A directedness ("`mentions` non-empty ⇒ directed") in [`agents/response_gate.py`](../../agents/response_gate.py) and [`internal/channels/floor_control.go`](../../internal/channels/floor_control.go). The [relevance amendment](0030-amendment-relevance-gated-response.md)'s own Tier A row already defines directedness as "an `@`-mention of a *different* agent" — this amendment is a spec-conformance fix plus the resolution mechanism the original text left unspecified, not a design change.

---

## Context — the spec–implementation gap

The relevance amendment's Tier A row (§"The graduated response gate") defines the suppression condition as:

> message is **directed at someone else** (an `@`-mention of a *different* agent, or a parsed "to X" recipient) and I am not also addressed.

The shipped implementation tests something weaker. The Python gate ([`response_gate.py`](../../agents/response_gate.py), `POLICY_ALWAYS` branch) suppresses iff `mentions` is non-empty, the agent is not named, and [`MENTION_EVERYONE`](../../agents/response_gate.py) is absent; the Go candidate set mirrors it ([`floor_control.go`](../../internal/channels/floor_control.go), `orderResponders`: `directed := len(msg.Mentions) > 0 && !mentioned[MentionEveryone]`). Neither side asks whether any named id **is an agent that could take the floor**. Mentions are shape-validated only ([`sqlite_messages.go`](../../internal/channels/sqlite_messages.go) — participant-ID-shaped, never membership-checked), so the filter fires identically for:

| Mentioned party | Can it take the floor? | Filter today | Result |
|---|---|---|---|
| `participant`/`always` member | yes | suppress others | intended (yield to the addressee) |
| `addressed` member | yes (it was just named) | suppress others | intended — the original pile-on defect ("how about you @ember-owl?") |
| **the human operator** (a `never` member by the [documented join convention](../guides/v0.3.0-demo.md)) | **no** | suppress others | **guaranteed total silence** |
| **an `observer`/`never` agent** | **no** | suppress others | **guaranteed total silence** |
| **a non-member** (typo, external name) | **no** | suppress others | **guaranteed total silence** |
| **the sender itself** (sole mention; "as I, @iron-fox, noted…") | **no** — both sides refuse the sender its own floor ([`orderResponders`](../../internal/channels/floor_control.go) "never reply to self"; the gate's `self_sender` suppress) | suppress others | **guaranteed total silence** |

The last four rows are the defect. Suppression exists to *yield the floor to the addressee*; when the addressee cannot take the floor, yielding produces a floor nobody holds. In the field this presents as the reported symptom: personas answer the human's open-floor prompt (round one), one of them addresses the human by name in its reply, and the channel is structurally dead until the next human message.

## A. The rule — directedness requires a floor-capable addressee

**A mention only directs the floor if the named party could actually take it.**

- **Floor-capable** (relative to a message) ≝ a current channel member, **other than the message's sender**, whose resolved respond policy ([`RespondPolicy.Normalize`](../../internal/channels/channels.go)) is not `never` — i.e. `always` or `when_mentioned` post-normalization. (Equivalently: a member the response gate could admit *for this message* — the gloss that forces the sender exclusion: both sides already refuse a sender the floor on its own message, so a self-mention is one more name that cannot take it, and a policy-only test would reintroduce the silence defect through the sender's own name.)
- **Floor-directing mentions** ≝ `mentions ∩ floor-capable members`.
- Tier A's directed-elsewhere condition changes basis: suppress a non-named `participant` iff the **floor-directing** subset is non-empty (and `@everyone` is absent, unchanged). A message whose mentions name only floor-incapable parties — the human, an `observer`, a non-member, the sender itself — is **open floor** for suppression purposes.

Why the member-*policy* test rather than a participant-*type* (human/agent) test, which the amendment's "different agent" wording might suggest:

1. **Membership rows carry no type.** [`Member`](../../internal/channels/channels.go) is `{ParticipantID, RespondPolicy, JoinedAt, SalienceGated, Threshold}`; the `participant_type` vocabulary ([`participant_type.go`](../../internal/channels/participant_type.go)) rides per-message metadata describing the *sender* only. A type test would need a schema migration; the policy test needs data fanout already holds.
2. **The policy test subsumes the type test.** The human is floor-incapable *because* the documented convention joins humans as `never` — and an `observer` *agent* is exactly as floor-incapable as a human. "Could the gate ever dispatch a reply turn to this id?" is the question suppression actually depends on; agent-vs-human is a proxy for it.
3. **Non-members fall out for free.** A typo'd or external mention resolves to no membership row, hence not floor-capable — no extra rule needed.

Edge semantics, stated explicitly:

- A message naming **both** a floor-capable member and the human ("@iron-fox what do you think? cc @alex") is **directed** — iron-fox is floor-directing; everyone else stays suppressed. The pile-on fix is untouched.
- `@everyone` semantics are unchanged (decision D3 of the relevance amendment): the sentinel always means "do not suppress".
- The named-member admit paths are unchanged and keep reading **raw** `mentions`: a `when_mentioned` member's `mentioned` admit, an `always` member's individually-named `mentioned` admit (the Tier B TB1 lane), and the thread-reply-to-self trigger are not touched by this amendment. Only the *suppression* decision changes basis.
- A mention of an `observer` does **not** wake the observer (OQ 2). `observer` means never; the message simply does not close the floor.
- A **self-mention alongside a floor-capable mention** ("@iron-fox is right, and as I (@ember-owl) said…") is **directed** — iron-fox carries it; the sender's own name contributes nothing either way. Only the *sole*-self-mention message reclassifies to open floor.

## B. What does NOT change

- **The original directedness defect stays fixed.** "how about you @ember-owl?" still names an `addressed` member — floor-capable — so every non-named `participant` is still suppressed. [MT-CHANNEL-RELEVANCE-001](../manual-tests/MT-CHANNEL-RELEVANCE-001.md)'s directed-question assertion is unaffected.
- **Open-floor pile-on control is still the Tier B bid.** Messages this amendment reclassifies from directed to open-floor land in the existing open-floor lane: `participant`s reach the [salience bid](../../agents/salience_bid.py) (bias-to-silence), bare-`always` members reply unconditionally, exactly as for an unmentioned message today. The amendment moves a message class between two existing lanes; it invents no third lane, so [`is_open_floor_admit`](../../agents/response_gate.py) and the Tier B routing are structurally untouched.
- **The idle-cost invariant holds.** Tier A stays free: fanout already loads the member set it needs to resolve the floor-directing subset (no new IO), and the agent-side gate reads a pre-resolved payload field (no lookup). The reclassified messages do add Tier B bids for `participant` members that were previously suppressed for free — that is the *point* (those suppressions were the defect), and the bid is the cheap leased `fast`-model call whose cost posture the relevance amendment already accepted for exactly this traffic class.
- **No DB or schema change.** Membership rows, the `respond_policy` encoding, and the mentions persist-validation are untouched.
- Self-sender, DM override, `never` fail-closed, and unknown-policy branches of the gate are untouched.

## C. Mechanism — resolve at the orchestrator, carry on the wire

Primary enforcement belongs orchestrator-side: only the orchestrator owns membership, and it already sits on the trust boundary for exactly this shape of decision (the cascade-depth amendment's posture: primary enforcement in Go fanout, Python as defence-in-depth backstop). The precedent for the wire shape is `thread_parent_sender_id` — "pre-resolved by the router so the gate need not look the parent up itself" ([`response_gate.py`](../../agents/response_gate.py), thread branch; [`task.proto`](../../proto/task.proto) field 10).

1. **Go (implementation PR 1).** At fanout, resolve `floorMentions := msg.Mentions ∩ {m : m.RespondPolicy.Normalize() != never && m.ParticipantID != msg.SenderID}` once per publish (the member list is already in hand; the sender exclusion keeps the subset sender-relative but still recipient-independent, so per-publish stamping below is unaffected). `orderResponders` becomes `directed := len(floorMentions) > 0 && !mentioned[MentionEveryone]` — only the length operand switches basis; the broadcast guard **stays on raw mentions**, because `@everyone` is a sentinel, not a member, and silently falls out of the intersection (a guard on the resolved subset would be vacuously true, re-suppressing "`@everyone` `@iron-fox`" — the §E matrix pins this row). The off-floor delivery split and mentioned-first ordering keep reading raw mentions (display/admission semantics, not suppression). The subset snapshots membership and policy at publish time; a member that leaves or flips policy between publish and gate evaluation makes it stale. That window is accepted: delivery itself fans out from the same membership snapshot, the gate has no membership view to re-resolve against (the premise of carrying the field at all), and the worst case is a one-message-late reclassification, not a new failure mode.
2. **Wire (implementation PR 1).** A new `repeated string floor_mentions` field on `ChannelMessageEvent` ([`task.proto`](../../proto/task.proto)), stamped per-publish (not per-recipient — the subset is recipient-independent), **plus a `bool floor_mentions_resolved` presence flag, set on every publish by an orchestrator that performed the resolution**. The flag is load-bearing, not belt-and-braces: proto3 `repeated` fields carry no presence semantics, so a subset *resolved to empty* — which is the motivating case, a sole mention of the human — is byte-identical on the wire to the field *never set* by a pre-amendment orchestrator, and the agent-side payload lift ([`server_servicers.py`](../../agents/server_servicers.py) copies `list(request.mentions)` unconditionally; a `floor_mentions` lift could do no better) cannot recover the distinction. Without the flag, the gate must pick one of two wrong readings: treat empty-or-absent as "resolved, open floor" (an old orchestrator's directed messages reclassify — under-suppression under skew, the direction item 3 forbids) or treat it as "absent, fall back" (the resolved-empty human-mention case falls back to raw mentions and stays suppressed — the defect this amendment exists to fix survives its own fix). A bare bool restores the distinction for free: old orchestrators emit the proto3 default `false`, so the fallback needs no version negotiation. (A wrapper message would also carry presence, but the flat flag matches the event's field style — `ChannelMessageEvent` has no metadata map, which is the same reason `cascade_depth` and `thread_parent_sender_id` took first-class fields.) The GRPCMessageDispatcher lifts both into the event payload alongside `mentions`.
3. **Python (implementation PR 2).** The `POLICY_ALWAYS` suppression branch of [`evaluate_response_gate`](../../agents/response_gate.py) reads `payload["floor_mentions"]` for the directed-elsewhere decision **iff `payload["floor_mentions_resolved"]` is true** — keyed on the flag, never on the list's own presence or emptiness, which the wire cannot express (item 2). The `mentioned`/`broadcast` admit checks keep reading raw `mentions`. Flag false or missing (an old orchestrator): the gate falls back to today's raw-mentions basis — degrading toward *over*-suppression, never under-suppression, so the failure direction under version skew is the current behaviour, not a new one. Flag true with an empty list: the message is open floor — the defect fix itself, and the reason emptiness cannot double as the fallback signal.
4. **Drift pins (implementation PR 2).** [`test_cross_language_respond_policy_drift.py`](../../tests/unit/python/test_cross_language_respond_policy_drift.py) gains the field-name + semantics pin (Go resolution and Python consumption agree on "floor-capable = normalized policy ≠ never, excluding the sender") and pins `floor_mentions_resolved` as the basis switch — consumption keys on the flag, never on list presence or emptiness.

The gate cannot independently verify `floor_mentions` (it has no membership view — the same reason it can't compute the subset itself), so this is a deliberate, narrow trust extension to an already-trusted orchestrator-resolved field, identical in kind to `thread_parent_sender_id` and `salience_gated`. A spoofed `floor_mentions_resolved: true` with an empty list on the untrusted gRPC port admits a `participant` to a directed message — the pre-v0.3.7 behaviour, bounded by the same cascade/cost layers that bounded it then; the flag widens the spoof surface by one bool but not the blast radius.

## D. Metrics and observability

- The `channel.messages.gated{policy=always}` counter semantics narrow slightly: a Tier A directed-elsewhere drop now implies a *floor-capable* addressee existed. No label change; the [RFC 0011 §D](0011-channels-bridges.md) bounded-label set is untouched.
- The Go resolution logs (debug) when raw mentions were non-empty but `floor_mentions` is empty — the previously-silent "addressed a non-responder" case — so the reclassification is visible while the change beds in.

## E. Test strategy

- **Go**: `orderResponders` unit matrix — mention of a `never` member (the human convention), of a non-member, of an `observer`, of **the sender alone** (self-mention): candidate set must equal the open-floor set; mention of an `addressed`/`participant` member: unchanged directed behaviour; mixed human+agent mention: directed; **`@everyone` alongside a floor-capable mention: not directed** (the sentinel is a non-member and falls out of the intersection, so this row pins that the broadcast guard still reads raw mentions rather than relying on the resolved subset). Dispatcher stamping: `floor_mentions_resolved` is set on **every** publish — in particular when the resolved subset is empty, the case the flag exists to disambiguate.
- **Python**: gate unit matrix over `floor_mentions_resolved` true/false × `floor_mentions` empty/non-empty × named/not-named, pinning: flag false ⇒ legacy raw-mentions basis even if a list is present; flag true + empty list ⇒ open floor (the motivating human-mention case); flag true + non-empty ⇒ directed for the unnamed; admit paths on raw `mentions` throughout. The matrix deliberately has no present-vs-absent axis for the list itself — the wire cannot express that distinction (§C item 2); the flag is its replacement.
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
