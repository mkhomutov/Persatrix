# Changelog

All notable changes to this project will be documented in this file.

## [0.3.11] - Unreleased

> **Codename:** Conversations that run themselves

### 🚀 Features

- **RFC 0052 PR 1 — the `autonomous` channel config block, with the mandatory cost cap enforced from the first PR (ships dark).** A channel may now carry an `autonomous` block (`enabled`, `topic`, `agenda`, `convener`, `goal`, `max_rounds`) on the RFC 0050 config surface — the foundation of opt-in, human-free convening (RFC 0052, *autonomous agent-only channels*). The safety contract lands with it: an `autonomous.enabled` channel is **rejected at config-validation** unless its resolved `interaction_budget_tokens` cost cap is positive (uncapped autonomy is un-creatable — there is no human to stop a runaway), and its `convener` must be a declared member — not an observer (`respond: never`), and **distinct from** `escalation_chair_id` (the convener owns the agenda lifecycle; the chair keeps its shipped synthesis/close role — RFC 0052 OQ #1). Arming is **group-only** — a DM/thread cannot be made autonomous (convening is an open-floor group concept). The block parses, validates, persists, and round-trips through the REST `GET`/`PATCH …/config` layer (nested merge with per-sub-knob provenance, the `If-Match` revision guard, the `null`-clears tri-state) and is stamped onto the router so the surface reports its effective value. **Dark backend:** nothing convenes yet (self-convening is PR 3), so the block changes no runtime behaviour and **ordinary channels are byte-for-byte unchanged**. The CLI + web config surfaces follow in PR 2. ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))
- **RFC 0052 PR 2 — the CLI + web config surfaces for the `autonomous` block (still dark).** An operator can now create and edit an autonomous channel **without hand-editing YAML**, from both surfaces, exactly as RFC 0051 surfaced its `reasoning` knob. The `persatrix channel config` CLI gains the nested `autonomous.*` dotted knobs (`set planning autonomous.enabled=true autonomous.convener=nova-sparrow autonomous.agenda='Cost, Coupling'`; `get` renders them as `autonomous.<sub>` rows; `unset autonomous.topic` clears one sub-knob), including the first **list-valued** knob — `autonomous.agenda` rides the wire as a `[]string`, comma-split client-side. The web Channel-settings panel renders a new **Autonomous channel** section (an extracted `AutonomousSettings` child component): an enable toggle, free-text Topic/Goal, a multiline Agenda (one item per line ↔ the wire array), a Convener member picker (observer-excluded like the chair), and Max rounds — each with its own provenance badge + inherit/override control, all riding the panel's one sparse `If-Match`-guarded PATCH. A new CLI↔server lockstep guard pins the dotted knob set + wire types (incl. the `[]string` → `list` class) against the Go `mergeAutonomousPatch`. Still **dark** (no convening until PR 3); the cap / convener / group-only gates from PR 1 stay authoritative. ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))
- **RFC 0052 PR 3 — self-convening: the convene action across CLI, REST, and web (the block goes live).** An armed channel can now be **convened** — opened into a human-free discussion with no human message. The convener persona authors the **opening turn** under a fresh interaction, from which the existing response-gate + `InboundEventWake` chain carries the discussion. Convening reuses the shipped dispatch seam: the orchestrator sends a directed **convene forced turn** to the configured `autonomous.convener` (a new `convene` marker on `ChannelMessageEvent`, the sibling of `chair_escalation`), admitted down the same directed lane so the bias-to-silence salience bid never silences the opener; the convener wraps the operator `topic`/`agenda`/`goal` in the RFC 0009 `<external_data>` envelope (operator config is a distinct trust class — the one genuinely new injection surface) with a defensive max-length bound, and renders the convener framing. The action is reachable on **all three** RFC 0050 surfaces — `persatrix channel convene <id>`, `POST /api/v1/channels/{id}/convene` (202 `{channel_id, convener, status}`), and a **Convene** button in the web Channel-settings panel's *Autonomous channel* section (shown only when armed per the saved config, disabled while there are unsaved edits) — each gated behind the **same** `config_edit_enabled` toggle as the config surface (convening triggers real LLM spend on an unattended channel). Convening targets an **idle** channel and fails loudly rather than into a silent gate suppression: a missing channel `404`s (consistent with `…/config`), and an unarmed channel, one that already has a live interaction, one whose roster has no **open-floor responder** besides the convener (only `always`/participant members answer the open-floor opener — a `when_mentioned` member, the default for an unspecified member, never does), or one with no topic/agenda/goal to convene on each `409`, while a convener that drifted out of the roster `400`s. The synthetic sender stamped on the convene directive is structurally **not** a legal participant id, so a convener can never collide with it and have its opener silently self-suppressed. **No new transport, wake type, or store table** — assembly over the publish path. The opening turn resolves uncapped (the wallet snapshots the per-interaction cap at first commit; the always-on Layer-0 depth cap is the net). The mechanisms that make the discussion *bounded and productive* — anti-collapse cadence, the deterministic bounded close, the roster-scaled synthesis reserve, and standing/scheduled convening — follow in the subsequent RFC 0052 PRs. ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))
- **RFC 0052 PR 4a — the close-path backend: roster-scaled synthesis reserve, interaction-closed eviction, and the mandatory-chair gate (ships dark).** The accounting and validation the deterministic bounded close (PR 4b) consumes, landed and unit-tested ahead of the path it serves (the PR 1 → PR 3 backend-then-path precedent). **(1) Roster-scaled synthesis reserve** — new wallet accounting (`internal/wallet/synthesis_reserve.go`) carving a per-interaction cost cap into a **soft** working budget the discussion is bounded by and a **reserve** held back for the close path. The reserve is sized for **`1 + N`** close-path LLM calls — the chair synthesis turn plus one RFC 0020 summary per participating persona (OQ #6 will meter the summary, and the close summary is authored per-agent, so an N-persona roster issues N metered summary calls — hence `1 + N`, not the fixed two an earlier framing assumed) — and clamped to at most half the cap so a tiny cap can never starve the discussion to fund the close. **(2) Interaction-closed eviction** — `WalletService.EvictInteraction` releases the per-interaction running-total residue the shipped wallet never pruned for a capped interaction that settled non-zero spend, so a standing channel will not leak one map entry per convening (PR 7). **(3) Mandatory-chair gate** — an `autonomous.enabled` channel must now declare an `escalation_chair_id` (the role that authors the synthesis turn on close), closing the PR 1 gap where the convener≠chair rule was vacuous with no chair. Enforced on both the YAML load path and the REST apply path; the first-edit baseline freeze symmetrically **drops** an armed-but-un-closeable block (no chair, or a chair drifted out of membership / turned observer) exactly as it already drops an un-convenable one, so a drifted chair never locks an unrelated config edit. **Dark backend:** `AcquireLease` still enforces only the hard cap and nothing consults the reserve/eviction yet — the bounded-close trigger, the OQ #6 close-summary metering, and the chair synthesis turn are PR 4b. ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))
- **RFC 0052 PR 4b-i — the deterministic bounded close: an autonomous discussion now terminates on its own.** The orchestrator-side terminator RFC 0052 §D requires, consuming the PR 4a reserve accounting. A new floor-round-tail trigger (`internal/channels/bounded_close.go`, the `maybeEscalateStall` sibling) closes an autonomous interaction when it crosses a hard bound: **`autonomous.max_rounds`** — the shipped knob had **no enforcement** until now (trigger `structural`) — **or** the wallet **soft budget** (running interaction spend reaches `interaction_budget_tokens` minus the PR 4a synthesis reserve, so the close fires *before* the hard cap would deny the close-path leases; trigger `cost`). On fire it runs the same artifact-bearing teardown the quorum end-vote path produces — it retires the interaction id (the channel is re-convenable and the next publish mints fresh), discards the per-interaction governance state, emits `interaction_closed{trigger=structural|cost}`, and fans the marked close notification so every member produces its **RFC 0020 interaction summary** (the readable artifact). To read the running spend, the router now holds a narrow wallet reference (`SetInteractionSpender`, the router→wallet mirror of the wallet's existing budget-resolver read); a fleet with no wallet leaves the soft-budget trigger inert and `max_rounds` alone bounds the close. **RFC 0030 CE4 is intact** — the chair still cannot close itself; this is an orchestrator trigger. **Scoped to `autonomous.enabled`** — an ordinary channel resolves the disabled default and the hook returns before touching any state, so **human channels are byte-for-byte unchanged** (pinned by a regression test). **Deferred by the maintainer-chosen slice:** the goal-directed **chair synthesis turn** against `autonomous.goal` and the OQ #6 close-summary **metering** are PR 4b-ii — dispatching a re-fanning synthesis turn around the close needs claim/correlation machinery (the ISSUE-0099 resynthesize shape) to keep the chair's synthesis reply from minting a fresh interaction and *reopening* the discussion, which lands with the Python authoring; the wallet **eviction** (PR 4a's `EvictInteraction`) is deferred to PR 7 (standing channels), where the residue leak bites and the schedule timer supplies a settle point for its cross-process precondition. Three correctness points the deep review surfaced ship folded in. **(1)** The close notification reaches the **round-triggering sender too** (the convener/chair whose reply drove the round cast no vote, so nothing else closed its tracker), and the agent-side response gate now lets that *self-addressed* notification close the sender's scope instead of dropping it as a self-echo — so *every* member, the sender included, authors its summary; the marker only outranks the group self-sender defence for a genuine close notification (a re-dispatched chair-escalation or convene stimulus still self-suppresses). **(2)** On the **concurrent fanout path** the bounded close runs *before* the stimulus dispatch and withholds it on close: a single-responder autonomous round (e.g. a two-persona roster, which takes that path even under floor control) can no longer reply-and-reopen the interaction it just terminated — the marked close notification still delivers that final message, ingested as the record's final turn rather than answered. **(3)** The **ISSUE-0099 resynthesize** — the third way a bounding round could reopen the interaction it just closed — is suppressed on close: the escalation chair is mandatory on every armed channel (PR 4a), so a stall escalates and the chair's hand-off routinely misfires onto the operator/observer; that reply's fanout ran the resynthesize re-force *before* the bounded close, and the re-forced turn (dispatched off any floor round) is not a floor speaker, so its reply re-entered `Publish` and minted a fresh interaction. The resynthesize now runs at the fanout tail gated on the bounded-close outcome, alongside the (1)/(2) dispatch decisions, so a sub-bound round re-forces unchanged while a bounding round does not. Review round 5 folds in three more. **(4)** The publish-path **no-reopen latch**: a straggler floor reply landing after its bounding round ended, or a reply drawn by a sub-bound sibling fanout racing the close, re-enters `Publish` *claiming the retired id* — traffic the three fanout-tail guards above cannot see — and used to mint a fresh interaction and re-fan, reopening the unattended discussion; on an autonomous channel such a claim now rides the closed interaction's tombstone into the existing post-close suppression (persisted as the closed record's late final word, no mint, no fanout), while human-channel post-close traffic keeps minting fresh, byte-for-byte unchanged. **(5)** The ISSUE-0099 resynthesize **claim returns to the fanout head** (its pre-4b-i position): parking the claim at the tail let a later innocuous chair message race down the fast concurrent path and steal the once-bound arm with *its* misfire flag while the real forced-turn reply sat in a multi-turn floor round — only the re-force *dispatch* now waits on the bounded-close outcome, so fix (3) holds with the claim ordering restored. **(6)** The concurrent path's withheld bounding dispatch now **clears the responders' presence marks** — no reply can ever re-enter to clear them, the same condition the escalation error branches unmark for — instead of stranding "… is thinking" on the console for the full TTL after the discussion terminated. Review round 8 (the verified PR #716 review) closes two holes in (4)'s own machinery. **(7)** Agent publishes now **echo the interaction id they were dispatched under** as the wire claim (`ActionExecutor.execute`'s origin pair; same-channel replies and end-votes only) — the latch keys on that claim, and **no production reply carried one** (the latch tests hand-stamped it), so every post-close straggler still minted fresh and reopened the discussion, the exact §D runaway (4) was built to stop; chain-origin publishes (tick, chat surface, the convener's opener) stay unstamped so a closed channel remains re-convenable, and the resolver stays authoritative over live claims (IP2). **(8)** The fanout-tail staleness verdict is **ledger-gated**: only a stimulus whose interaction was *deliberately* closed — the no-reopen ledger, or losing the closing race to a standing tombstone — is withheld (and now also skips the stall-escalation/resynthesize revival tails), while a benign resolver interleaving (the orphan-park artefact, an idle rotation) dispatches again instead of silently swallowing a live committed message; a bound-crossing fanout that loses the tombstone CAS reports the logged stale shape rather than masquerading as the close, with the racing sibling's committed-but-undelivered message a documented accepted loss until the 4b-ii redelivery marker. Review round 10 closes two more. **(9)** The bounded close's `structural`/`cost` triggers are now in **both close-trigger allowlists** (Go `readPreviousClose`, Python `WIRE_CLOSE_TRIGGERS`) — they were missing while the close stamped them, and because the OQ 5 pair validates both-or-neither the omission zeroed `previous_interaction_id` too, silently disarming the `predecessor_wire_id` straggler defence on every bounded-close boundary (the drift pin stayed green because it never parsed `bounded_close.go`; it does now). **(10)** The ISSUE-0065/0066 **chat-error recovery reply now echoes the no-reopen claim** — the third same-channel publish path, missed when (7) stamped replies and end-votes: a budget-denied member's error reply lands under exactly the spend pressure that fires the cost close, so the unstamped straggler minted fresh and re-fanned the just-terminated discussion. Round 10 also folds in the review's cleanups: the executor's origin threading collapses into one required `DispatchContext` (a claim-less publish is a `TypeError` at the call site, not a silent kwarg default), the three byte-identical interaction-id readers delegate to the one drift-pinned `wire_interaction_id`, and the fanout resolves the autonomous config once per publish (a single tear-safe snapshot, the `ReasoningFor` posture) and branches on the head staleness verdict instead of re-deriving it at the tail. One known limit ships documented rather than fixed: the **floor-path** close notification re-delivers a bounding stimulus every member already ingested live inside the round (one duplicate final turn + `turn_count` inflation per non-sender member per close); distinguishing re-delivery from sole delivery needs a typed **redelivery marker** on the wire, which lands with PR 4b-ii's proto work. ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))
- **RFC 0052 PR 4b-ii — the goal-directed chair synthesis turn: a bounded close now ends with a readable synthesis, and the close summaries count toward the cap.** The §D "always produce an artifact" contract's user-facing half, riding the PR 4b-i deterministic bounded close via a **close-on-reply** ordering. When an autonomous discussion crosses its bound (`max_rounds` or the wallet soft budget), a chaired channel no longer closes inline: the orchestrator dispatches a **synthesis forced turn** to the `escalation_chair_id` (a new `synthesis_turn` marker — the `convene` sibling: synthetic non-participant sender so the gate's self-sender defence can never silence it, operator `goal`/`topic` in the content wrapped agent-side in the RFC 0009 `<external_data>` envelope with the convene-precedent length bound, and the closing interaction's id stamped as the wire claim), arms the close, and **withholds all further discussion traffic** — the discussion has terminated; only the closing artifact is outstanding. The chair's reply, recognised at the fanout head by the claim it echoes (the PR 4b-i origin pair — the ISSUE-0099 correlation shape the 4b split waited for), **is the closing artifact**: it runs the same artifact-bearing teardown with the synthesis as the closing message, so every member ingests the goal-directed synthesis as its record's final turn and then authors its RFC 0020 summary. The chair proposes, the **orchestrator** disposes — CE4 intact. Termination never waits on a model: a missing/drifted chair, a failed dispatch, or a reply that never arrives (gate drift, a hard-cap lease denial, a provider error) degrades to the 4b-i immediate close via a timeout net (default 120s; the new `channel.conversation.synthesis_turn{outcome}` counter makes the reply-vs-timeout ratio the §D health signal), and a racing end-vote quorum keeps its supremacy (the orphaned synthesis lands as the closed record's tail, never a reopen). **OQ #6 lands with it:** the RFC 0020 close summary — verified unmetered at plan-opening — now draws a wallet lease on the autonomous bounded close, billed to the governance interaction id the close notification carries, so all **N** per-persona summaries (and the chair turn, whose directive claims the same id) count toward the mandatory cap the PR 4a `1 + N` reserve was carved for; human/end-vote/idle closes keep the unleased call byte-for-byte. **The proto opens for two more typed fields** (the wire work 4b-i deferred): `close_notification_redelivery` — the floor-path bounded close's notification no longer double-ingests a closing message every member already received live (one duplicate final turn + `turn_count` inflation per non-sender member per close, the documented 4b-i limit, now gone) — and `close_notification_close_trigger`, whose presence doubles as the metering key and whose `cost` value now closes the local record with the truthful cost cause instead of the 4b-i `REASON_STRUCTURAL` fallback. Both additive: an old producer/consumer keeps the pre-4b-ii behaviour on every field, and a new cross-language drift pin holds the proto/Go/Python triple in lockstep. ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))
- **RFC 0052 PR 5 — the Phase-1 acceptance suite + `MT-AUTONOMOUS-001`: the human-free brainstorm contract, proven end to end.** Where PR 1–4b pinned each mechanism in isolation, the new integration suite chains them against a **real wallet** wired to the router in both directions (the production `SetInteractionSpender` ↔ `SetInteractionBudgetResolver` coupling — the first test to close that loop): the **full-cycle leg** runs convene → discussion → structural bound → chair synthesis turn → close-on-reply → per-persona metered summaries with **zero human turns** (asserted over the committed history, not just by construction) and total spend ≤ the mandatory cap, then re-convenes the retired channel; the **no-runaway leg** puts an adversarial "everyone always wants to talk" roster on the self-amplifying concurrent path and proves the discussion still terminates at `max_rounds` with exactly one close, one synthesis turn, and a dispatch count bounded by the round bound — the RFC §Security no-runaway measurement; and the **close-by-budget leg** (a ≥2-persona roster) drives real leases exactly to the soft threshold and proves the `1 + N` reserve earns its keep — at the moment the cost close fires, a discussion-sized lease is already fail-closed **denied** by the hard cap, while the chair synthesis turn **and every persona's OQ #6-metered close summary** still lease from the held-back reserve, with the budget snapshot verified to survive the close (so the summaries are genuinely metered, not silently uncapped). The Python half chains the per-persona close-artifact contract across both bounded causes: truthful `cost`/`structural` close reason → the synthesis ingested as the record's final turn → the metering mark → one leased summariser call per persona billing the **shared** governance id → a **real** RFC 0020 summary, never the `[interaction summary unavailable]` placeholder — including the floor-path redelivery skip (no duplicated final turn), the round-triggering sender's self-echo close, and the human-close regression (unmarked closes stay unmetered with the unleased call signature byte-for-byte). `MT-AUTONOMOUS-001` documents the live acceptance — arm (both safety gates re-proven), convene from the CLI, watch it converge/terminate/synthesize untouched, read every persona's real summary, check spend ≤ cap, re-convene from the web button — with the live run scheduled for release-prep; the `workflow-execution` diagram gains the autonomous-brainstorm sequence as the third runtime path. ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md); [MT-AUTONOMOUS-001](docs/manual-tests/MT-AUTONOMOUS-001.md))
- **RFC 0052 Phase-1 deep-review follow-ups — two §D artifact-safety fixes + a doc-truth pass.** A deep review of the merged Phase 1 (PRs 1–5) surfaced two ways the "always produce an artifact" guarantee could still fail silently, both fixed here with regression tests. **(1) Convene now pre-flights the escalation chair.** `ConveneChannel` re-validated the convener, audience, and subject but never the chair — so a channel whose mandatory chair drifted out of the roster after arming (`RemoveMember` touches neither the block nor the chair knob) convened with a `202` and then closed with **no goal-directed synthesis turn** (`maybeArmSynthesisClose` degrades a drifted/observer chair to `synthesisUnavailable`). Convene now re-checks the chair as defence-in-depth, the mirror of the drifted-convener guard (`classifyEscalationChairMember`; a `when_mentioned` chair is still accepted — the forced-turn marker lifts its gate), and a drifted/observer chair fails loudly (`ErrAutonomousChairUnavailable` → `400`). **(2) The floor-path bounded close no longer dispatches into a discussion it just closed.** The fanout took its terminating-state verdict at the head, but the floor round then parked on the per-channel floor queue (`floorRegistry.acquire`, up to minutes behind a prior round) — long enough for a sibling fanout's bounded close or armed synthesis turn to land, after which the parked round woke and poured a full multi-speaker LLM round into the terminated/armed discussion (the wasted spend + mid-synthesis stimuli the close-before-dispatch ordering exists to prevent). `floorRound` now re-checks the verdict **after** acquiring the floor and abandons pre-speaker, treated exactly like a head-stale round; human channels stay a byte-for-byte no-op (`!a.enabled` short-circuits the read). **Plus a documentation-truth pass** correcting stale claims the review found: the bundled `config/channels.yaml` example channel now ships **disarmed** (`enabled: false`) with a truthful comment — as of PR 3 the convene path is live, so an armed example was convenable via one `POST …/convene` on a default deployment (behind the bundled `config_edit_enabled`) under comments still calling it "dark/inert" (the same stale claim is corrected in `channel.schema.json`, `channel_types.go`, and `channel_routes.go`); the wallet's `synthesis_reserve.go` / `interaction_budget.go` comments now state that the reserve is a router-side trigger point rather than wallet-enforced funding (a crossing floor round or armed-window straggler can still spend it — a reserve-**preservation** gap distinct from the two documented sizing gaps), that `EvictInteraction` is owned by PR 7 (not PR 4b) and still uncalled, and that the per-interaction residue leak is now **live** (not latent) since autonomous convenings track capped interactions; and `cost_close.py` documents why the Layer-1 cost close leaves its summary unmetered while the reserve is dark (metering it against the exhausted cap would degrade a real summary to the placeholder). ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))
- **RFC 0052 PR 6 — anti-collapse cadence: the convener keeps a human-free discussion alive without un-doing bias-to-silence.** The RFC §C design heart. With no human present, the v0.3.x realism arc's bias-to-silence + one-shot chair escalation compose into premature death — every persona reasons "the others can cover this", all stay silent, the single chair escalation fires and its hand-off also draws silence, and an unattended channel converges to a near-empty transcript with no synthesis. The counter-pressure is a **convener cadence** (a role **distinct** from the chair — RFC 0052 OQ #1): on a stalled floor round with the agenda not yet exhausted, the orchestrator dispatches a forced turn to the `autonomous.convener` that **advances to the next agenda item** (or re-poses an item the room passed on the first quiet round), giving the room something concrete to react to. This **generalizes** the shipped CE5 one-escalation-per-*interaction* ration to **one per agenda item** (the chair's own one-shot ration stays untouched) while **preserving the CE5 loop guard**: the agenda cursor is **monotonic**, so an item is never re-posed once advanced past and the convener **re-invites any one item at most once** — total convener turns are linear in agenda length (one introduction plus at most one re-invite per item), a hard ceiling, not an open loop. The cadence **takes precedence** over the chair escalation while the agenda has items (anti-collapse before convergence) and **falls through** to the shipped chair escalation only when the agenda is exhausted, which then proposes synthesis-and-close (§D); a best-effort **liveness target** (shipped at its default of one substantive turn per item) re-invites an under-discussed item once rather than skipping it on the first quiet round, and in the worst case — a genuinely silent roster — the target and the ceiling collapse to the same event. **Silence stays semantic** — the cadence does not lower the RFC 0051 threshold (no return of pile-on); it raises salience honestly by giving the convener a concrete next thing to ask. It **reuses the §B convene lane end to end** (the `convene` wire marker → the gate's forced-turn admit → the convener framing), so it needs **no new proto field, gate rule, or prompt snippet** — the forced turn carries the synthetic convene sender (never self-suppressed) and the open interaction id (its lease bills to the cap; the convener's reply resolves back into the same interaction). A new `channel.conversation.convener_advance{outcome ∈ advance|reinvite|dispatch_error}` counter makes the per-item ration observable. **Scoped to `autonomous.enabled`** (RFC 0052 OQ #2) — a human channel keeps the shipped CE5 one-shot ration and bias-to-silence **byte-for-byte** (a stalled human round dispatches no convener turn and the chair escalation fires unchanged, pinned by a regression test). `MT-AUTONOMOUS-002` documents the live acceptance — a collapse-prone roster worked through a multi-item agenda; termination never depends on the cadence (the deterministic bounded close backstops it regardless). ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md); [MT-AUTONOMOUS-002](docs/manual-tests/MT-AUTONOMOUS-002.md))
- **RFC 0052 PR 7a — the standing/scheduled config backend, with the mandatory aggregate bound enforced from the first PR (ships dark).** Phase 3 (RFC 0052 §E) opens with its config foundation, the §E mirror of the PR 1 backend-then-surface split. A channel's `autonomous` block gains three sub-knobs — `schedule_interval_seconds` (the RFC 0024 timer interval that makes the channel **standing**: the convener is re-woken to open a fresh discussion on a cadence — 0, the default, is a one-shot channel), and the aggregate bound `max_convenings` (a convening-count ceiling) and `standing_budget_tokens` (a standing-window cost budget). The safety contract lands with them, exactly as PR 1's per-interaction cap did: a **standing** channel re-opens a fresh, **separately capped** interaction on every fire, so the per-interaction `interaction_budget_tokens` cap does **not** bound its recurring total — so an armed channel with a positive `schedule_interval_seconds` is **rejected at config-validation** (`ErrAutonomousStandingBoundRequired`, a 400) unless it declares `max_convenings` and/or `standing_budget_tokens`. A one-shot armed channel (no schedule) needs no aggregate bound — the cap alone bounds it. The gate is enforced on **both** the YAML load path and the REST apply path (a shared predicate so the two cannot drift), and the three sub-knobs parse, validate (negatives rejected per-field), persist, and round-trip through the REST `GET`/`PATCH …/config` layer + the CLI `channel config` dotted knobs (`autonomous.schedule_interval_seconds` etc., kept in lockstep with the server merge switch by the source-parsed guard) and are captured by the first-edit / reconcile freeze so a revisioned standing channel survives the boot round-trip. **Dark backend:** nothing fires the timer or counts convenings yet — the config-round-trip into the convener's `agents.yaml` timer set + the convening counter are a later PR; **ordinary and one-shot autonomous channels are byte-for-byte unchanged**. ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))
- **RFC 0052 PR 7b — the aggregate convening ceiling goes live: `max_convenings` now stops re-convening.** PR 7a required a standing channel *declare* an aggregate bound but left it dark — nothing counted convenings. This slice makes the `autonomous.max_convenings` half a **runtime ceiling**: the router keeps a per-channel convening count and `ConveneChannel` refuses once the count reaches `max_convenings`, returning **`ErrAutonomousConveningBoundReached` → 429 Too Many Requests** (the aggregate-quota sibling of the RFC 0030 Layer-2 participant-budget 429). The count is taken with a **reserve-before-dispatch / release-on-miss** split, so only an opener that actually **dispatches** consumes a slot — a flapping/unreachable convener endpoint whose dispatch fails can never silently exhaust the budget (the release covers a dispatch miss only: an opener that lands but whose interaction never commits still consumes its slot) — and the check-and-reserve is atomic under one lock, so two concurrent convenes at the ceiling cannot both slip through. The count tracks opener-**dispatches**, **fail-closed**: it can only refuse *early*, never exceed `max_convenings`, and it does **not** close the two-openers idle race (a raced burst can burn a slot per opener while their openers fold into one interaction — that race is the force-fresh slice's, still deferred), so `max_convenings` is a fail-closed ceiling, not an exact discussion count. It gates **both** the manual convene path (today) and the timer-fired path (the next slice), so auto-convening is never wired ahead of the ceiling that bounds it — the §E mirror of PR 1's cap-required-before-convene ordering. The count is **per-process** in-memory state — a restart resets it to zero, so the aggregate bound holds within one process lifetime, **not** across the standing window §E's safety framing targets (a durable count would need persistence, which RFC 0052 rules out — "no new store migration" — so the across-restart bound is a tracked follow-up; latent while convene is manual-only, load-bearing once the timer fires unattended); it is **not** refilled by disarm/re-arm within a process (the conservative posture); a channel **delete** clears it (no map-entry leak). A channel with `max_convenings` unset (0) — a one-shot channel, or a standing channel bounded only by `standing_budget_tokens` — is never count-gated, and its count is still tracked for the web readout a later slice surfaces; the `standing_budget_tokens` aggregate-spend ceiling and the config-round-trip timer seam are the remaining PR 7b slices. **Ordinary and one-shot channels are byte-for-byte unchanged.** ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))
- **RFC 0052 PR 7b (readout) — the convening count reaches the operator: a live convening / aggregate-bound readout on the config surface.** PR 7b-i made `max_convenings` a runtime ceiling but left its count **invisible** — surfaced only in tests. This slice reports it on the RFC 0050 read surface: the `GET …/config` payload gains an `autonomous_runtime` block carrying `convening_count` (openers this channel has dispatched **this process lifetime** — the count resets on restart, matching the ceiling's own scope) and `convenings_remaining` (the `max_convenings` allowance left). The remaining allowance is derived **server-side** so the two rules live in one place: JSON `null` when there is no positive `max_convenings` to count against (unbounded), and **clamped at zero** when a *lowered* `max_convenings` (a legal RFC 0050 edit) sits below the already-spent count — never a negative — so the clients only render, never recompute. The web **Autonomous channel** panel shows a `Convenings: N used, M remaining` line beside the Convene button (armed channels only — a non-autonomous channel has no convening story), and `persatrix channel config get` prints a trailing `convenings` row tagged `(runtime)` so it never reads as a config knob missing its `[channel]`/`[default]` provenance. **Read-only observability** — no new enforcement — and the runtime block is **additive**: an older payload without it still decodes (count 0 / unbounded), so no client breaks. **Still remaining in PR 7b:** the config-round-trip timer seam. ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))
- **RFC 0052 PR 7b — the aggregate standing SPEND ceiling goes live: `standing_budget_tokens` now stops re-convening.** The §E aggregate bound has two halves; PR 7b-i activated the convening COUNT (`max_convenings`), and this slice activates the token-SPEND half. A standing channel re-opens a fresh, **separately-capped** interaction each fire, so the per-interaction `interaction_budget_tokens` cap leaves the recurring TOTAL spend unbounded — `standing_budget_tokens` is that total's ceiling. Each interaction **close** folds its settled discussion spend into a per-channel running total, and `ConveneChannel` refuses a fresh convening once that total reaches the budget, returning **`ErrAutonomousStandingBudgetExhausted` → 429 Too Many Requests** (the aggregate-spend sibling of the count ceiling's 429). This lets a channel be bounded by cost instead of / alongside the count: a channel with `max_convenings` unset (`0`) but a positive `standing_budget_tokens` is now spend-gated (previously declared-but-dark at runtime). The fold rides the interaction-close notification and reads the wallet's per-interaction running total, so a **$0/mock fleet with no wallet wired leaves the gate inert** (the same posture as the bounded-close soft-budget trigger). Two deliberate, fail-**safe** scope limits, the siblings of the count ceiling's: the total is **per-process** in-memory state (a restart resets it; not refilled by disarm/re-arm; a channel **delete** clears it — no map-entry leak), so the bound holds within one process lifetime, **not** across the standing window (a durable total would need persistence, which RFC 0052 rules out — a tracked follow-up); and only the **discussion** spend is folded at close — the per-persona RFC 0020 close summaries settle asynchronously cross-process **after** the close, so the folded total can under-count by up to the close-path reserve (`1+N` calls), refusing at-or-slightly-late with the per-window over-run bounded by that reserve per convening (and the co-declared count bound caps the number of convenings). This slice deliberately does **not** evict the wallet residue (that stays exactly as PR 4b-i left it — the cross-process settle barrier the eviction needs is the timer slice's concern); folding a router-side running total is orthogonal to pruning the wallet's per-interaction map. **Still remaining in PR 7b:** the config-round-trip timer seam that fires the schedule + `MT-AUTONOMOUS-003`. **Ordinary and one-shot channels are byte-for-byte unchanged.** ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))
- **RFC 0052 PR 7c-i — the config-round-trip timer PRODUCER: an armed standing channel now derives its convener wake spec (ships dark).** §E resolves the schedule wiring (OQ #4) as a **config round-trip**, not a runtime `RegisterTimer` API: RFC 0024 timers are `agents.yaml`-canonical, so a channel's `schedule_interval_seconds` reaches the convener by registering an `autonomy.timers` entry in the convener persona's config. This slice lands the **producer** of that entry — `ChannelRouter.StandingConveneTimers()` derives, from every armed standing channel in the resolved autonomous registry, the `ConveneTimerSpec` (convener id + a schema-valid, reversible timer id + the `convene` callback kind + the interval) the round-trip must register. Two contracts are pinned: the timer id **satisfies the `agents.yaml` timer-id schema pattern** *and* **reverses** to the channel id (`ParseStandingConveneTimerID`) — load-bearing because a fired RFC 0024 `ScheduledWake` carries only `timer_id`/`callback_kind` (no channel id), so the convener-side handler must recover the channel to convene from the id alone; and **only an armed standing channel yields a timer** (a one-shot, disarmed, convener-less, or aggregate-unbounded block derives nothing — the producer refuses to arm a schedule looser than the §E bounds), so the round-trip never arms a schedule the operator did not declare. **Dark producer:** nothing consumes the specs yet — the `agents.yaml` writer + the convener-side `ScheduledWake(convene)` → `ConveneChannel` handler (so a fired schedule passes the **same** §E aggregate ceilings PR 7b built for the manual path) are **PR 7c-ii**, which also carries `MT-AUTONOMOUS-003`. **Ordinary and one-shot channels are byte-for-byte unchanged.** ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))
- **RFC 0052 PR 7c-ii-a — the convener-side convene-wake handler: a fired `convene` timer now re-opens its channel (ships dark).** PR 7c-i landed the producer (a `ConveneTimerSpec` per armed standing channel); this slice lands the **consumer** half of the fire path. The convener's tick scheduler now branches on the RFC 0024 `ScheduledWake.callback_kind`: a **`convene`** wake is a re-open signal, not a heartbeat, so it routes to a dedicated handler and **never** falls through to the ordinary idle/LLM tick (running a tick on it would both misfire — the convener would *deliberate* instead of *convene* — and spend unbudgeted on an unattended channel). The handler recovers the group channel from the timer id alone (`parse_standing_convene_timer_id`, the Python inverse of the producer's encoder — a fired wake carries **no** `channel_id`, so the id is the only reference; a cross-language drift guard pins the shared charset/prefix against the Go source) and POSTs the orchestrator's `/api/v1/channels/{id}/convene` — the **same** endpoint the CLI verb and web button hit, so the fired schedule passes the **same** §E aggregate ceilings (`max_convenings` / `standing_budget_tokens`) the manual path does; those bounds live server-side in `ChannelRouter.ConveneChannel`, so routing through the endpoint is what keeps an unattended timer bounded, not a caller-side re-check. Every failure is **log-and-drop** so the event loop survives to the next fire, but the log **classifies** the decline rather than blanket-labelling it benign: a §E bound (429), a state conflict (409: a live prior convening / unarmed / no-audience), or a retryable convener miss (503) is logged as an *expected* outcome on an unattended channel; any **other** answered status — a drifted-chair / invalid convener (400), a gated-off convene surface (403), a channel deleted since the timer was armed (404), or a server error (500) — is logged **distinctly as actionable**, because it recurs every fire until an operator fixes it and is exactly the runaway-class condition §E's observability exists to surface (it must not read as an "expected" decline); a transport failure that never reached the orchestrator keeps its traceback. **Consumer-first is the safely-dark ordering:** registering a `convene` timer *before* this handler existed would have fired wakes the scheduler treated as ordinary ticks (it ignored `callback_kind`), so the handler lands first and the `agents.yaml` writer that actually registers the timer + `MT-AUTONOMOUS-003` are **PR 7c-ii-b**. Still dark: nothing registers a `convene` timer yet, so no convene wake is produced in a running fleet; the legacy/idle tick path is **byte-for-byte unchanged**. ([RFC 0052](docs/rfcs/0052-autonomous-agent-channels.md); [PR plan](docs/rfcs/0052-pr-plan.md))

## [0.3.10] - 2026-06-27

> **Codename:** Conversations worth posting into

### Highlights

- **Personas now think before they speak — semantic silence ships on by default once a channel is governed.** On an open-floor turn a persona privately decides *whether* the turn is worth a post; if it would only be agreeing, restating, or piling on, it stays silent *with a reason* instead of posting. This generalizes the RFC 0030 Tier-B salience bid into a structured `{ should_post }` verdict on the same leased `fast`-model seam — the idle path stays free, and the private trace never becomes a message, never reaches the channel store, and is never visible to another persona (audit-only). The plan-threaded *considered* compose (`mode: plan`) is a one-rung-up, per-channel operator opt-in. ([RFC 0051](docs/rfcs/0051-reasoning-before-posting.md))

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| **[Behaviour — default flip for governed channels]** `reasoning.mode` default `off → bid` | A channel with a salience-gated (`participant` / `chair`) member **defaults to `reasoning.mode: bid`** out of the box — semantic silence on open-floor turns (the structured `{ should_post }` verdict supersedes the numeric score; a turn with nothing to add ends in silence *with a reason*). An **ungoverned** channel is unchanged (the knob is inert there; a non-`off` mode is rejected at `validate`). The flip only fills an **absent** mode — an explicit `reasoning.mode: off` in config is preserved. See [RFC 0051 §G](docs/rfcs/0051-reasoning-before-posting.md). |
| **[Kill switch — one flip]** `reasoning.mode: off` restores the prior score gate | Setting a channel to `reasoning.mode: off` restores **byte-for-byte** the prior RFC 0030 scalar score-gate behaviour at runtime (no restart) and is preserved across a boot. The deliberation egress goes flat (`deliberation.*` emits nothing; the scalar `below_threshold` gate runs instead). The one-flip kill switch is the escape hatch for any deployment that wants the old numeric gate back. |
| **[Behaviour — supersession under bid/plan]** Per-member `threshold` superseded | Under `mode: bid` / `plan`, the `{ should_post }` verdict **is** the silence decision — the per-member numeric salience `threshold` no longer governs the speak/skip call. A deployment that tuned `threshold` floors gets **semantic silence** instead; pin `reasoning.mode: off` to keep the numeric gate. |
| **[Capability-gated — rejected at validate]** `reasoning.depth: deep` not yet deployed | `reasoning.depth: deep` (native extended thinking) needs a provider-protocol change and is telemetry-gated on [RFC 0051 OQ 1](docs/rfcs/0051-reasoning-before-posting.md#open-questions); it is **rejected at `validate`** as an unbacked enum value (a 400, no silent downgrade) until RFC 0051 Phase 4 builds it. `shallow` is the only backed depth this release. |
| **[Opt-in — gated to `mode: plan`]** `reasoning.revise: 0\|1\|2` reflexion loop | The critic→revise reflexion loop is **off by default** (`revise: 0`). `revise ≥ 1` is accepted **only under `mode: plan`** (the critic re-reads the draft against the plan) and **capped at 2** — a `revise ≥ 1` on any other rung, or `> 2`, is a 400. The loop is fail-soft: a hiccup keeps the last good draft and never blocks the post. |
| **[Wire — additive, forward/backward compatible]** `reasoning_mode` / `reasoning_revise` proto fields | `ChannelMessageEvent` gains two additive fields — `reasoning_mode = 25` (string) and `reasoning_revise = 26` (int32) — carrying the channel's resolved rung on the orchestrator → agent dispatch. New field numbers → a pre-v0.3.10 producer leaves them empty/0, which the agent maps to `off` / single-pass, so both are additive across a mixed-version deployment. |

**No store migrates this release.** Unlike v0.3.9 (two channel-store migrations), v0.3.10 advances **no store** — the channel store stays at **v10** and the persona-memory store is untouched. There is **no migration release gate** and no upgrade-on-open step; an already-v10 channel store is a no-op open.

**Version-skew / upgrade-ordering caution.** The orchestrator owns the resolved-rung stamp and the governed-channel default flip. **Upgrade the orchestrator together with (or before) the agents** to arm the flip and the reflexion opt-in; the reverse skew is safe (the fields are additive and default to the prior behaviour — a pre-v0.3.10 agent talking to a v0.3.10 orchestrator simply ignores the new fields).

### 🚀 Features

- **RFC 0051 go-live (PR 6): the resolved `reasoning.mode` rung reaches the agent over the wire, and the governed-channel default flips `off → bid` in lockstep with the kill switch and telemetry.** The router stamps the channel's resolved rung onto every dispatch (`ChannelMessageEvent.reasoning_mode`); the salience seam reads it to pick the bid's verdict grammar (scalar gate / structured silence verdict / plan-threaded compose). Governed channels resolve to `bid` by default at load, boot, runtime-create, and runtime-edit; an explicit `off` is preserved as a kill switch across all of them. New deliberation telemetry — `deliberation.total` (rate), `deliberation.suppressed` (silence charted by `reason_code` + `mode`), `deliberation.duration` (latency histogram), and `deliberation.budget_starved` — makes the active default safe to run. The reasoning cluster — the structured silence verdict + the seam threading + `agent.deliberated` audit, the plan-threaded compose + no-leak test, the `reasoning` config backend, the CLI + web config surfaces, and the `off → bid` go-live flip + telemetry — landed across [#692](https://github.com/mkhomutov/Persatrix/pull/692)–[#697](https://github.com/mkhomutov/Persatrix/pull/697). See the [RFC 0051 PR plan](docs/rfcs/0051-pr-plan.md).
- **RFC 0051 reflexion (PR 8, Phase 5a): an opt-in critic→revise pass that sharpens a `mode: plan` post before it ships, default off.** Under `reasoning.revise: 1|2` a cheap `fast` **critic** re-reads the composed draft against the private plan and, only if it flags weakness, a `quality` **revise** rewrites it — bounded to the round count and **fail-soft** (a parse/critic failure or an exhausted lease keeps the last good draft; the post is never blocked). The critic adds no compose unless it flags weakness, so an `N`-round post costs up to `N+1` quality composes, hard-capped at 2 and metered against the same interaction lease. The discarded drafts and critic notes are walled exactly like the plan (never a message, never persisted) — the privacy wall over both reflexion intermediates is pinned by the no-leak integration test (PR 9). The resolved count rides the wire (`ChannelMessageEvent.reasoning_revise`); `validate` gates `revise ≥ 1` to `mode: plan`. Default `revise: 0` keeps single-pass the norm. Operator telemetry charts the opt-in loop: `reflexion.runs` (by `outcome=revised|noop|degraded` — the draft-changed signal, with a fail-soft hiccup kept separable from a strong-draft no-op so a silent critic/budget outage stays alertable) and `reflexion.rounds` (the revise-round counter). ([#698](https://github.com/mkhomutov/Persatrix/pull/698); the no-leak wall + revise telemetry + closeout [#699](https://github.com/mkhomutov/Persatrix/pull/699))

### ⚡ Performance

- **RFC 0034 Phase 3: the persona conversation-window fetch cache is now bounded, and the window is instrumented.** The in-process cache that fronts the per-turn transcript fetch was unbounded — over a long-lived orchestrator serving many channels it grew without limit. It is now a bounded LRU (process-global capacity, `agents/persona_runtime/_conversation_window_cache.py`). New OTEL metrics make the window tunable from data: `conversation_window.cache_access` (`result=hit|miss`), `conversation_window.cache_evictions`, `conversation_window.fetch_duration`, and `conversation_window.fallback` (`reason=fetch_failed|fetch_none` — the silent degrade-to-current-event-only that could otherwise mask a history-endpoint outage). The `max_turns` / `max_tokens` defaults are unchanged; their data-driven retune is the one remaining follow-up, gated on collecting this telemetry. ([RFC 0034](docs/rfcs/0034-persona-conversational-working-memory.md); [#700](https://github.com/mkhomutov/Persatrix/pull/700))

### 📚 Documentation

- *(operator/persona guides + README/ROADMAP)* The [persona-agents guide](docs/guides/persona-agents.md) §*Reasoning before posting* (the `reasoning.{mode,model,depth,revise}` knob — `bid` = semantic silence on by default once a channel is governed, `plan` = the considered plan-threaded compose, `off` = the one-flip kill switch; the silence reason observable at `reason_code` granularity; the `depth: deep` deferral + the `revise` gating to `mode: plan`) and the [memory-architecture diagram](docs/diagrams/memory-architecture.md) note that the walled reasoning trace is **deliberately outside** the memory diagram were authored during implementation ([#699](https://github.com/mkhomutov/Persatrix/pull/699)) and verified against the shipped config surface (the RFC 0050 `reasoning` knob, `persatrix channel config`, web `ChannelSettings`) and the resolved-rung router; the README Roadmap row + the ROADMAP Version Map / RFC Master Index (RFC 0051 + RFC 0034 → `✅ Implemented`) were refreshed and the [v0.3.10 release checklist](docs/v0.3.10-release-checklist.md) landed ([#705](https://github.com/mkhomutov/Persatrix/pull/705)).
- *(v0.3.10 planning + release-prep)* The v0.3.10 [master plan](docs/v0.3.10-plan.md) + the [RFC 0051 PR plan](docs/rfcs/0051-pr-plan.md) ([#691](https://github.com/mkhomutov/Persatrix/pull/691)), the [release-prep plan](docs/v0.3.10-release-prep-plan.md) ([#702](https://github.com/mkhomutov/Persatrix/pull/702)), and the [MT execution report](docs/manual-tests/v0.3.10-execution-report.md) ([#703](https://github.com/mkhomutov/Persatrix/pull/703); the `agent.deliberated` audit `extra=` egress fix [#704](https://github.com/mkhomutov/Persatrix/pull/704)).

### 🧪 Testing

- The headline manual test [`MT-REASON-001`](docs/manual-tests/MT-REASON-001.md) *reasoning before posting — semantic silence with a reason, a plan-threaded considered post, and the walled private trace* was executed **live on Anthropic** against the `main` RC tip: an **80% semantic suppress-rate** under `bid` (`deliberation.suppressed{reason_code=nothing_to_add}` = 8 / `deliberation.total` = 10), plan-threaded considered posts with the private plan markers **absent** from the transcript **and** from both peers' `?as_participant` recall, a reflexion `revised`×2 with the discarded draft walled, and the one-flip `off` kill switch zeroing the deliberation egress and surviving a boot; `reasoning.depth: deep` / `revise ≥ 1` outside `mode: plan` (and `> 2`) rejected at `validate`. See the [execution report](docs/manual-tests/v0.3.10-execution-report.md).
- Structural release-blocker gates green on the RC tip (live on host): the **privacy-wall no-leak integration test** ([`tests/integration/test_deliberation_no_leak.py`](tests/integration/test_deliberation_no_leak.py) — pins zero `messages` rows + zero appearance in a second persona's reconstruction for **both** the private `CompositionPlan` *and* a discarded reflexion draft + critic note, the load-bearing correctness gate), the `off → bid` go-live + kill-switch + capability-gate + reconcile-survives-boot + proto-stamp Go suites ([`config_reasoning_test.go`](internal/channels/config_reasoning_test.go) / [`channel_config_reasoning_test.go`](internal/server/channel_config_reasoning_test.go) / [`fanout_salience_test.go`](internal/channels/fanout_salience_test.go) / [`grpc_dispatcher_salience_test.go`](internal/channels/grpc_dispatcher_salience_test.go)), the Python deliberation / plan / reflexion + idle-cost telemetry suites, and the RFC 0034 P3 cache/instrument suite. The carried-forward recall / governance / convergence + alias cost-attribution + session/epoch isolation gates stay green, and the **cost-regression** gate confirms reasoning is net-neutral-to-saving — it reuses the existing leased `fast`-model seam, **saves** the compose on a suppressed turn (no new wallet origin), and the idle-cost invariant holds.

### 🏗️ Build

- *(RFC 0045 — open-core policy + dependency-direction CI gate; a fold-in with no user-facing story)* The `MIT ← BUSL ← Private` dependency-direction CI gate — `make imports-check` (a Python `import-linter` contract) + the Go `internal/archpolicy` check — plus the [reserved-seams note](docs/open-core-reserved-seams.md) and the DCO scaffold. Pure license-boundary infrastructure: it **moves no code**, stands up no private track, and adds no shipped-artifact dependency (the `import-linter` gate is dev/CI-only). The per-extraction RFCs (0046/0047) stay v0.4.0+. ([#701](https://github.com/mkhomutov/Persatrix/pull/701))

[0.3.10]: https://github.com/mkhomutov/Persatrix/compare/v0.3.9...v0.3.10

## [0.3.9] - 2026-06-22

> **Codename:** Conversations you can mine

### Highlights

- **A persona can recall the verbatim text of past conversations — scoped to the channels it was actually present for.** The new `recall_channel_messages` tool searches the exact words of prior messages (not the lossy episodic *summary*), with the access rule enforced server-side in SQL: an FTS5 index over `messages` joined against the RFC 0035 membership-interval ledger, so a persona added → removed → re-added recalls messages from *both* of its stints and from neither the pre-join period nor the removal gap. Recall is `epoch_id`-hard-filtered (run isolation) and spans sessions within the epoch. ([RFC 0036](docs/rfcs/0036-persona-message-recall.md))
- **The same "scoped to where you were present" rule now holds for the *live* prompt, not just the recall tool.** A re-added persona's conversation window and boot-time catch-up no longer surface messages from the removal gap — closing an inconsistency RFC 0034 left open (the persona could once *see* gap messages live but could not *recall* them). For a current single-stint member the filter is a no-op. ([RFC 0036 §G](docs/rfcs/0036-persona-message-recall.md#g-conversation-window-membership-filter))

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| **[Permission — opt-in, deny-by-default]** New `channels:recall` permission | The `recall_channel_messages` tool is **unavailable** until a persona is granted `channels:recall` in its `permissions:` block (`channels: { recall: true }`). Existing configs without the block load unchanged and the tool is simply denied (a failed `ToolResult`) — verbatim cross-channel recall is materially more sensitive than reading the persona's own episodic summaries (`memory:read`), so it is a **distinct** grant an operator enables deliberately. |
| **[Migration — automatic, additive]** Two channel-store migrations (v9 ledger, v10 FTS) | On first open after upgrade the channel store migrates **v8 → v9 → v10**: v9 adds the append-only `membership_intervals` ledger ([RFC 0035](docs/rfcs/0035-channel-membership-interval-ledger.md), backfilled with one open interval per current member), v10 adds the `messages_fts` FTS5 index over `messages` (backfilled via `('rebuild')`). Both are additive, idempotent, and `user_version`-stamped in-transaction; no operator action is required. On a SQLite build without FTS5, v10 records the unavailable flag and recall transparently falls back to a `LIKE` scan under the identical membership scope. |
| **[Behaviour — active by default for personas]** Conversation-window / catch-up membership filter | Persona-runtime history fetches now pass `?as_participant=<agent_id>`, so a re-added persona's live window and episodic seeding exclude pre-join and removal-gap messages. **No-op for a current single-stint member** (recent messages are unaffected); **human/CLI callers omit the param and are unchanged**. |
| **[Known limitation — accepted]** Pre-upgrade closed stints are unrecallable | The RFC 0035 §D backfill recovers **one open interval per *currently present* participant**; membership stints that *closed* (a remove, or a remove-then-rejoin) **before** migration v9 shipped are permanently absent from the ledger, so messages inside them cannot be recalled and are not surfaced by the window filter. This is an [accepted gap](docs/rfcs/0035-channel-membership-interval-ledger.md#d-backfill) ([RFC 0035 OQ #1](docs/rfcs/0035-channel-membership-interval-ledger.md#open-questions)), not a bug — recall and membership history are exact from v9 forward. |
| **[Known limitation — by design]** Recall honours the retention horizon | Recall reads the **live** `messages` table, so a cap-pruned or user-deleted message is gone from results (removed from `messages_fts` by the delete trigger). The tool description does not promise total recall; a durable, retention-immune recall store is explicitly [out of scope](docs/rfcs/0036-persona-message-recall.md#h-retention-horizon-and-deletions). |

**Behaviour-active-by-default caveat.** The `?as_participant` conversation-window / catch-up membership filter runs **live by default for personas** — a re-added persona's live prompt and episodic seeding now stop at its membership boundary out of the box. It is a **no-op for a current single-stint member** (the common case — recent messages are unaffected), and human/CLI callers omit the param and are unchanged, so the practical change is confined to removed-and-re-added personas. The **recall tool itself stays opt-in** (gated behind `channels:recall`): a persona without the grant behaves exactly as in v0.3.8.

**Version-skew / upgrade-ordering caution.** The orchestrator owns the channel-store migrations **and** the new REST surface (`POST …/recall`, `?as_participant` on the history endpoint). **Upgrade the orchestrator first** (or together with the agents): a v0.3.9 agent talking to a **pre-v0.3.9** orchestrator gets a graceful failure from the recall tool (no endpoint) and its `?as_participant` filter is silently ignored (the old orchestrator returns unfiltered history — removal-gap messages would reappear in the live window until it is upgraded). The reverse skew is safe (a v0.3.9 orchestrator simply offers the tool to agents that may not request it).

### 🚀 Features

- **`recall_channel_messages` persona tool (RFC 0036 Phase 2).** Verbatim, membership-scoped message recall. The scope participant is closure-bound to the calling persona and sent as the endpoint path segment — the LLM supplies only `query` / `channel_id` / `sender` / `limit` and can never recall another persona's scope. Default call (`query` only) searches every accessible channel; `channel_id` / `sender` narrow it. Wired onto personas post-session alongside the conversation-window history fetcher. The recall machinery — the `messages_fts` FTS5 migration, the membership-scoped `RecallMessages` query, the `POST …/recall` endpoint + server-side audit, the tool itself — landed across [#675](https://github.com/mkhomutov/Persatrix/pull/675)–[#678](https://github.com/mkhomutov/Persatrix/pull/678), [#681](https://github.com/mkhomutov/Persatrix/pull/681).
- **New `channels:recall` permission.** Distinct from `memory:read` — verbatim cross-channel recall is materially more sensitive than reading the persona's own episodic summaries, so an operator can enable episodic recall while leaving verbatim recall off. Deny-by-default: a persona without the grant gets a failed `ToolResult`, and existing configs without the block load unchanged (the tool is simply denied). ([#678](https://github.com/mkhomutov/Persatrix/pull/678))
- **`?as_participant=<id>` on `GET /api/v1/channels/{id}/messages` (RFC 0036 Phase 3).** A scoped history variant (`GetHistoryScoped`) applying the *same* `membership_intervals` `EXISTS` + `epoch_id` clause as recall, reused by the persona conversation window and boot-time catch-up so the live prompt obeys the membership scope. Omitting the param yields the unchanged unscoped history (human/CLI path). ([#679](https://github.com/mkhomutov/Persatrix/pull/679))
- **Append-only `membership_intervals` ledger in the channel store (RFC 0035 — substrate).** One row per membership stint `(channel_id, participant_id, joined_at, left_at)`, written transactionally by `AddMember` / `RemoveMember` / `GetOrCreateDM` / `CreateChannelWithMembers` and guarded by a partial unique index (≤1 open stint per pair). No user-visible behaviour of its own — it is the ledger recall scoping joins against — plus a read-only `GET …/members/{id}/history` operator inspection endpoint. ([RFC 0035](docs/rfcs/0035-channel-membership-interval-ledger.md); [#671](https://github.com/mkhomutov/Persatrix/pull/671)–[#674](https://github.com/mkhomutov/Persatrix/pull/674), [#680](https://github.com/mkhomutov/Persatrix/pull/680))

### 🐛 Bug Fixes

- *(persona recall — channel-narrowed recall by the bare channel name returned zero results; [ISSUE-0107](docs/issues/ISSUE-0107-recall-channel-id-narrowing-not-canonicalized.md))* The `POST …/recall` endpoint forwarded the optional `channel_id` narrowing parameter to the store **unmodified**, with no `group:`/`dm:` canonicalization — unlike the channel REST paths, which canonicalize their id. The store matches `messages.channel_id` against the canonical (`group:`-prefixed) id, so a caller narrowing by the bare, human-facing channel name (`mt-recall-001` rather than `group:mt-recall-001`) got **zero** results. Surfaced live in `MT-PERSONA-RECALL-001` Step 5: `ember-owl`'s own recall-tool call narrowed by the bare name, got `count=0`, and fell back to its conversation transcript. Non-exposure (narrowing can only **reduce** the result set, never widen scope past the membership filter), but it silently broke channel-narrowed recall for the natural id form. The endpoint now canonicalizes the narrowing id before the store call; pinned by `TestRecallEndpoint_BareChannelNarrowMatchesCanonical`. Found and fixed in the same PR ([#685](https://github.com/mkhomutov/Persatrix/pull/685)).

### 🔒 Security

- **Recall output is quarantined in the RFC 0009 `<external_data>` envelope** before it reaches the model (`recall_channel_messages` is registered in `EXTERNAL_TOOL_SOURCES`). Recalled verbatim text is the most-untrusted tool output in the persona path, so it gets the same structural "do not treat as instructions" boundary as `http_request` / `file_read` results — and that boundary fences the whole row, provenance fields (`sender` / `channel_id`) included.
- As defense-in-depth *inside* that envelope, each recalled message's `content` is also delimiter-escaped per row through the same `<|…|>` sanitizer the RFC 0034 conversation window uses (now centralised in `agents.prompt_safety`), so the literal `<|user_message|>` boundary token is broken before serialization. Each row is tagged with its origin `channel_id` + `sender` so the model knows it is quoting cross-context material.
- **Recall is deny-by-default and audited server-side.** The `channels:recall` permission is a distinct grant an operator enables deliberately (a persona without it cannot call the tool at all), and every recall — via both the deterministic `POST …/recall` endpoint and the persona tool — emits one `channel.recall` audit event recording the **count, not the content**, so the access is observable without logging the verbatim text. The membership scope is enforced in SQL (the `membership_intervals` `EXISTS` clause), not in the persona runtime, so the LLM cannot widen it.

### 📚 Documentation

- *(operator/persona guides + README/ROADMAP)* The [persona-agents guide](docs/guides/persona-agents.md) (the `recall_channel_messages` tool + the verbatim-vs-summary distinction — "what was literally said" vs. "what I remember concluding" — + the `channels:recall` grant) and the [memory-architecture diagram](docs/diagrams/memory-architecture.md) (the recall path: persona tool → `POST …/recall` → `messages_fts` ⋈ `membership_intervals`, beside the episodic/semantic summary tiers) were authored during implementation ([#681](https://github.com/mkhomutov/Persatrix/pull/681)) and verified against the shipped surface; the README Roadmap row + the ROADMAP Version Map / RFC Master Index (RFC 0035 + RFC 0036 → `✅ Implemented`) were refreshed; the [v0.3.9 release checklist](docs/v0.3.9-release-checklist.md) landed ([#686](https://github.com/mkhomutov/Persatrix/pull/686)).
- *(v039 planning + release-prep)* The v0.3.9 [master plan](docs/v0.3.9-plan.md) ([#669](https://github.com/mkhomutov/Persatrix/pull/669)), the [RFC 0035](docs/rfcs/0035-pr-plan.md) + [RFC 0036](docs/rfcs/0036-pr-plan.md) PR plans ([#670](https://github.com/mkhomutov/Persatrix/pull/670)), the [release-prep plan](docs/v0.3.9-release-prep-plan.md) ([#682](https://github.com/mkhomutov/Persatrix/pull/682)), and the [MT execution report](docs/manual-tests/v0.3.9-execution-report.md) ([#685](https://github.com/mkhomutov/Persatrix/pull/685)).

### 🧪 Testing

- The headline manual test [`MT-PERSONA-RECALL-001`](docs/manual-tests/MT-PERSONA-RECALL-001.md) *verbatim recall is scoped to where the persona was present* — a persona added → removed → re-added recalls **both** stints but **never** the removal-gap message (even though the gap message also contains the query term), via **both** the deterministic `POST …/recall` endpoint **and** the real persona tool; a gap-only query returns empty; the persona honestly reports no record of the gap fact; the membership-history endpoint shows two non-overlapping intervals; one `channel.recall` audit event per recall records count-not-content — was executed **live on Anthropic** against the `2bd72a8` RC tip; see the [execution report](docs/manual-tests/v0.3.9-execution-report.md).
- Structural release-blocker gates green on the RC tip: the recall store scope/ranking/safety + `LIKE`-fallback suites ([`sqlite_search_test.go`](internal/channels/sqlite_search_test.go) / [`sqlite_search_fallback_test.go`](internal/channels/sqlite_search_fallback_test.go) — incl. `TestRecallMessages_Scope_JoinLeaveRejoin`, the data-exposure gate), the recall-endpoint scope/audit + §G window-endpoint suites ([`persona_recall_handlers_test.go`](internal/server/persona_recall_handlers_test.go) / [`channel_history_scoped_test.go`](internal/server/channel_history_scoped_test.go)), the Python recall-tool closure-binding/permission/injection suite ([`test_recall_tool.py`](tests/unit/python/test_recall_tool.py)), the ledger transactional-consistency suite, and the **two channel-store migration suites** (v8→v9 ledger; v9→v10 FTS). The carried-forward alias cost-attribution + session/epoch structural-isolation gates stay green, and the cost-regression gate confirms recall adds **no new LLM-call origin** — the tool is store-side FTS5/`LIKE` SQL and never bills the wallet.

[0.3.9]: https://github.com/mkhomutov/Persatrix/compare/v0.3.8...v0.3.9

## [0.3.8] - 2026-06-16

> **Codename:** Conversations that converge

### Highlights

- **Give a group of personas a problem and the brainstorm converges, terminates, and hands back a readable result.** v0.3.8 lands the convergence cluster across four jointly-owned workstreams. **No pile-on (RFC 0030 relevance Tier B):** on an un-addressed open-floor message a `participant`/`chair` runs one cheap, leased `fast`-model salience bid and stays silent unless it has something genuinely new — an open question draws a *small, relevant* set, a redundant follow-up draws silence, and an idle/`observer` persona costs **zero**. **Bounded cost & finite turns (governance Layers 1/2/4 + the interaction-id producer + the chair-stall-escalation and end-vote-close-propagation amendments):** the orchestrator stamps one open interaction-id per channel on every publish, the Layer 1 cost ceiling fails closed in the wallet, the Layer 2 reply budget rejects pre-persistence, and a Layer 4 end-of-interaction vote (K=2 distinct votes in W=3 turns) **closes the discussion before the depth cap** — a stall escalates to the chair instead of dying silently, and a vote-closed discussion is announced to the room rather than buried as "went idle." **A readable result (RFC 0020 interaction-summary surface):** a closed interaction surfaces its one-per-interaction summary in the web console and via `persatrix agent interactions`, with an honest `"[interaction summary unavailable]"` sentinel on summariser failure — never a blank or a fabrication. **Operator-editable channel config (RFC 0050):** the convergence knobs are editable from the CLI (`persatrix channel config`) and the web-console settings panel without a restart, with the channel store as the single source of truth and a revision-gated YAML loader so config-as-code and live edits coexist (`config_edit_enabled` ships **on**). The user-facing promise — *open a multi-persona group channel, pose an open question, and watch a small relevant set converge on an answer, vote to close, and leave behind a summary you can read.*

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| **[Config — additive, opt-in, back-compat]** Deterministic conversation governance (RFC 0030 Layers 1/2/4) | New **opt-in, default-off** per-channel knobs let a multi-persona brainstorm converge, stay bounded, and terminate without a moderator. **Layer 1 — cost ceiling**: `interaction_budget_tokens` (+ fleet `default_interaction_budget_tokens`) caps total LLM tokens leased per interaction; an over-budget lease is denied (`INTERACTION_BUDGET_EXHAUSTED`) **fail-closed** in the wallet. **Layer 2 — reply budget**: `max_replies_per_participant_per_interaction` (+ fleet `default_max_replies_per_participant`) bounds how many times one participant publishes in an interaction; the `(K+1)`th publish is rejected **pre-persistence** (HTTP 429); `governance.exempt_principals: [human]` exempts human participants. **Layer 4 — end-of-interaction vote**: an `END_INTERACTION_VOTE` action plus per-channel `end_vote_threshold` (K, default 2) / `end_vote_window` (W, default 3) — K distinct participants voting within W consecutive turns closes the interaction. **Every enforcement knob defaults to uncapped/off** — but note the row below: with the `interaction_id` producer landed in this same release, the layers run **live by default** (id stamping, end-vote close at the K=2/W=3 defaults, idle rotation at 600s), so an unedited `config/channels.yaml` no longer behaves exactly as in v0.3.7; only the *enforcement ceilings* (cost, reply budget) stay off until opted in. The layers compose per [RFC 0030 §B](docs/rfcs/0030-multi-agent-conversation-governance.md#b-layered-architecture): a publish proceeds only if every active layer admits it; a lower-layer drop short-circuits the higher layers and increments `governance_drop{layer}`. New telemetry under `channel.conversation.*`: `governance_drop{layer}`, `interaction_closed{trigger}`, `end_vote_emitted`, `reply_budget_remaining`, plus a `conversation.governance.layer` trace-span attribute. The Layer 5 moderator remains **v0.4.0**. See the [channels guide](docs/guides/channels.md#conversation-governance-rfc-0030-layers-124--v038). |
| **[Behaviour — active by default]** Conversations end because the participants said so (RFC 0030 interaction-id producer) | The governance layers above are now **live on real traffic**: the orchestrator resolves one open interaction per channel and **stamps its id on every publish** ([producer plan](docs/rfcs/0030-interaction-id-producer-pr-plan.md) IP1/IP2 — inbound id claims are overridden, so only orchestrator-minted ids ever key governance state, retiring the spoofable-token map-growth hazard), rotates to a fresh interaction after `interaction_idle_timeout_seconds` of quiet (new knob, default **600**, matching the agent-side memory tracker; explicit `0` disables; threads never rotate), and personas **carry the end-of-discussion vote vocabulary** in their system prompt (the [`end-interaction-vote`](prompts/runtime/safety/end-interaction-vote.md) snippet — the one structured JSON action a persona emits). The arc this buys: a discussion closes on **two distinct votes** (`interaction_closed{trigger=end_votes}`) *before* the depth cap, the closed conversation's summary is surfaced, a racer into the closed interaction is suppressed and self-healed, and the channel's **next message opens a fresh interaction** — a quorum ends one conversation, never the room. Leased LLM calls (the quality turn *and* the Tier B bid) now bill the resolved interaction, activating Layer 1 attribution end-to-end; a vote into a DM is dropped agent-side (`status=dm_channel`). `interaction_closed{trigger}` gains `idle` (emitted lazily on the rotating publish). Deterministic acceptance: [`interaction_convergence_test.go`](internal/channels/interaction_convergence_test.go); live-LLM acceptance: [`MT-CHANNEL-GOV-003`](docs/manual-tests/MT-CHANNEL-GOV-003.md). |
| **[Behaviour — opt-in per channel]** A stalled discussion escalates to the chair instead of dying silently (RFC 0030 chair-stall-escalation, a minimal Layer 5 slice) | The last silent-death mode is closed. A floor round ending with **zero replies** on the open interaction — every participant honestly bid "nothing new" with the question *unresolved* — previously stood until idle rotation buried it, outcome unrecorded; the Layer 4 quorum could never form because votes only ride publishes and nobody published. With the new per-channel **`escalation_chair_id`** set (the demo `planning` channel pairs it with `nova-sparrow`), the orchestrator detects the stall deterministically at the round's tail and dispatches **one forced turn per interaction** to that member: the [`chair-escalation`](prompts/runtime/safety/chair-escalation.md) framing forbids silence for the turn and steers it to cast the end-of-discussion vote with the **synthesis in the vote's content** (one concurring vote then closes with the synthesis on the record) or to @-mention the member best placed. **Closing still flows through the Layer 4 quorum alone** — the chair proposes, the quorum disposes; no new close path, no new trust grant; the inert `chair_moderation.py` seam and TB5 stay pinned. The forced turn bypasses the Tier B bid (re-running it would re-produce the silence being escalated), is billed to the interaction (Layer 1), and is fail-open everywhere (a missing/dead chair degrades to the previous stall; the marker is additive wire surface, `ChannelMessageEvent.chair_escalation`). Load-time guards: the chair must be a non-`observer` member and the channel must not disable floor control. Telemetry: `chair_escalation{channel_type, outcome ∈ dispatched/no_chair/already_escalated/self_stimulus/dispatch_error}` — every *detected* stall counts, so stalls surface even on channels with no chair configured. The full §I moderator (v0.4.0) absorbs this slice. Deterministic acceptance: [`interaction_convergence_test.go`](internal/channels/interaction_convergence_test.go); live: [`MT-CHANNEL-GOV-004`](docs/manual-tests/MT-CHANNEL-GOV-004.md). |
| **[Behaviour — active by default]** A vote-closed discussion is announced to the room, not buried as "went idle" (RFC 0030 end-vote close propagation) | MT-CHANNEL-GOV-004's live run exposed the gap: the Layer 4 quorum close suppresses the closing vote's fanout (correctly — the room must stop), but that starved every member's agent-local tracker of the close itself, so with no follow-up traffic each member buried the *converged* discussion as **went idle** up to a full idle window later — and the chair never learned its synthesis closed the room. The orchestrator now re-dispatches the closing vote to every dispatch-served non-sender member as a marked, ingestion-grade **close notification** (`ChannelMessageEvent.interaction_close_notification` — additive, the `chair_escalation` trust class): the receiver's gate refuses it pre-LLM (no turn, no Tier B bid, no spend), the closing message still lands in the window as the record's final turn (ingest-on-suppress), and the member's interaction record closes **immediately** as *ended* with the summary generated now. Fanout suppression, the quorum mechanics, and the lazy-idle successor-stamp path are unchanged; `respond: never` members (the human seam) read the persisted vote on demand as before. Fire-and-forget with the fanout-drain guarantees (`WaitForPendingFanout`); fail-open — a dropped notification degrades to exactly the pre-amendment idle-out, observable per recipient on `channel.conversation.close_notification{channel_type, outcome ∈ dispatched/dispatch_error}`. Mixed-version safe both directions (an old agent sees one extra bid per close; an old orchestrator changes nothing). See the [amendment](docs/rfcs/0030-amendment-end-vote-close-propagation.md) and the [channels guide boundary notes](docs/guides/channels.md#conversation-governance-rfc-0030-layers-124--v038). |
| **[Behaviour — opt-in via the disposition vocabulary]** No pile-on — the Tier B salience bid + the `chair` (RFC 0030 relevance amendment) | On an **un-addressed open-floor** message, a `participant`/`chair` member now runs one cheap, leased `fast`-model **salience bid** ([`agents/salience_bid.py`](agents/salience_bid.py)) — "do I have something genuinely new to add that hasn't already been said?", reading the in-round transcript — and stays **silent** unless its score clears the member's `threshold`. This is the no-pile-on win: an un-addressed question draws a small relevant set, and a redundant follow-up draws silence. The per-disposition **`threshold`** field (reserved/no-op in v0.3.7) is **now live** — an **unset** threshold biases to silence (only a decisive score speaks). A new **`chair`** disposition is a `participant` with a low default threshold (a facilitator that clears the bid readily); a `chair` **cannot close, wrap up, or terminate** a conversation in v0.3.8 (that moderator half is Layer 5, **v0.4.0** — present but inert in [`agents/chair_moderation.py`](agents/chair_moderation.py): a typed seam no runtime path calls). **Natural-language addressing** ("let's hear from X") *biases* the bid toward the named persona without hard-dropping anyone (a signal, not a filter — only structured `@`-mentions deterministically drop). A channel-level `salience_max_channel_members` (default `20`) skips the bid above that size (falls back to `addressed`-only). The bid is keyed on the **declared vocabulary**: a member written with `participant`/`chair` is bid-governed, while the **literal `always` keyword keeps replying unconditionally** (back-compat) — so a config that never adopted the disposition vocabulary behaves exactly as in v0.3.7. A bid that fails (parse failure, denied/exhausted lease, unresolvable `fast` alias) **fails closed** to silence; idle personas still cost zero. Acceptance: [`MT-CHANNEL-RELEVANCE-002`](docs/manual-tests/MT-CHANNEL-RELEVANCE-002.md). See the [channels guide §2](docs/guides/channels.md#per-membership-respond-dispositions). |
| **[Behaviour — defect fix]** A mention of someone who can't reply no longer silences the room (RFC 0030 floor-capable directedness) | The Tier A **directed-elsewhere** filter now requires a **floor-capable** addressee. Pre-amendment, *any* non-empty mentions list counted as directed — including a mention of the human operator (a `respond: never` member by the documented join convention) — so one polite "@alex, here's our recommendation…" suppressed every `participant` as `directed_elsewhere` and the room fell silent until the next human message. The orchestrator now resolves the floor-capable subset of a message's mentions (current members, **other than the sender**, whose normalized policy is not `never`) once per publish and carries it on the wire (`ChannelMessageEvent.floor_mentions` + the `floor_mentions_resolved` producer flag); both the receiver gate and the Go candidate set suppress only when that subset is non-empty. A message addressing only the human, an `observer`, a non-member, or the sender itself is **open floor** — the Tier B salience bid (above), not the directedness filter, decides who has something to add. The `mentioned`/`@everyone` admit paths are unchanged. **Version-skew safe in one direction — upgrade agents first (or together)**: an old orchestrator leaves the flag unset and agents fall back to the raw-mentions basis (today's behaviour — over-suppression, never under). The *reverse* skew — a v0.3.8 orchestrator fanning out to pre-v0.3.8 agents — queues open-floor candidates the old raw-basis gate still suppresses into the serialized floor round, degrading the trigger scenario from "room goes silent" to a multi-minute publish stall (≈45s per silent candidate; [amendment §C item 1](docs/rfcs/0030-amendment-floor-capable-directedness.md#c-mechanism--resolve-at-the-orchestrator-carry-on-the-wire)). Acceptance: [`MT-CHANNEL-RELEVANCE-001` §Step 6](docs/manual-tests/MT-CHANNEL-RELEVANCE-001.md). See the [floor-capable-directedness amendment](docs/rfcs/0030-amendment-floor-capable-directedness.md). |
| **[Behaviour — additive]** A closed interaction surfaces a readable summary (RFC 0020 surface) | When an interaction **closes** — by a Layer 4 end-vote (a `structural` close), by the Layer 1 cost ceiling, or by going **idle** — the persona's already-persisted [RFC 0020 §C/§D](docs/rfcs/0020-interaction-lifecycle.md#c-interaction-lifecycle-states) one-per-interaction summary is now **surfaced** so a converged brainstorm hands back a readable result instead of merely stopping. The summariser is **unchanged** — this is a read surface, not a new synthesis step. New read API `GET /api/v1/agents/{id}/interactions/closed` (per-agent — each participant persists its own row); the **web console** conversation view renders an "interaction closed" affordance below the live turns carrying the summary + close trigger (*went idle* / *ended* / *cost limit reached*), and the **CLI** prints it via `persatrix agent interactions <agent> [--scope] [--interaction-id] [--limit] [--json]`. The cost-ceiling close was wired to route through the summarising close path (a new `cost` `CloseReason` + `agent.interactions.closed.by_cost` counter) so a cost-bounded conversation still yields a result. A failed summariser surfaces the `"[interaction summary unavailable]"` sentinel honestly — never a blank, never a fabricated synthesis. Additive: an open interaction's live feed is unchanged. The channel-side close also **propagates to the per-agent record** ([`agents/persona_runtime/interaction_boundary.py`](agents/persona_runtime/interaction_boundary.py), the MT-CHANNEL-GOV-003 Step 3 follow-up): a persona that emits `END_INTERACTION_VOTE` closes its local interaction structurally **once the vote publish succeeds** (the close is parked at decide time and confirmed by the publish outcome, so a vote that never reached the orchestrator leaves no early "ended" record), and every other member closes when it receives the channel's next publish carrying the **rotated `interaction_id`** — so a vote-closed discussion appears here as *ended* instead of merging into the next topic and eventually surfacing as *went idle*. Threaded replies are exempt on both seams (they ride the *parent floor's* id, so a floor close never splits a live thread — the agent-side mirror of the resolver's "the thread IS the interaction" rule); see the channels guide's boundary notes for the rotation's label-fidelity caveat. The propagation also covers **startup catch-up replay**: replayed history carries the wire interaction ids, so a restarted agent's replayed span splits into the same closed records a live agent would have written (each replayed close runs the one-time summariser at boot) instead of merging into one blob that disarms the first live rotation. Acceptance: [`MT-INTERACTION-SUMMARY-001`](docs/manual-tests/MT-INTERACTION-SUMMARY-001.md). See the [channels guide](docs/guides/channels.md#the-interaction-summary-surface-rfc-0020--v038). |
| **[Migration — forward-only, upgrades on open]** Channel store `channels.db` v6→v8 (RFC 0030 Tier B `threshold` + RFC 0050 config columns) | The channel store (`channels.db`, [`internal/channels/sqlite_schema.go`](internal/channels/sqlite_schema.go)) advances from **v6** (the v0.3.5 epoch axis) to **v8**: **v7** adds the per-member salience-bid `threshold` column (RFC 0030 Tier B) — a pre-v7 row reads back as an un-gated legacy `always` member; **v8** adds three additive `channels` columns (RFC 0050) — `config_overrides_json` (a sparse per-channel governance override blob; `NULL` = inherit every knob), `config_revision` (a store-owned monotonic revision, backfilled to `0`), and a reserved/dormant `config_change_lineage`. **Upgrades automatically on first open**, no operator action, no data loss; an already-v8 store is a no-op open. An existing `config/channels.yaml` seeds the router unchanged. With this release `config_edit_enabled` defaults **on**, so the CLI `channel config` verbs and the web-console settings panel are live (RFC 0050 Phase 2). **There is no rollback path — forward-only.** See the [channels guide](docs/guides/channels.md) and [RFC 0050](docs/rfcs/0050-extensible-channel-configuration.md). |
| **[Migration — forward-only, upgrades on open]** Persona-memory v14→v15 (queryable governance interaction id) | The persona-memory schema (`agents/memory/migrations.py`) advances from **v14** to **v15**: migration 15 ([`_migration_governance_id.py`](agents/memory/_migration_governance_id.py)) promotes the RFC 0030 governance interaction id to a queryable `episodes.governance_interaction_id` column (backfilled from the ISSUE-0102 context blob via `json_extract`), so `--interaction-id` matches **either** namespace (agent-side episode id *or* the orchestrator's governance id pasted from the logs). **Upgrades automatically on first open**, no operator action, no data loss; an already-v15 DB is a no-op open. **There is no rollback path — forward-only**, per the established migration contract. See [RFC 0020 §C/§D](docs/rfcs/0020-interaction-lifecycle.md#c-interaction-lifecycle-states). |

**Behaviour-active-by-default caveat.** With the **interaction-id producer** landed in this same release, the governance layers run **live by default** — ids are stamped on every publish, an interaction closes on the end-vote defaults (K=2 / W=3), and idle rotation fires at 600s. An unedited `config/channels.yaml` therefore **no longer behaves *exactly* as in v0.3.7**. The **enforcement ceilings** (the Layer 1 cost ceiling, the Layer 2 reply budget) stay **off until opted in** — only the id stamping / end-vote close / idle rotation are active out of the box.

**Version-skew caution (RFC 0030 floor-capable directedness).** Upgrade agents first (or together) — a v0.3.8 orchestrator fanning out to pre-v0.3.8 agents degrades the directedness defect-fix scenario to a multi-minute publish stall ([floor-capable-directedness amendment §C item 1](docs/rfcs/0030-amendment-floor-capable-directedness.md#c-mechanism--resolve-at-the-orchestrator-carry-on-the-wire)). The reverse skew is safe (over-suppression, never under).

### 🚀 Features

- *(RFC 0030 governance Layers 1/2/4)* Deterministic conversation governance — the Layer 1 per-interaction cost ceiling (`interaction_budget_tokens`, fail-closed in the wallet), the Layer 2 per-participant reply budget (`max_replies_per_participant_per_interaction`, HTTP 429 pre-persistence), and the Layer 4 end-of-interaction vote (`END_INTERACTION_VOTE`; K distinct votes in W turns close) — each opt-in/uncapped, composed per [RFC 0030 §B](docs/rfcs/0030-multi-agent-conversation-governance.md#b-layered-architecture). See the [governance-layers PR plan](docs/rfcs/0030-governance-layers-pr-plan.md) ([#576](https://github.com/mkhomutov/Persatrix/pull/576)–[#581](https://github.com/mkhomutov/Persatrix/pull/581)).
- *(RFC 0030 interaction-id producer)* The orchestrator resolves one open interaction per channel and **stamps its id on every publish** (inbound id claims overridden), rotates to a fresh interaction after `interaction_idle_timeout_seconds` of quiet (default 600; threads never rotate), and personas carry the end-of-discussion vote vocabulary in their system prompt — so the governance layers run live on real traffic and leased LLM calls bill the resolved interaction. See the [producer plan](docs/rfcs/0030-interaction-id-producer-pr-plan.md) ([#604](https://github.com/mkhomutov/Persatrix/pull/604)–[#607](https://github.com/mkhomutov/Persatrix/pull/607)).
- *(RFC 0030 chair-stall escalation — minimal Layer 5 slice)* A zero-reply round on an open interaction with a per-channel `escalation_chair_id` set forces **one** chair turn (synthesize-in-vote or hand off); closing still flows through the Layer 4 quorum alone, and the move is fail-open (a missing/dead chair degrades to the previous stall). Telemetry: `chair_escalation{outcome}`. See the [chair-stall-escalation amendment](docs/rfcs/0030-amendment-chair-stall-escalation.md) ([#608](https://github.com/mkhomutov/Persatrix/pull/608)–[#611](https://github.com/mkhomutov/Persatrix/pull/611)).
- *(RFC 0030 end-vote close propagation)* A vote-closed discussion is re-dispatched to every member as a marked, ingestion-grade **close notification** (`ChannelMessageEvent.interaction_close_notification`) so each member's interaction record closes **immediately** as *ended* with a summary, instead of burying the converged discussion as "went idle" an idle window later. Fail-open and mixed-version safe. See the [end-vote-close-propagation amendment](docs/rfcs/0030-amendment-end-vote-close-propagation.md) ([#613](https://github.com/mkhomutov/Persatrix/pull/613)–[#615](https://github.com/mkhomutov/Persatrix/pull/615)).
- *(RFC 0030 relevance Tier B — salience bid + `chair` + NL-addressing)* On an un-addressed open-floor message a `participant`/`chair` runs one cheap, leased `fast`-model salience bid ([`agents/salience_bid.py`](agents/salience_bid.py)) and stays silent unless its score clears its `threshold` (the v0.3.7 reserved/no-op field, now live); the `chair` disposition is a low-threshold facilitator (its Layer-5 moderator half inert in [`agents/chair_moderation.py`](agents/chair_moderation.py)); natural-language addressing biases the bid without hard-dropping; a `salience_max_channel_members` cap (default 20) skips the bid above that size. See the [Tier B PR plan](docs/rfcs/0030-amendment-relevance-gated-response-tierb-pr-plan.md) ([#571](https://github.com/mkhomutov/Persatrix/pull/571)–[#575](https://github.com/mkhomutov/Persatrix/pull/575), [#582](https://github.com/mkhomutov/Persatrix/pull/582); unanswered-opener calibration [#624](https://github.com/mkhomutov/Persatrix/pull/624)).
- *(RFC 0020 interaction-summary surface)* A closed interaction (votes / cost / idle) surfaces its already-persisted one-per-interaction summary via the new `GET /api/v1/agents/{id}/interactions/closed`, the web-console conversation view, and `persatrix agent interactions <agent>`; the summariser is unchanged (a read surface, not a new synthesis step). See the [summary-surface PR plan](docs/rfcs/0020-interaction-summary-surface-pr-plan.md) ([#583](https://github.com/mkhomutov/Persatrix/pull/583)–[#586](https://github.com/mkhomutov/Persatrix/pull/586), [#590](https://github.com/mkhomutov/Persatrix/pull/590)/[#592](https://github.com/mkhomutov/Persatrix/pull/592)).
- *(RFC 0050 operator-editable channel config)* Per-channel governance config is editable from the CLI (`persatrix channel config get`/`set`/`unset`/`export`/`import`/`diff`) and the web-console settings panel without a restart, with the channel store as the single source of truth and a revision-gated YAML loader so config-as-code and live edits coexist (`config_edit_enabled` ships on). The `interaction_budget_tokens` knob is router-held with server-side fail-closed enforcement. See the [Phase 1](docs/rfcs/0050-phase1-pr-plan.md) / [Phase 2](docs/rfcs/0050-phase2-pr-plan.md) PR plans + the [interaction-budget-enforcement amendment](docs/rfcs/0050-amendment-interaction-budget-enforcement.md) ([#642](https://github.com/mkhomutov/Persatrix/pull/642)–[#647](https://github.com/mkhomutov/Persatrix/pull/647), [#652](https://github.com/mkhomutov/Persatrix/pull/652)–[#654](https://github.com/mkhomutov/Persatrix/pull/654), [#656](https://github.com/mkhomutov/Persatrix/pull/656)–[#660](https://github.com/mkhomutov/Persatrix/pull/660)).

### 🐛 Bug Fixes

- *(channel config — the first live edit of a YAML-seeded channel no longer detaches its other knobs; [ISSUE-0103](docs/issues/ISSUE-0103-first-config-edit-detaches-yaml-seeded-knobs.md))* RFC 0050's store-canonical apply path treats a patch as the **complete** desired override set, and the REST layer merged a sparse `PATCH …/config` onto the channel's *stored* overrides. On a YAML-seeded channel (revision 0, store blob empty — config-as-code is never persisted there) that merge carried only the edited knob, so the re-stamp silently reset **every other non-default knob** to the fleet default — most visibly detaching the channel's `escalation_chair_id`, with no validation warning, persisting across restart. The `PATCH` handler now seeds its merge base from the channel's **resolved governance** on a first edit (revision 0), so a sparse edit layers over the full baseline instead of replacing it; the chair and other knobs survive. Two deliberate consequences: the first edit makes the channel store-canonical, so its previously-inherited knobs show source `channel` and stop tracking fleet defaults (the same freeze the YAML *adopt* path makes); and the now-visible chair means a lone `floor_control:false` on a chaired channel is **rejected** rather than silently creating an inert chair (the issue's "validator consults effective state" guard, gained for free). `ApplyChannelConfig`'s wholesale-replace contract is unchanged — the fix is one layer up, at the REST merge. This was the blocking prerequisite to flipping `config_edit_enabled` on (RFC 0050 Phase 2). Regression: `TestChannelConfig_FirstEditPreservesYAMLSeededChair`, `TestChannelConfig_FirstEditFreezesDefaultsAsChannel`, `TestChannelConfig_FirstEditFloorOffWithYAMLChairRejected`. See the [web console guide](docs/guides/web-console.md).
- *(observability — closed-interaction summary surfaces & is look-up-able by the governance interaction id; [ISSUE-0102](docs/issues/ISSUE-0102-closed-summary-episode-id-diverges-from-governance-interaction-id.md))* The `interaction_id` on a closed-interaction row (`agent interactions`, `GET /api/v1/agents/{id}/interactions/closed`) is the persona's **agent-side** RFC 0020 memory-episode id — a different namespace from the orchestrator's RFC 0030 **governance** interaction id stamped on channel messages and the end-vote close logs. The two segment on independent idle clocks, so one governance interaction can map to several episode ids, and the shared field name gave no signal: taking the end-vote-closed id from the logs and looking it up returned nothing (cost real mid-MT confusion on MT-CHANNEL-GOV-004). The persona now **persists** the governance interaction id the episode was opened under (previously in-memory only) and the surface **exposes it** as a distinct `governance_interaction_id` field — the CLI renders it on a dimmed `governance:` line, present only when the interaction carried one (empty for DM / thread / non-channel scopes). The agent-side `interaction_id` is documented as the persona-memory episode id across proto / DTO / CLI, without a breaking rename. The `--interaction-id` filter now matches **either** namespace (schema migration v15 promotes the governance id to a queryable `episodes.governance_interaction_id` column, backfilled from the PR-1 context blob): pass an agent-side episode id for that one interaction, or paste the end-vote-closed **governance** id straight from the logs to get every episode of that arc (newest-first) — so the natural diagnostic move just works whichever namespace the id came from. (Landed in two PRs: PR 1 persist + display + honesty; PR 2 the queryable column + filter.) See the [channels guide](docs/guides/channels.md#the-interaction-summary-surface-rfc-0020--v038).
- *(agent runtime — optimization config resolution in containers)* In the Docker images the agents tree is pip-installed as the `persatrix_agents` package, so `agents/optimization.py`'s package-relative default path resolved to `site-packages/config/optimization.yaml` — which does not exist — and **every accessor silently returned its default in containers**: most visibly, `summarization_model()` returned `""`, so the RFC 0020 close-path summary degraded to the `"[interaction summary unavailable]"` sentinel with a per-close `Summarisation model '' is not resolvable` WARN under **every** provider overlay (first observed on the Anthropic overlay during MT-CHANNEL-GOV-003 — the overlay itself was never at fault). Alias resolution masked the bug by accident: `model_aliases.py` imported the module by its repo-absolute name, creating a *second* instance whose path resolved under `/app`. Fixed at the deployment layer: `Dockerfile.agent` now pins `PERSATRIX_OPTIMIZATION_CONFIG=/app/config/optimization.yaml` (the in-image `COPY config/` / compose bind-mount location, covering every overlay), and `model_aliases.py` uses the relative import so one module instance serves both surfaces. Library resolution stays deterministic — env override, else the package-relative default, else built-in defaults; a CWD-relative fallback was considered and rejected so model selection can never silently follow whatever `config/optimization.yaml` exists in an unrelated working directory.
- *(channel config — stale operator-facing "not yet enforced" annotation)* `persatrix channel config get` annotated an overridden `interaction_budget_tokens` row with `⚠ not yet enforced` and several doc-comments called the knob "not router-held / persisted-but-not-yet-live" — all authored mid-Phase-1 and **false at release**: the [RFC 0050 interaction-budget-enforcement amendment](docs/rfcs/0050-amendment-interaction-budget-enforcement.md) shipped router-held, server-side fail-closed enforcement ([`internal/wallet/interaction_budget.go`](internal/wallet/interaction_budget.go)), proven by `MT-CHANNEL-CONFIG-003`. Surfaced as F-1 in the [v0.3.8 MT execution report](docs/manual-tests/v0.3.8-execution-report.md); the CLI special-case note and the now-wrong Go/CLI comments were removed (cosmetic/advisory only — enforcement was already live) ([#663](https://github.com/mkhomutov/Persatrix/pull/663)).

### 📚 Documentation

- *(operator guides + README/ROADMAP)* The [channels guide](docs/guides/channels.md) (conversation-governance Layers 1/2/4 + the end-of-interaction vote + the interaction-summary surface + the Tier B `threshold`/`chair` semantics) and the [web-console guide](docs/guides/web-console.md) (the channel settings panel + the interaction-closed affordance) were verified against the shipped surface; the README Roadmap row + the ROADMAP Version Map / RFC Master Index (RFC 0050 added) were refreshed; the [v0.3.8 release checklist](docs/v0.3.8-release-checklist.md) landed ([#664](https://github.com/mkhomutov/Persatrix/pull/664)).
- *(RFCs)* The [RFC 0050 closeout](docs/rfcs/0050-extensible-channel-configuration.md) — all four channel-config manual tests run live, RFC closed as `✅ Implemented` (Phase 1+2; Phase 3 schema-driven config → future RFC) ([#661](https://github.com/mkhomutov/Persatrix/pull/661)).
- *(v038 planning + release-prep)* The v0.3.8 [master plan](docs/v0.3.8-plan.md), the [release-prep plan](docs/v0.3.8-release-prep-plan.md) ([#662](https://github.com/mkhomutov/Persatrix/pull/662)), and the [MT execution report](docs/manual-tests/v0.3.8-execution-report.md) ([#663](https://github.com/mkhomutov/Persatrix/pull/663)).

### 🧪 Testing

- The five headline manual tests — [`MT-CHANNEL-RELEVANCE-002`](docs/manual-tests/MT-CHANNEL-RELEVANCE-002.md) *Tier B no-pile-on*, [`MT-CHANNEL-GOV-003`](docs/manual-tests/MT-CHANNEL-GOV-003.md) *end-vote convergence + summary*, [`MT-CHANNEL-GOV-004`](docs/manual-tests/MT-CHANNEL-GOV-004.md) *chair stall escalation + announced close*, [`MT-INTERACTION-SUMMARY-001`](docs/manual-tests/MT-INTERACTION-SUMMARY-001.md) *summary surface + honest sentinel*, and [`MT-CHANNEL-CONFIG-001`](docs/manual-tests/MT-CHANNEL-CONFIG-001.md)…[`004`](docs/manual-tests/MT-CHANNEL-CONFIG-004.md) *RFC 0050 channel config* — plus a **combined convergence walkthrough** (no-pile-on **and** bounded cost **and** ends-on-votes **and** a readable summary, observed together on one multi-persona channel) were executed **live on Anthropic** against the `8897727` RC tip; see the [execution report](docs/manual-tests/v0.3.8-execution-report.md).
- Structural release-blocker gates green on the RC tip: the salience-bid + response-gate suites ([`agents/salience_bid.py`](agents/salience_bid.py) / [`agents/response_gate.py`](agents/response_gate.py)), the Go governance + interaction-id + convergence suites (incl. [`interaction_convergence_test.go`](internal/channels/interaction_convergence_test.go)), the channel-config store/REST suites (`TestChannelConfig_*`), and the **two migration suites** (channel store v6→v8; persona-memory v14→v15 forward-migrate + backfill + crash-replay). The carried-forward alias cost-attribution + session/epoch structural-isolation gates stay green, and the **cost-regression gate is extended** to assert Tier B never bills idle/`observer` personas and every bid is leased + attributable (the one new LLM-call origin this release).

[0.3.8]: https://github.com/mkhomutov/Persatrix/compare/v0.3.7...v0.3.8

## [0.3.7] - 2026-06-06

> **Codename:** Conversations worth watching

### Highlights

- **A group channel reads like colleagues, not bots.** v0.3.7 lands the realism rung across three workstreams. **Group-channel working memory** (RFC 0034 Phase 2): each persona sees the in-progress transcript with per-peer `[<peer_id>]: ` attribution and builds on a *specific* peer's contribution rather than treating its turn as the first. **The addressing-aware response gate** (RFC 0030 relevance Tier A): a directed `@`-question is answered by **exactly the addressee** — the other `participant`s are suppressed (`reason="directed_elsewhere"`) at **zero cost**, while an un-addressed open-floor message still admits everyone and an explicit `@everyone` broadcast disables the filter. **The peer-voice prompt**: personas address each other by name and build on the round rather than performing assistant-helpfulness. The user-facing promise — *open a multi-persona group channel, ask one persona a question, and exactly that persona answers; then ask the room and watch an ordered, mutually-aware round that reads like colleagues.* Folded in: the F-1…F-8 conversation test-findings cluster, capped by the F-7 Option D person-identity-on-the-cross-room-tier migration.

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| **[Config — additive, back-compat]** `respond_policy` → disposition vocabulary | A channel member's `respond` field is now a **disposition**: `participant` (joins the open floor), `addressed` (replies only when `@`-mentioned), `observer` (never replies). The legacy values still load and map automatically — `always → participant`, `when_mentioned → addressed`, `never → observer` — so **existing `config/channels.yaml` files keep working unchanged**; an unknown value is still a loud config error. New configs should prefer the disposition vocabulary. A reserved per-disposition `threshold` field is accepted by the schema but **no-op until v0.3.8** (Tier B salience). See the [channels guide](docs/guides/channels.md#per-membership-respond-dispositions). |
| **[Behaviour — defect fix]** A `@`-mention to one persona is no longer answered by everyone | The response gate gained **addressing-aware directedness** (RFC 0030 relevance Tier A): a message that `@`-mentions one persona is answered by **exactly that persona** — other `participant` members are suppressed (`reason="directed_elsewhere"`) instead of all piling on. An un-addressed open-floor message still admits **all** `participant`s (the "stay out because someone already covered it" *salience* suppression is v0.3.8 Tier B); an explicit `@everyone` broadcast disables the directed filter. Tier A is free — no LLM call, no recall — so a suppressed persona costs zero tokens. Acceptance: [`MT-CHANNEL-RELEVANCE-001`](docs/manual-tests/MT-CHANNEL-RELEVANCE-001.md). |
| **[Migration — forward-only, upgrades on open]** Persona-memory v12→v14 (person identity on the cross-room tier) | The persona-memory schema (`agents/memory/migrations.py`) advances from **v12** to **v14**: migration 13 adds a nullable `identity TEXT` (JSON) column to the `relationships` table so person identity (name / role / stable preferences) lives on the genuinely cross-room relationship tier (F-7 Option D); migration 14 backfills pre-cutover `contact:<id>` notes by parsing their identity onto that column, so identity learned before the cutover is not stranded. **Upgrades automatically on first open**, no operator action, no data loss; an already-v14 DB is a no-op open. **There is no rollback path — forward-only**, per the established migration contract. The channel store (`channels.db`) stays at schema **v6** (no channel migration this release). See the [RFC 0031 person-identity-cross-room-tier amendment](docs/rfcs/0031-amendment-person-identity-cross-room-tier.md). |

### 🚀 Features

- *(RFC 0034 Phase 2 — group working memory)* The persona conversation window now reads **group channels**, not just DMs: every replayed peer turn is prefixed inline `[<peer_id>]: ` so a persona sees *who said what* this round and can attribute and build on a *specific* peer's contribution (its own turns stay unprefixed `assistant` turns; the §D delimiter escape composes by construction). The in-process fetch cache is re-keyed on `(channel_id, limit)` so a small-`max_turns` persona can no longer serve an undersized window to a large-`max_turns` peer on the same multi-persona channel. Group-channel acceptance walkthrough: [`MT-PERSONA-CONVERSATION-002`](docs/manual-tests/MT-PERSONA-CONVERSATION-002.md). See [RFC 0034 §G](docs/rfcs/0034-persona-conversational-working-memory.md#g-group-channel-handling) ([#533](https://github.com/mkhomutov/Persatrix/pull/533), [#534](https://github.com/mkhomutov/Persatrix/pull/534)).
- *(RFC 0030 relevance gate — Tier A + disposition reframe)* A message that `@`-mentions one persona no longer draws a reply from **every** `participant` member of a group channel. The response gate ([`agents/response_gate.py`](agents/response_gate.py)) gained a free, deterministic **directed-elsewhere** filter (no LLM, no recall): when a message names specific recipients and is not an explicit `@everyone` broadcast, an un-named `participant` is suppressed (`reason="directed_elsewhere"`) — fixing the v0.3.6 probe defect where a directed question was answered by everyone, including one persona that protested *"I'm not Ember Owl, but…"* and answered anyway. The candidate-responder set in [`internal/channels/fanout.go`](internal/channels/fanout.go) replicates the cheap filter so a directed-elsewhere member is never queued into the floor round only to be suppressed. The `respond_policy` enum is reframed as the `participant`/`addressed`/`observer` disposition vocabulary (back-compat above). Acceptance: [`MT-CHANNEL-RELEVANCE-001`](docs/manual-tests/MT-CHANNEL-RELEVANCE-001.md). See the [relevance amendment](docs/rfcs/0030-amendment-relevance-gated-response.md) ([#536](https://github.com/mkhomutov/Persatrix/pull/536), [#537](https://github.com/mkhomutov/Persatrix/pull/537)).
- *(peer-voice prompt — RFC 0030 relevance amendment)* A new `prompts/runtime/safety/peer-conversation-voice.md` safety snippet frames a persona in a group channel as a **colleague among peers, not an assistant serving a user**: address people by name, build on what others have already said this round, and disagree/defer the way a colleague would rather than performing helpfulness. Where the Tier A gate decides *whether* a persona may speak, this nudge shapes *how* it speaks when it does. It is rendered unconditionally through prompt assembly alongside [`reply-discretion.md`](prompts/runtime/safety/reply-discretion.md) and [`conversational-pacing.md`](prompts/runtime/safety/conversational-pacing.md), carrying the DM carve-out inline. See the [v0.3.7 plan §Workstream 1c](docs/v0.3.7-plan.md#phase-1--implement-the-three-realism-workstreams).
- *(F-7 Option D — person identity on the cross-room tier)* Person identity (name / role / stable preferences) now lives on the genuinely **cross-room relationship tier** rather than the room-scoped notes tier, so a persona recognizes a person on a fresh channel via **both** ambient injection and explicit recall (closing the F-7 seam where explicit `recall_notes` was narrower than ambient injection). A nullable `identity` (JSON) column is added to the `relationships` table (migration 13), `store_note(contact:*)` writes through to it and renders cross-room, and pre-cutover `contact:<id>` notes are backfilled onto it (migration 14); the interim Option-A `recall_contact_notes` carve-out is retired (identity now lives on the relationship tier only). Carries a forward-only persona-memory migration (v12→v14, see Upgrade Notes). See [ISSUE-0093](docs/issues/ISSUE-0093-person-identity-cross-room-tier.md) + the [RFC 0031 amendment](docs/rfcs/0031-amendment-person-identity-cross-room-tier.md) ([#549](https://github.com/mkhomutov/Persatrix/pull/549)–[#556](https://github.com/mkhomutov/Persatrix/pull/556)).

### 🐛 Bug Fixes

- *(v0.3.7 conversation test-findings — F-1…F-6)* The end-to-end conversation probe surfaced six realism-blocking gaps, all fixed: **F-1** the always-on `external-data-handling` safety snippet leaked onto plain user turns (a benign "you're an AI persona" message drew an injection-style deflection citing a "page" that was never fetched) — now scoped to flagged envelopes ([#540](https://github.com/mkhomutov/Persatrix/pull/540)); **F-2** a persona was blind to its own conversation window (denied it could read past messages) — a window self-awareness snippet ([#541](https://github.com/mkhomutov/Persatrix/pull/541)) plus interim group-channel window sizing ahead of RFC 0034 Phase 3 ([#542](https://github.com/mkhomutov/Persatrix/pull/542)); **F-3** the `memory-tool-usage` prompt promised cross-conversation memory while writing person facts to the room-scoped notes tier — prompt-honesty fix ([#543](https://github.com/mkhomutov/Persatrix/pull/543)) plus person-keyed contact notes recalled cross-room ([#544](https://github.com/mkhomutov/Persatrix/pull/544)); **F-4** no shared channel world-state let personas contradict each other on shared context — a channel roster is now fetched, rendered, and injected into per-event context ([#547](https://github.com/mkhomutov/Persatrix/pull/547), [#548](https://github.com/mkhomutov/Persatrix/pull/548)); **F-5** the `agent_tool_invocations_total` metric carried `agent_id="unknown"` — the agent id is bound once at startup, correcting every `current_agent_id()` metric ([#546](https://github.com/mkhomutov/Persatrix/pull/546)); **F-6** the F-1 carve-out is now **sender-aware** so "engage directly with a surprising claim" is conditioned on the author ([#545](https://github.com/mkhomutov/Persatrix/pull/545)). See the [test-findings PR plan](docs/v0.3.7-test-findings-pr-plan.md).
- *(F-8 — `@everyone` broadcast)* An `@everyone` broadcast was rejected by the agent inbound validator, so the directed-filter-disabling broadcast never reached the floor round (surfaced as `MT-CHANNEL-RELEVANCE-001` Step 4). The inbound validator now accepts the `@everyone` broadcast sentinel ([#562](https://github.com/mkhomutov/Persatrix/pull/562)) and the CLI `channel send --mention-all` emits it ([#563](https://github.com/mkhomutov/Persatrix/pull/563)); [ISSUE-0094](docs/issues/ISSUE-0094-everyone-broadcast-rejected-by-agent-inbound-validation.md) resolved.

### 📚 Documentation

- *(RFC 0049 — memory consolidation gradient, design ratified docs-only)* The [memory-consolidation-gradient RFC](docs/rfcs/0049-memory-consolidation-gradient.md) — the vertical "consolidation level" axis + the one law *scope = f(consolidation level)* and the §D re-rooting that lets consolidated topic knowledge cross rooms behind the RFC 0037 egress gate — is **ratified as design only**: **no RFC 0049 code ships in v0.3.7** (all implementation is v0.4.0, gated on the RFC 0037 confidentiality keystone). Landed with four amendment stubs ([RFC 0031 fact-scope-by-consolidation-level](docs/rfcs/0031-amendment-fact-scope-by-consolidation-level.md), [RFC 0027 cross-scope consolidation](docs/rfcs/0027-amendment-cross-scope-consolidation.md), [RFC 0028 decisions-as-readable-memory](docs/rfcs/0028-amendment-decisions-as-readable-memory.md), and the `memory-scope-axes.md` decision-4 supersession); RFC 0049 stays `📋 Proposed` ([#559](https://github.com/mkhomutov/Persatrix/pull/559), [#560](https://github.com/mkhomutov/Persatrix/pull/560)).
- *(operator guides + README/ROADMAP)* The [channels guide](docs/guides/channels.md) (the per-membership `respond` disposition vocabulary + the directedness / no-pile-on behaviour) and the persona/conversation guide (group-channel working memory — personas see who said what this round + the peer-voice framing) were verified against the shipped surface; the README Roadmap row + the ROADMAP Version Map / RFC index were refreshed; the [v0.3.7 release checklist](docs/v0.3.7-release-checklist.md) landed ([#565](https://github.com/mkhomutov/Persatrix/pull/565)).
- *(v037 planning + release-prep)* The v0.3.7 [release-prep plan](docs/v0.3.7-release-prep-plan.md) ([#558](https://github.com/mkhomutov/Persatrix/pull/558)) and the [MT execution report](docs/manual-tests/v0.3.7-execution-report.md) ([#561](https://github.com/mkhomutov/Persatrix/pull/561), re-run on the post-F-8-fix tip [#564](https://github.com/mkhomutov/Persatrix/pull/564)).

### 🧪 Testing

- The two headline manual tests — [`MT-CHANNEL-RELEVANCE-001`](docs/manual-tests/MT-CHANNEL-RELEVANCE-001.md) *addressing-aware directedness / no-pile-on* (the **primary v0.3.7 gate**: directed → exactly one reply; open-floor → all `participant`s; `@everyone` → filter disabled; suppressed members cost zero) and [`MT-PERSONA-CONVERSATION-002`](docs/manual-tests/MT-PERSONA-CONVERSATION-002.md) *persona conversational continuity* (per-peer attributed transcript + cross-peer referential follow-up) — plus a **combined realism walkthrough** were executed **live on Anthropic** against the `92a5a00` RC tip; see the [execution report](docs/manual-tests/v0.3.7-execution-report.md).
- Structural release-blocker gates green on the RC tip: the directedness receiver-gate + Go candidate-set parity + disposition-normalization suites, the RFC 0034 Phase 2 conversation-window suite (per-peer prefixing + the `(channel_id, limit)` fetch-cache-key fix), and the **identity-migration suite** (forward-migrate v12→v14, backfill correctness, crash-replay safety). The carried-forward alias cost-attribution + session/epoch structural-isolation gates and the cost-regression "uninvolved persona costs zero" invariant (Tier A makes no new provider call) stay green.

[0.3.7]: https://github.com/mkhomutov/Persatrix/compare/v0.3.6...v0.3.7

## [0.3.6] - 2026-06-04

> **Codename:** Web Console

### Highlights

- **Open a URL and talk — the embedded web console (RFC 0048 Slice 1: Interactions).** Run the orchestrator with `--enable-ui`, open `http://localhost:8080/ui`, pick a persona, chat with it, and watch a channel — with zero CLI knowledge. The console is a Svelte single-page app served same-origin from the Go binary (`embed.FS`, no separate web server, no Node runtime in the deployed binary), rendering over the existing chat / channels / agents REST API (RFC 0002 / 0011 / 0016). It boots off two read-only endpoints — `GET /api/v1/ui/config` (per-panel feature toggles, with a runtime-derived `available` flag) and `GET /api/v1/ui/context` (the `principal=local` forward-compat identity source) — and renders only panels that are both `enabled` (in `config/ui.yaml`) and `available` (subsystem wired). The Chat panel passes optional session/epoch selectors through (demonstrating the v0.3.5 isolation story from the browser); the Channel-timeline panel stays live by polling with visibility-pause + error-backoff + head-poll de-dupe, plus an optional human publish. See the [web console guide](docs/guides/web-console.md).
- **Feature-toggled vertical slices.** `config/ui.yaml` ships Slice-1 panels on (`chat`, `channel_timeline`) and later-slice panels off (`memory_strip`, `cost`), so memory inspector / isolation verifier / cost / control-plane panels land additively in v0.4.0+ with no Slice-1 rework. The toggle file is schema-validated (`schemas/ui.schema.json`, wired into `make validate`); `available` is runtime-derived and authoring it is a validation error.
- **Repo's first JS toolchain, isolated to `web/`.** A Svelte + Vite build (`make ui`) emits static assets embedded into the orchestrator. The Go-only build/test lane stays green against a committed placeholder embed (`go build ./...` needs no Node); only `make ui`, the Docker image build, and the CI release lane produce the real bundle. The demo compose stack (`make demo-offline` / `docker compose up --build`) bakes the bundle in-image, so the console works from a clean clone with no host JS toolchain.
- **Create channels from the browser (RFC 0048 channel-creation amendment).** The Channels panel gains a collapsed "New channel" form: pick a name (the server derives the canonical `group:<name>` id, shown read-only), an optional description, and **persona** members (task agents are excluded) with a per-member respond policy, over the already-exposed `POST /api/v1/channels` (no new endpoint); the acting user is auto-added as a member so they can post. It closes the Slice-1 gap where a tester could *watch* personas interact but could not *create* a channel to put them in without hand-editing `config/channels.yaml`. The affordance is **on by default** (the `channel_timeline.create_enabled` toggle defaults true; set it false to hide the affordance) and renders only when the channel store is wired (`create.available`, runtime-derived) — the forward-compat seam that becomes capability-gated once RFC 0039 auth lands. The structural-write-before-auth carve-out is a signed-off, local-mode-only exception that adds **zero new reachability** (the endpoint is already reachable unauthenticated on the localhost surface; the console only changes its discoverability) — the console itself stays off by default (`--enable-ui`) and localhost-bound.
- **One conversation surface — the Chat panel is retired (RFC 0048 chat-panel-retirement amendment).** A chat *is* a `dm:` channel server-side, so the console no longer maintains two panels over one model: the standalone **Chat panel is removed**, and the **Channels panel** is now the single conversation surface hosting both group channels and DMs. It gains a **persona entry point** — pick a persona to open a direct message, with a persona header (name — role — capabilities), the synchronous chat-façade send and its abortable "thinking…" turn, and the session/epoch scope selector + "Acting as" override. A DM **renders as a channel** (one timeline + poll path for both kinds of conversation); the per-turn scope *annotation* is dropped in exchange (the scope selector itself still scopes each send). The cross-panel "view in timeline" deep-link (`nav.svelte.js`) is gone — a DM is just an in-panel selection. The "New channel" form is **group-only** (its redundant direct-message mode removed; the persona picker is the single DM entry point). Slice 1 is now a single-panel console; the shell keeps its tab scaffold for the v0.4.0+ panels. No REST endpoint or schema change (the chat façade `POST /api/v1/agents/{id}/chat` stays — RFC 0032 owns its fate); `config/ui.yaml` drops the `chat:` toggle.
- **Personas in a group channel take turns now — floor control is on by default (RFC 0030 Layer 2.5, the floor-control amendment).** Before this, a single message in a channel with two or more responders was fanned out to all of them **concurrently and fire-and-forget**, so each persona composed its reply against a transcript containing **none** of its peers' replies — N overlapping, mutually-blind answers to one prompt (cascade depth and reply budgets bounded the *volume* of the mess but never its *order*). **Floor control** serializes the responders into a deterministic speaker round: ordered mentioned-first then by member order (frozen at round start), they take the floor **one at a time**, each dispatched only after the previous speaker's reply has landed in history, so every persona reads its predecessors before composing. A stalled speaker is bounded by a per-turn timeout (`floor_turn_timeout_seconds`, default **45 s** — distinct from the 5 s per-recipient fanout timeout); non-responders are still delivered concurrently for memory ingestion, off the floor. The trade is latency — responders go serial — which is the intended cost of a coherent, mutually-aware conversation. It is **on by default for every group channel** — whether declared in `config/channels.yaml` or created at runtime via `POST /api/v1/channels` (the console "New channel" form), and persisted runtime channels are re-resolved on restart — and a no-op below two responders (a DM is single-responder), so the default is free for one-on-one chats; set `floor_control: false` on a config channel to opt back out. Orchestrator-side only, single-replica (floor state is in-process). Floor telemetry shipped with it (PR 4): a `channel.conversation.floor_turn{channel_type, outcome=replied|timeout}` counter and a `channel.conversation.floor_round_duration{channel_type}` histogram make the serialization's latency cost and timeout rate observable, so the 45 s per-turn default is data-driven. See the [channels guide §7](docs/guides/channels.md), [observability §11.3](docs/observability.md), and the [floor-control amendment](docs/rfcs/0030-amendment-floor-control-speaker-serialization.md).

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| **[Feature]** Embedded web console (`--enable-ui`, **off by default**) | A new `--enable-ui` flag serves the RFC 0048 Slice 1 web console at `/ui`. It is **off by default**; with it off, `/ui/` is a clean 404 and no default runtime behaviour changes. New surfaces: the `/api/v1/ui/config` + `/api/v1/ui/context` endpoints, the `config/ui.yaml` toggle file (+ `schemas/ui.schema.json`), the `web/` JS toolchain, and the `make ui` / `make run-ui` build targets. |
| **[Security — load-bearing]** Localhost-only until auth | The console makes the **unauthenticated** REST surface browser-discoverable. The orchestrator still binds `127.0.0.1` by default and the console is off by default, but **do not expose the console (or `:8080`) beyond localhost without fronting the orchestrator with an authenticating reverse proxy** until RFC 0039 (accounts/auth) ships. Slice 1 is interact-only (chat + optional channel publish); the destructive/admin control plane is Slice 5 and is hard-gated on RFC 0039. See [web console guide §Security](docs/guides/web-console.md#security--do-not-expose-beyond-localhost). |
| **[Scope]** Slice 1 only — later panels deferred | v0.3.6 ships the Interactions slice (chat + channel timeline). The memory inspector, isolation verifier, cost/observability, and control-plane panels are deferred to v0.4.0+ / post-RFC 0039; their toggles ship **off**. Channel real-time push and chat token streaming are named later enhancements (Slice 1 polls + synchronous chat). |
| **[Behaviour — default change]** Floor control on for group channels | Group channels with ≥2 responders now reply **serially** (one persona at a time, each reading the prior speaker) instead of concurrently. This **adds latency** — a round of N responders costs ~N reply-compositions end-to-end instead of one — in exchange for coherent, mutually-aware conversation. It is on by default for every group channel — declared in `config/channels.yaml` or created at runtime via the console / `POST /api/v1/channels` (persisted ones re-resolved on restart) — and a no-op below two responders (DMs are unaffected). To restore the old concurrent fanout for a config channel, set `floor_control: false` on it. Per-turn wait is `floor_turn_timeout_seconds` (default 45 s). Single-replica only (floor state is in-process). See [channels guide §7 "Floor control"](docs/guides/channels.md). |

### 🚀 Features

- *(RFC 0048 Phase 1 / Slice 1 — embedded web console)* The interact-only console landed across the six-PR Phase 1 plan: the `WithUI` `ServerOption` + `--enable-ui` flag + `embed.FS` static serving under `/ui/` ([#496](https://github.com/mkhomutov/Persatrix/pull/496)), the read-only `/api/v1/ui/config` + `/api/v1/ui/context` endpoints + `config/ui.yaml` + [`schemas/ui.schema.json`](schemas/ui.schema.json) ([#497](https://github.com/mkhomutov/Persatrix/pull/497)), the repo's first JS toolchain — a Svelte + Vite build (`make ui`) emitting an embedded bundle ([#498](https://github.com/mkhomutov/Persatrix/pull/498)), the Chat panel ([#501](https://github.com/mkhomutov/Persatrix/pull/501)), the Channel-timeline panel ([#502](https://github.com/mkhomutov/Persatrix/pull/502)), the in-image Docker enablement (bundle baked in-image, no host JS toolchain) ([#503](https://github.com/mkhomutov/Persatrix/pull/503)), and the docs/status closeout ([#504](https://github.com/mkhomutov/Persatrix/pull/504)).
- *(RFC 0048 interaction-UX amendment)* Console interaction polish: persona legibility ([#506](https://github.com/mkhomutov/Persatrix/pull/506)), chat-history continuity ([#507](https://github.com/mkhomutov/Persatrix/pull/507)), the session/epoch scope selector + composer ergonomics ([#508](https://github.com/mkhomutov/Persatrix/pull/508)), the "Acting as" tester identity override ([#509](https://github.com/mkhomutov/Persatrix/pull/509)), and onboarding + cross-panel continuity ([#510](https://github.com/mkhomutov/Persatrix/pull/510)) — with chat disabled for task agents ([#511](https://github.com/mkhomutov/Persatrix/pull/511)), the selected persona persisted across tab switches ([#512](https://github.com/mkhomutov/Persatrix/pull/512)), and a reachable persona switcher + exit-to-lobby + stable agent order ([#513](https://github.com/mkhomutov/Persatrix/pull/513)).
- *(RFC 0048 channel-creation + chat-panel-retirement amendments)* Create group channels from the browser over the existing `POST /api/v1/channels` (no new endpoint), the acting user auto-added as a member ([#515](https://github.com/mkhomutov/Persatrix/pull/515)); the standalone Chat panel is **retired** and the Channels panel becomes the single conversation surface hosting both group channels and DMs, with a persona entry point for direct messages ([#516](https://github.com/mkhomutov/Persatrix/pull/516)).
- *(console — @-mentions, RFC 0011 over RFC 0048)* The channel composer gains `@`-mention support, so a human publish can address specific personas (mentioned-first floor ordering) from the browser ([#524](https://github.com/mkhomutov/Persatrix/pull/524)).
- *(RFC 0030 Layer 2.5 — floor control)* Serialized, mutually-aware multi-persona channel replies: the floor registry + responder ordering (inert) ([#518](https://github.com/mkhomutov/Persatrix/pull/518)), the serialized floor loop behind the flag ([#519](https://github.com/mkhomutov/Persatrix/pull/519)), default-on for group channels + docs + `MT-CHANNEL-GOV-002` ([#520](https://github.com/mkhomutov/Persatrix/pull/520)), and floor telemetry — the `floor_turn` counter + `floor_round_duration` histogram ([#521](https://github.com/mkhomutov/Persatrix/pull/521)).

### 📚 Documentation

- *(RFCs)* [RFC 0048](docs/rfcs/0048-operator-tester-web-console.md) — operator/tester web console ([#493](https://github.com/mkhomutov/Persatrix/pull/493)) → Implementing + the [Phase 1 PR plan](docs/rfcs/0048-phase1-pr-plan.md) ([#494](https://github.com/mkhomutov/Persatrix/pull/494)); the interaction-UX amendment proposal ([#505](https://github.com/mkhomutov/Persatrix/pull/505)); the channel-creation + chat-panel-retirement amendments ([#514](https://github.com/mkhomutov/Persatrix/pull/514)); the [RFC 0030 Layer 2.5 floor-control amendment](docs/rfcs/0030-amendment-floor-control-speaker-serialization.md) ([#517](https://github.com/mkhomutov/Persatrix/pull/517)).
- *(operator guides)* The [web console guide](docs/guides/web-console.md) (incl. the load-bearing §Security localhost-only / reverse-proxy note), [channels guide §7 "Floor control"](docs/guides/channels.md), and [observability §11.3](docs/observability.md) (the `floor_turn` counter + `floor_round_duration` histogram) shipped with the implementation PRs and were verified at release-prep ([#525](https://github.com/mkhomutov/Persatrix/pull/525)).
- *(v036 planning + release-prep)* The v0.3.6 [master plan](docs/v0.3.6-plan.md) ([#495](https://github.com/mkhomutov/Persatrix/pull/495)), the [release-prep plan](docs/v0.3.6-release-prep-plan.md) ([#522](https://github.com/mkhomutov/Persatrix/pull/522)), the [MT execution report](docs/manual-tests/v0.3.6-execution-report.md) ([#523](https://github.com/mkhomutov/Persatrix/pull/523)), and the README / ROADMAP refresh + [v0.3.6 release checklist](docs/v0.3.6-release-checklist.md) + guide verification ([#525](https://github.com/mkhomutov/Persatrix/pull/525)). The v0.3.5 post-release follow-up closed out the prior cycle ([#492](https://github.com/mkhomutov/Persatrix/pull/492)).

### 🧪 Testing

- Two new manual tests, **authored during implementation and executed live at release-prep** against the `83d478b` RC tip on Anthropic — [`MT-CONSOLE-001`](docs/manual-tests/MT-CONSOLE-001.md) *Web Console fresh-stack Interactions slice* (the **primary v0.3.6 gate**: `make ui` real bundle → `--enable-ui` serves `/ui` → chat hero flow + within-conversation recall → session/epoch scope pass-through → live channel timeline → the `ui.yaml` `available:`-rejection guard → in-image Docker console) and [`MT-CHANNEL-GOV-002`](docs/manual-tests/MT-CHANNEL-GOV-002.md) *Floor control: ordered, mutually-aware multi-persona replies* (the **headline-claim test**: an ordered three-persona round with a clear yield, demonstrably **not** the pre-amendment concurrent shout). See the [execution report](docs/manual-tests/v0.3.6-execution-report.md).
- New **web-console structural gates** (the `WithUI`/`WithoutUI` serving + redirect + no-directory-listing tests, the `/api/v1/ui/config|context` shape + availability tests, the build-version fallback/override tests, the `404`-when-disabled matrix, and `TestAssets_TreeContainsOnlyWebAssets`) and **floor-control suites** ([`floor_control_test.go`](internal/channels/floor_control_test.go), [`fanout_floor_test.go`](internal/channels/fanout_floor_test.go) — serialization, mutual visibility, mentioned-first, deferred fanout, timeout-advance, telemetry, the config tri-state, the registry, **plus the flag-off regression path** that runs the legacy concurrent fanout unchanged). The Svelte SPA carries a **115-test Vitest** suite.
- The carried-forward **alias cost-attribution gate** (`internal/server/cost_alias_gate_test.go`, `internal/cost/cost_alias_pricing_test.go`) and the **session + epoch structural-isolation gates** (`tests/integration/test_{session,epoch}_*.py`) stay green against the v0.3.6 RC tip.

[0.3.6]: https://github.com/mkhomutov/Persatrix/compare/v0.3.5...v0.3.6

## [0.3.5] - 2026-06-01

> **Codename:** Session-Scoped Memory

> **What a "session" means here:** a named, **persistent room-continuity** namespace keyed `(agent, channel)` — *not* a login/connection session, and *not* a single conversation that ends. A session **accumulates** memory across runs and restarts (that is the point); switching sessions changes *which* room's memory a run reads and writes, it does **not** wipe anything. For a rerun that inherits *nothing* (a clean slate, same room and user), use the sibling **`epoch`** axis below — not a new session. See the [sessions](docs/guides/sessions.md) and [epochs](docs/guides/epochs.md) guides.

### Highlights

- **Persona-memory recall is now session-scoped — concurrent conversations no longer bleed into each other** (RFC 0031, Phases 2–4). A run reads only the rows tagged with its own session (its room), plus the always-visible `legacy` carve-out, across all four persona-memory tiers (episodes, relationships, facts, notes). Reaching across sessions (`sessions="*"`) is an explicit, library-only opt-in with no operator entry point — the default context path is pinned never to reach it. A session is *room continuity*, not a clean slate.
- **A first-class `persatrix session …` operator surface.** `persatrix session new --label L [--activate] / use <id-or-label> / list [--include-archived] / current / archive <id-or-label>`, backed by the `/api/v1/sessions` REST registry. The active session resolves by precedence `--session` flag > `PERSATRIX_SESSION_ID` env > `~/.persatrix/active-session` pointer file > built-in `legacy`.
- **Run/test isolation is the sibling `epoch` axis — a same-room/same-user rerun under a fresh `epoch` inherits nothing** ([ISSUE-0085](docs/issues/ISSUE-0085-epoch-axis-run-isolation.md)). Where a session gives a room continuity, a fresh `PERSATRIX_EPOCH` (or `--epoch`) gives a *clean slate* under the same room and user — strict-equality isolation of episodes / relationship trust / person-facts with no `legacy` carve-out and no `*` wildcard. This replaces `make reset` as the everyday run-isolation tool; `make reset` is reframed as the whole-stack volume nuke.
- **Together these close the F-3 cross-run state bleed at the root** — the *recall* half via session scoping, the *structural* half via epoch strict-equality isolation.
- **The model-alias layer is complete (RFC 0033 Phase 3, co-resident).** A raw vendor `model:` that is not a declared alias is now **rejected at resolve** with a loud `SystemExit`; `models.aliases` is the single source of truth for model identity. The v0.3.4 raw-ID fall-through deprecation, the `_infer_provider` heuristic, and the `provider_inference` accessor are retired; `config/optimization.yaml` `schema_version` bumps `"0.2"` → `"0.3"`.

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| **[Behaviour change — session-scoped default recall]** | Default persona-memory recall is now **session-scoped** (here a *session* = a room's persistent memory namespace, not a login/conversation): a run reads only the rows tagged with its active session, plus the always-visible `legacy` carve-out, across all four persona-memory tiers (episodes, relationships, facts, notes). Concurrent conversations no longer bleed into each other. Reaching across sessions (`sessions="*"`) is an explicit, library-only opt-in with no operator entry point (RFC 0031 §D). This is a behaviour change from the pre-v0.3.5 whole-store recall. |
| **[Feature]** `persatrix session …` operator CLI | A first-class session surface: `persatrix session new --label L [--activate] / use <id-or-label> / list [--include-archived] / current / archive <id-or-label>`, backed by the `/api/v1/sessions` REST registry. The active session resolves by precedence `--session` flag > `PERSATRIX_SESSION_ID` env > `~/.persatrix/active-session` pointer file > built-in `legacy` (RFC 0031 OQ #6). `legacy` is a reserved label; archive is one-way. See the [sessions operator guide](docs/guides/sessions.md). |
| **[Feature]** Epoch run/test-isolation axis | A new `epoch` axis isolates a *same-named* rerun structurally: a fresh `PERSATRIX_EPOCH` (orchestrator boot env) or `--epoch <id>` (per-invocation override on `chat` / `channel send` / `channel reply`), default `live`, inherits **none** of a prior run's episodes / relationship trust / person-facts — strict-equality isolation with no `legacy` carve-out and no `*` wildcard. Production is unchanged (untagged deployments run under `live`). See the [epochs operator guide](docs/guides/epochs.md). |
| **[Behaviour change — `make reset` reframed]** | `make reset` (`docker compose down -v`) is now positioned as the **whole-stack volume nuke** — all epochs across all sessions — not the everyday run-isolation tool. For an isolated rerun reach for a fresh `epoch` instead; `make reset` is for when you want the volumes themselves gone. The breadcrumb is reframed across the [channels](docs/guides/channels.md), [persona-agents](docs/guides/persona-agents.md), and [sessions](docs/guides/sessions.md) guides. |
| **[Migration]** Auto-migration to `legacy` / `live` | A pre-v0.3.5 database auto-migrates on first open. Persona memory (`memory.db`) runs migrations v4→**v12**: the session axis added `session_id` (episodes/relationships v7, facts v8, notes v9, defaulting `'legacy'`); the epoch axis added `epoch_id` across all five tiers + the `relationships` primary key (v12, defaulting `'live'`). The channel store (`channels.db`) is at `channelStoreSchemaVersion = 6` (v5 session axis, v6 epoch axis). Every existing row lands under `session_id='legacy'` / `epoch_id='live'` and stays visible from the default resolution path — single-world deployments are byte-identical. |
| **[Behaviour change — RFC 0033 Phase 3]** Raw vendor IDs rejected | A `model:` in `config/agents.yaml` / `config/optimization.yaml` that is **not a declared alias** is now **rejected at resolve** with a loud `SystemExit` — the RFC 0033 §E raw-ID fall-through (a deprecation warning in v0.3.4) is removed, along with the `_infer_provider` heuristic, the `provider_inference` accessor/YAML block, and the `persatrix.llm.alias.raw_id_usage` gate counter. `config/optimization.yaml` `schema_version` bumps `"0.2"` → `"0.3"`. `models.aliases` is the single source of truth for model identity — provider is data, not inferred. |

### 🚀 Features

- *(RFC 0031 Phase 2 — session-scoped default recall)* Session scoping landed across all four persona-memory tiers: notes-tier `session_id` coverage + migration v9 (#448), episodic + notes recall filtering (#449), relationship + facts recall filtering (#450), the facade read-path `sessions=` extension + call-site threading (#451), the dementia-test bridge + interactions/supersede session scoping (#452), and the Phase 2 closeout that closed **F-3** at the recall layer (#461). The session-emission isolation work (per-request session binding store → dispatch-path emission → end-to-end gate, #458–#460) and the context-local session-id + principal/tenant dimension (#453–#456) underpin it.
- *(RFC 0031 Phase 3 — operator CLI)* A first-class session surface: the orchestrator `/api/v1/sessions` REST registry (#464), the `new / list / archive` registry verbs (#466), the active-session pointer file + `use / current / --activate` (#467), the dropped session-binding sender axis (#468), and the `--session` override on `chat` / `channel` (#469), with the operator-surface e2e gate + closeout (#470). Phase 4 shipped the operator guides + reframed `make reset` breadcrumb and closed [ISSUE-0051](docs/issues/ISSUE-0051-per-session-memory-namespacing-channels.md) (#471).
- *(Epoch run-isolation axis — Phase 3b)* A new structural `epoch` axis ([ISSUE-0085](docs/issues/ISSUE-0085-epoch-axis-run-isolation.md)): the `agents/epoch_id.py` leaf module (#472), migration v12 (`epoch_id`) + channel-store v6 (#474), the strict-equality filter + per-tier wiring (#475), the gRPC rail — orchestrator emission + ingress lift (#476), the `--epoch` operator override (#477), and the closeout with the F-3 structural-isolation gate (#478).
- *(RFC 0033 Phase 3 — alias-layer closeout)* Raw vendor IDs are now rejected at resolve: the resolver raw-ID pass-through removal (#481) and the `_infer_provider` / `provider_inference` retirement + `schema_version` `"0.3"` (#482), with eager whole-map alias pricing validation at server boot (#483, ISSUE-0071), memory-compression routed through the alias layer (#484, ISSUE-0072), `task_agents`/`evaluators` routing defaults documented as reserved (#485, ISSUE-0069), and mock agents resolving through an alias (#486, ISSUE-0074).

### 🐛 Bug Fixes

- *(channels)* ISSUE-0068 ([#479](https://github.com/mkhomutov/Persatrix/pull/479)) — the chat peer is now recorded with its `participant_type` carried to the agent (the lone defect fix in the window).

### 📝 Refactoring

- *(ISSUE-0053)* Extract `_coerce_event_timeout` to its own submodule (#480).
- *(ISSUE-0083)* Drop the session-binding sender axis — `(agent, channel, user)` → `(agent, channel)` (#468).

### 📚 Documentation

- *(v035)* Open the v0.3.5 master plan — Session Isolation (RFC 0031 Phases 2–4) (#447); fold the ISSUE-0085 epoch axis into v0.3.5 scope (#473); RFC 0031 Phase 3 PR plan + session scope-axes reframing (#462); ISSUE-0082 session-emission PR plan (#457).
- *(operator guides)* New [sessions](docs/guides/sessions.md) and [epochs](docs/guides/epochs.md) guides; the reframed `make reset` breadcrumb across the channels / persona-agents / sessions guides (#471, #478).
- *(release-prep)* The v0.3.5 release-prep plan (#487), the MT execution report (#488), and the README / ROADMAP refresh + the [v0.3.5 release checklist](docs/v0.3.5-release-checklist.md) + the sessions/epochs guide verification (#489).

### 🧪 Testing

- Three new manual tests — [`MT-SESSION-002`](docs/manual-tests/MT-SESSION-002.md) *session operator surface, live* (the **primary v0.3.5 gate**: the `new / use / list / archive / current` round-trip + the `--session` > env > pointer-file > `legacy` resolution chain), [`MT-SESSION-003`](docs/manual-tests/MT-SESSION-003.md) *F-3 recall isolation + within-session continuity* (the **headline-claim test**: a fresh session surfaces none of a prior session's rows **and** a within-session arc still continues), and [`MT-EPOCH-001`](docs/manual-tests/MT-EPOCH-001.md) *epoch structural run-isolation* (a fresh `PERSATRIX_EPOCH` / `--epoch`, same room + same `--user`, inherits nothing).
- New **session + epoch structural-isolation gates** — the automated release-blocker counterpart to the new MTs: [`test_session_recall_isolation.py`](tests/integration/test_session_recall_isolation.py), [`test_epoch_run_isolation.py`](tests/integration/test_epoch_run_isolation.py), [`test_session_continuity.py`](tests/integration/test_session_continuity.py), [`test_session_emission_isolation.py`](tests/integration/test_session_emission_isolation.py), and [`test_session_id_cross_process.py`](tests/integration/test_session_id_cross_process.py). The dementia-test continuity gate ([`MT-MEMORY-005`](docs/manual-tests/MT-MEMORY-005-dementia-test.md)) is **re-run** on the session-routed config to prove single-session continuity is not regressed.
- The carried-forward **alias cost-attribution gate** (`internal/server/cost_alias_gate_test.go`, `internal/cost/cost_alias_pricing_test.go`) stays green after the RFC 0033 Phase 3 raw-ID rejection; the bored-persona [`cost-regression-gate`](tests/integration/test_bored_persona_cost.py) remains a release-blocker.
- Full **40-row** manual-test surface (3 new + the 37-row v0.3.4 surface carried forward, regression-checked against the session/epoch-routed config) executed against the `3ceb400` RC tip — 34 Pass, 5 Accepted-with-known-gap, 1 Deprecated, 0 Fail (#488).

[0.3.5]: https://github.com/mkhomutov/Persatrix/compare/v0.3.4...v0.3.5

## [0.3.4] - 2026-05-27

> **Codename:** Any Model, Any Provider

### Highlights

- **Any Model, Any Provider — the same agents run on Anthropic, OpenAI, a free local model (Ollama), or a `$0` offline mock, selected the one standard way** (RFC 0033, Phases 1–2). Every agent, routing default, and the summarisation path references a logical model alias (`quality` / `fast` / `summarizer`) that resolves to a `(provider, model_id, pricing)` record in [`config/optimization.yaml`](config/optimization.yaml). A vendor retirement or a provider swap is a **one-line config edit** to one alias entry — not a code change, not a config sweep. All four providers (`anthropic` / `openai` / `ollama` / `mock`) are peers dispatched through the same factory path; there is no env force-knob and no special-cased provider.
- **No default provider — nothing privileges a vendor or can spend money by default.** The shipped config ships its role aliases **unconfigured**, so a plain `docker compose up` fails loud at agent startup with an actionable message until you pick a provider. Each provider is an equal one-command demo (`make demo-anthropic` / `demo-openai` / `demo-ollama` / `demo-offline`), each mounting a per-provider alias config via a Compose overlay. The offline `MockProvider` and the Ollama local-model provider both run the whole society at **`$0` cloud spend**, with the OTel `gen_ai.*` spans, token metrics, and the RFC 0023 wallet-lease path fully populated.

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| **[Feature]** Provider-agnostic model aliases | Agents, routing defaults, and the summarisation path reference a logical alias (`quality` / `fast` / `summarizer`) resolved to a `(provider, model, pricing)` record in [`config/optimization.yaml`](config/optimization.yaml) `models.aliases` (RFC 0033). A vendor retirement or provider swap is a one-line edit to one alias entry. `schema_version` bumped `0.1` → `0.2`. A priced OpenAI peer alias ships. |
| **[Behaviour change — no default provider]** Explicit, config-driven provider choice | The shipped `config/optimization.yaml` ships its `quality` / `fast` / `summarizer` role aliases **unconfigured** (`provider: unconfigured`) — there is **no default provider**. A plain `docker compose up` fails loud at agent startup (an actionable `SystemExit`) until you pick one: run a `make demo-*`, or set `provider`/`model`/pricing on the alias. Nothing privileges a vendor or can spend money by default. See [amendment 2026-05-27](docs/v0.3.4-plan-amendment-2026-05-27.md). |
| **[Behaviour change — provider selection]** Config-driven, no env force-knob | All four providers (`anthropic` / `openai` / `ollama` / `mock`) are selected the same standard way — by the resolved alias `provider` field. The `PERSATRIX_OFFLINE` / `PERSATRIX_OLLAMA` global force-knobs are **removed** (they were never in a tagged release — added and removed within the v0.3.4 window). `make demo-anthropic` / `demo-openai` / `demo-ollama` / `demo-offline` each select their provider by mounting a per-provider alias config (`config/demo/<provider>/optimization.yaml`) via a Compose overlay. `PERSATRIX_OLLAMA_MODEL` / `PERSATRIX_OLLAMA_BASE_URL` / `PERSATRIX_OFFLINE_RESPONSES` survive as provider *configuration* (not selection). |
| **[Behaviour change — onboarding]** Provider-neutral compose + `.env` | [`docker-compose.yaml`](docker-compose.yaml) plumbs every provider key (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) into each agent optionally and drops the single-vendor `:?` startup guard, so a config-only swap to a cloud-OpenAI alias authenticates in stock Docker and first-run privileges no vendor; an agent routed to a provider whose key is unset logs a clear startup warning. [`.env.example`](.env.example) is reframed "pick one provider." |
| **[Safety]** Missing-price guard | A resolved alias with no pricing fails closed (loud `SystemExit`) for non-local providers, rather than a silent `$0` estimate that would disable the RFC 0023 budget/lease gate. Local providers (`ollama` / `mock`, or a loopback `base_url`) are exempt — they carry an explicit `0` ($0-real). |
| **[Cost]** Alias-derived pricing + `model_alias` span | `cost.pricing.models` (the Go cost table) is now a checked-in *projection* of the alias map (lock-step test `TestShippedCostPricingDerivedFromAliases`). The `agent.llm.call` span carries `persatrix.llm.model_alias` alongside the physical `gen_ai.request.model` (telemetry-only). |
| Raw-ID back-compat | A raw vendor model ID still resolves (the §E fall-through) but fires a one-shot deprecation warning + increments `persatrix.llm.alias.raw_id_usage`. Phase 3 (raw-ID rejection, `_infer_provider` retirement, schema `"0.3"`) is **observed-traffic gated** — it opens when that counter reads zero across dogfood, → v0.3.5+. |
| `$0`-local vs. the wallet cap | A genuinely-$0 local alias (offline / Ollama) never trips the simulated wallet, so the README's "agent pauses itself at the cap" behaviour shows only on a priced (cloud) alias. The offline / Ollama demos are $0 by design; use `make demo-openai` (or an Anthropic alias) to watch the cap trip. |

### 🚀 Features

- *(RFC 0033 — provider-agnostic model alias layer, Phases 1–2)* Landed across #431 (alias resolver + `models.aliases` block + OpenAI peer alias) → #432 (factory alias resolution + raw-ID deprecation signal) → #433 (config migration to aliases + drop the last model literal) → #434 (missing-price guard) → #435 (`model_alias` span + alias-derived pricing + cost gate) → #436 (documentation sweep) → #437 (Phases-1–2 closeout); PR plan #430. The offline (#422) and Ollama (#423) providers and the knob-free, config-driven provider selection (#440) ship alongside.
- **Any provider, selected the same way — no force-knobs.** All four providers — `anthropic`, `openai`, `ollama`, and the offline `mock` — are now chosen by one mechanism: the resolved `provider` field on a model alias (RFC 0033). Routing the whole society to a local, mock, or OpenAI provider is a one-line edit to the `quality` alias in [`config/optimization.yaml`](config/optimization.yaml), exactly as the release headlines. `mock` and `ollama` are first-class alias providers, dispatched through the same factory path ([`agents/llm_factory.py`](agents/llm_factory.py)) as the cloud providers.
- **Offline / demo mode — run the whole society for $0 with no API key.** The `MockProvider` ([`agents/llm_offline.py`](agents/llm_offline.py)) joins `AnthropicProvider` / `OpenAIProvider` / `OllamaProvider` behind the `LLMProvider` protocol. Point an alias at `provider: mock` and every agent returns scripted, persona-accurate replies with **zero provider calls, zero network, and zero spend**. The provider only emits plain text with `stop_reason=END_TURN`, so the runtime's `synthesize_channel_reply` seam routes it through both the chat-as-DM and channel paths unchanged; synthetic token usage keeps the OTel `gen_ai.*` spans, token metrics, and the RFC 0023 wallet-lease settle path populated at $0. Curated replies live in [`config/offline_responses.yaml`](config/offline_responses.yaml) (override via `PERSATRIX_OFFLINE_RESPONSES`); unmatched turns degrade to a deterministic in-character fallback.
- **Ollama mode — run the whole society on a real local model, no API key, no cloud spend.** A first-class `ollama` provider ([`agents/llm_ollama.py`](agents/llm_ollama.py)) — a thin subclass of `OpenAIProvider`, since Ollama serves the OpenAI-compatible API verbatim at `/v1` — reuses the cloud provider's message/tool translation and overrides only the `gen_ai.system` name (`ollama`) and a localhost-default `base_url`. Point an alias at `provider: ollama` (with a real Ollama tag as `model:`). Unlike offline mode this is **real inference** — usage carries the model's actual token counts — but it runs on your own hardware, so cloud spend is `$0`.
- **Config-driven demos — `make demo-anthropic` / `make demo-openai` / `make demo-ollama` / `make demo-offline`.** Each demo selects its provider by mounting a per-provider alias config from [`config/demo/<provider>/optimization.yaml`](config/demo/) over the stack's `optimization.yaml` via a Compose overlay — no env force-knob, no throwaway key, and all four providers on equal footing. `demo-ollama` bundles an `ollama` container and pulls `PERSATRIX_OLLAMA_MODEL` (default `llama3.2`); the cloud demos need their provider's key. See the [model providers guide](docs/guides/model-providers.md).

### 🔧 Changed

- **No default provider — provider choice is always explicit, and config-driven.** The shipped [`config/optimization.yaml`](config/optimization.yaml) ships its `quality` / `fast` / `summarizer` role aliases **unconfigured** (`provider: unconfigured`), so a plain `docker compose up` **fails loud** at agent startup with an actionable message ("run a `make demo-*`, or set provider/model on the alias") until a provider is picked. Nothing privileges a vendor or can spend money by default.
- **Provider selection is purely config/alias-driven; the `PERSATRIX_OFFLINE` / `PERSATRIX_OLLAMA` global force-knobs are removed** (they were never in a tagged release — added and removed within the v0.3.4 window). The factory no longer has an offline/Ollama pre-check ahead of alias resolution; a provider is chosen only by the resolved `provider` field, so all four providers (`anthropic` / `openai` / `ollama` / `mock`) are peers. `OllamaProvider`'s forced-model substitution is gone — the factory resolves the physical model and threads it through like every other provider, with `PERSATRIX_OLLAMA_MODEL` surviving only as an optional model *configuration* override for ollama-routed agents (it keeps the demo's `ollama-pull` and the agents in lock-step).
- **Provider-neutral onboarding.** [`docker-compose.yaml`](docker-compose.yaml) plumbs **every** provider key (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) into each agent optionally and drops the single-vendor `:?` startup guard — so a config-only swap to a cloud-OpenAI alias authenticates in stock Docker (closes the MT execution report's F-5), first-run privileges no vendor, and an agent routed to a provider whose key is unset logs a clear startup warning instead. [`.env.example`](.env.example) is reframed "pick one provider."

### 📝 Config / Schema

- [`schemas/agent.schema.json`](schemas/agent.schema.json) documents the `provider` field (`anthropic` / `openai` / `ollama` / `mock`) and `provider_config.base_url`, so an explicit `provider: mock` / `provider: ollama` (and the OpenAI-compatible `base_url` for local models) validate instead of being rejected by `additionalProperties: false`. The `ollama` provider defaults `base_url` to `http://localhost:11434/v1`; the `PERSATRIX_OFFLINE_RESPONSES` / `PERSATRIX_OLLAMA_MODEL` / `PERSATRIX_OLLAMA_BASE_URL` provider-configuration env vars are documented in [`.env.example`](.env.example).
- New per-provider demo alias configs under [`config/demo/`](config/demo/) (`anthropic` / `offline` / `ollama` / `openai`, each a `<provider>/optimization.yaml`) — each points the `quality` / `fast` / `summarizer` aliases at one provider with a matching derived `cost.pricing.models`. The shipped base `cost.pricing.models` projects the `unconfigured` placeholder; the alias cost-attribution gate (`internal/server/cost_alias_gate_test.go`, `internal/cost/cost_alias_pricing_test.go`) now pins the configured `config/demo/anthropic` artifact.

### 📚 Documentation

- *(release)* Post-release follow-up for v0.3.3 (#421)
- *(v034)* Open v0.3.4 master plan — Any Model, Any Provider (#424)
- *(v034)* Harden the v0.3.4 plan for provider parity (#425)
- Require plain-English docs and comments (#426)
- *(v034)* RFC 0033 PR plan — Phases 1–2 (#430)
- *(v034)* RFC 0033 PR 6 — documentation sweep (literal vendor IDs → aliases) (#436)
- *(v034)* RFC 0033 PR 7 — Phases-1–2 closeout (#437)
- *(v034)* Release-prep plan — master-plan Phase 3 (#438)
- *(v034)* Release-prep PR 1 — manual-test execution report (alias routing + provider swap + offline + Ollama) (#439)
- *(v034)* New [model-providers guide](docs/guides/model-providers.md) + provider-neutral onboarding, shipped with the knob-free provider-selection refactor (#440)
- *(v034)* Refresh the four MT recipes to config-driven form + live re-run on HEAD (#441)
- *(RFC drafts — tracking only, all `🔨 Draft`)* RFC 0045 — open-core library-extraction policy (#427); RFC 0046 — budget-lease library extraction `persatrix-budget` (#428); RFC 0047 — low-coupling batch library extraction, prompt kit / mock LLM / schemas (#429)

### 🧪 Testing

- Four new manual tests — [`MT-ALIAS-001`](docs/manual-tests/MT-ALIAS-001.md) *alias routing, live* (the **primary v0.3.4 release gate**: an alias-routed agent reports correctly-keyed **non-zero** `/api/v1/cost/summary` priced at the physical model, with `persatrix.llm.model_alias` on the `agent.llm.call` span while `gen_ai.request.model` stays the physical ID); [`MT-ALIAS-002`](docs/manual-tests/MT-ALIAS-002.md) *one-line provider swap* (the headline claim — a single alias edit re-routes the same agent); [`MT-OFFLINE-001`](docs/manual-tests/MT-OFFLINE-001.md) *offline, $0, zero network*; [`MT-OLLAMA-001`](docs/manual-tests/MT-OLLAMA-001.md) *Ollama local model, real tokens, $0 cloud*.
- New **alias cost-attribution gate** — [`internal/server/cost_alias_gate_test.go`](internal/server/cost_alias_gate_test.go) + [`internal/cost/cost_alias_pricing_test.go`](internal/cost/cost_alias_pricing_test.go) — the automated release-blocker counterpart to `MT-ALIAS-001`; with no default provider it pins the configured `config/demo/anthropic` artifact (plus `config/demo/openai` for the priced peer). The carried-forward bored-persona [`cost-regression-gate`](tests/integration/test_bored_persona_cost.py) stays a release-blocker.
- The offline / Ollama factory-interplay regression ([`test_llm_offline.py`](tests/unit/python/test_llm_offline.py) / [`test_llm_ollama.py`](tests/unit/python/test_llm_ollama.py)) and the `unconfigured`-sentinel fail-loud ([`test_model_aliases.py`](tests/unit/python/test_model_aliases.py) / [`test_llm_factory.py`](tests/unit/python/test_llm_factory.py)) keep provider selection config-driven after the force-knob removal.
- Full **38-row** manual-test surface (4 new + 34 carried-forward) executed against the `7e74873` RC tip and re-run in config-driven form on HEAD `6ce23cd`; the release gate is met (#439, #441).

[0.3.4]: https://github.com/mkhomutov/Persatrix/compare/v0.3.3...v0.3.4

## [0.3.3] - 2026-05-22

> **Codename:** Idle Truly Idle

### Highlights

- **Idle is truly idle — a persona with no scheduled work and no inbound traffic now costs nothing** (RFC 0024, Phases 1–4). The persona autonomy loop is event-driven: it parks on `queue.get()` and wakes only on an inbound RPC, a scheduled `autonomy.timers` entry, or a salience memory write. A persona with `autonomy.timers: []` and no inbound traffic does no SQLite recall, no `_inject_memory_context`, no provider call, and no wallet lease — the polling-loop cost-leak class is closed **structurally**, not patched per release. The promise is guarded on every PR by the bored-persona `cost-regression-gate` CI job ([`tests/integration/test_bored_persona_cost.py`](tests/integration/test_bored_persona_cost.py)), a release-blocker.
- **`tick_interval_seconds` keeps working unchanged.** The legacy field is synthesised into a single `ScheduledWake(timer_id="legacy_tick")` through a back-compat adapter, so existing configs need no edit. The deprecation *warning* is RFC 0024 Phase 5 (v0.4.0); removal is Phase 6 (v0.5+).

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| **[Behaviour change — idle is truly idle]** Event-driven autonomy loop | The persona autonomy loop is now event-driven ([RFC 0024](docs/rfcs/0024-event-driven-scheduling.md) Phases 1–4). A persona with `autonomy.timers: []` and no inbound traffic parks on `queue.get()` and pays nothing — no SQLite recall, no `_inject_memory_context`, no provider activity, no wallet lease. The polling-loop cost-leak class is closed structurally, guarded by `cost-regression-gate`. |
| **[Behaviour change — channel dispatch]** Fire-and-forget channel messages | `ReceiveChannelMessage` now enqueues `InboundEventWake(event)` fire-and-forget via `event_loop.enqueue` (not `scheduler.wake()`) and returns `TaskAck` immediately. Chat, sub-agent, and workflow-task paths are unchanged — they keep their synchronous-return contract via `SyncDispatchHandle`. |
| New `autonomy.timers` config block | Per-agent timer list in `agents.yaml` / [`schemas/agent.schema.json`](schemas/agent.schema.json): `id`, `interval_seconds` (≥ `1.0`), `kind` (a `callback_kind` label), optional `jitter_max_seconds`. `tick_interval_seconds` **continues to work** — synthesised as one `ScheduledWake(timer_id="legacy_tick", callback_kind="tick")`; if both are set, `timers` wins (one-time INFO log). Absent block → `timers: []`. Deprecation **warning** is Phase 5 (v0.4.0); removal is Phase 6 (v0.5+). |
| New per-agent SQLite `scheduled_wakes` cache | Derived from `agents.yaml` (source of truth), rebuilt on startup; restores `next_fire_at_ms` so a persona restarted mid-jitter-window resumes its schedule instead of re-randomising. Orphan rows deleted on next startup. No operator action. |
| New `autonomy.salience_threshold` (default `0.95`) + `autonomy.salience_rate_max_per_sec` (default `10`) | Salience-triggered wakes from the memory-write path. **Ships disabled by default** — the threshold is strictly above stock conservative scoring (`REFLECTION_CONTRADICTION_SALIENCE = 0.6`). Calibration is a data-gated follow-up ([RFC 0024 §OQ §2](docs/rfcs/0024-event-driven-scheduling.md#open-questions)). The rate-limit is a DoS guard, not a calibration knob. |
| New `agent.wake.{inbound,scheduled,salience,dropped}` metric counters | Carry `wake.kind` (plus `timer_id` on scheduled, `tier` + `suppressed_reason` on salience). `wake.kind` is an **OTEL/metric dimension only** — *not* a new `LeaseRequest` proto field; [`proto/wallet.proto`](proto/wallet.proto)'s `Cause cause` is unchanged, so v0.3.2 cost-attribution dashboards keep working (`trace_id` already links the lease to the wake span). See [observability.md §10.5](docs/observability.md#105-persatrix-specific-attribute-namespace). |
| RFC 0017 §F empty-context TICK guard is now structurally unreachable but stays in place | Documented as vestigial via an [`action_loop.py`](agents/persona_runtime/action_loop.py) cross-link naming Phase 5/6 as the deletion path. No behaviour change. |
| **[Breaking]** `agents.memory.MemoryFacade` alias removed | The `MemoryFacade` deprecation alias introduced in v0.3.2 (RFC 0029 Phase 1 facade freeze) is **removed**, honouring its published one-minor-version horizon ("removal in v0.3.3"). Import `agents.memory.MemoryStore` (or `agents.memory.store.MemoryStore`) directly — `MemoryStore` is the same class object the alias pointed at, so only the import name changes. Production call sites migrated in v0.3.2 (RFC 0029 Phase 1 PR 3); the v0.3.3 release-prep migrated the test suite and removed the symbol. |

### 🚀 Features

- *(v033)* RFC 0024 PR 1 — `EventLoop` + `WakeEvent` + `SyncDispatchHandle` (Phase 1) (#406)
- *(v033)* RFC 0024 PR 2 — `autonomy.timers` + `scheduled_wakes` table (Phase 2) (#407)
- *(v033)* RFC 0024 PR 2.1 — wire `ScheduledWakesCache` into persona init (Phase 2) (#408)
- *(v033)* RFC 0024 PR 3a — write-side `salience` + `source_span_id` (Phase 3 prereq) (#409)
- *(v033)* RFC 0024 PR 3b — `SalienceWake` enqueue + threshold + loop-back guard (Phase 3) (#410)
- *(v033)* RFC 0024 PR 4 — channel-message fire-and-forget dispatch + cost-regression CI gate (Phase 4) (#411)
- *(v033)* RFC 0024 PR 5 — `EventLoop` lifecycle hardening (#412)
- *(v033)* RFC 0024 PR 5.1 — review-follow-up cleanups + deferred test-gap fills (Phases 1–4) (#413)

### 📚 Documentation

- *(release)* Post-release follow-up for v0.3.2 (#403)
- *(rfcs)* RFC 0041–0044 drafts + agent-runtime vocabulary roadmap — all `🔨 Draft`, RFC-tracking only (#399)
- *(v033)* Open v0.3.3 master plan — Idle Truly Idle (#404)
- *(v033)* RFC 0024 PR plan — Phases 1–4 (#405)
- *(v033)* RFC 0024 PR 6 — Phases 1–4 closeout (#414)
- *(v033)* Release-prep plan (master-plan Phase 3) (#415)
- Refresh MT-CHAT-003/004 for RFC 0020 interaction-close; file ISSUE-0067/0068 (#417)
- *(v033)* Release-prep PR 1 — manual-test execution report (#416)
- *(v033)* Release-prep PR 2 — docs refresh + release checklist (#418)

### 🧪 Testing

- New manual test [`MT-IDLE-001`](docs/manual-tests/MT-IDLE-001.md) — *idle persona costs nothing*: an `autonomy.timers: []` persona with no inbound traffic shows zero provider/wallet/`agent.wake.*` activity over a 60 s window, then a single inbound event wakes the parked loop. This is the **primary v0.3.3 release gate**.
- The bored-persona [`cost-regression-gate`](.github/workflows/ci.yml) CI job ([`tests/integration/test_bored_persona_cost.py`](tests/integration/test_bored_persona_cost.py)) is a **release-blocker** (a PR trigger on the wake-path file set, not a nightly) — it fails any change that re-introduces an LLM call, `_inject_memory_context`, `LeaseRequest`, or `agent.wake.*` activity from an idle persona.
- New `EventLoop` / `scheduled_wakes` / salience / wake-counter suites cover the event-driven loop, the cache rebuild + `next_fire_at_ms` restore, the `legacy_tick` adapter, and the four `agent.wake.*` counters.
- Full 34-row manual-test surface (1 new gate + 33 carried-forward) re-executed against the `ea2b86d` release-candidate tip; release gate met ([#416](https://github.com/mkhomutov/Persatrix/pull/416)).

[0.3.3]: https://github.com/mkhomutov/Persatrix/compare/v0.3.2...v0.3.3

## [0.3.2] - 2026-05-20

> **Codename:** Cost Gate + Facade Freeze

### Highlights

- **Wallet lease — every LLM call is gated by a server-issued lease before issuing** (RFC 0023, Phases 1–6). A new `WalletService` (proto + Go) issues short-TTL leases against per-agent / per-cause / global budgets; an asynchronous reaper sweeps abandoned leases on a 5 s tick. `WalletClient` in the persona runtime wraps every LLM call with `AcquireLease` → call → `SettleLease`; five origins are now leased (workflow task, chat dispatch, autonomous TICK, sub-agent delegation, channel-message reply). A wallet denial surfaces as `reply_status="error"` on chat (carrying `LeaseDenied.message`) and as `idle_reason=budget_denied` on autonomous TICK — cost is a **structural gate**, not a post-hoc accountant. Closes the v0.2.3 chat-bypass known limitation.
- **Memory facade freeze ahead of the v0.4.0 personal/society split** (RFC 0029 Phase 1). `MemoryFacade` is promoted to the single boundary for all agent-memory access and renamed to `MemoryStore`; `agents.memory.MemoryFacade` survives as a deprecation alias for one minor version. Society-tier methods raise `SocietyBackendUnavailable` in single-agent mode — no Postgres opened, no surprise dependency. A new ruff rule blocks direct `aiosqlite` imports outside `agents/memory/`, and direct `EpisodicMemory` / `RelationshipMemory` construction emits a `DeprecationWarning`. The personal-tier recall-latency regression gate (`tests/perf/personal_tier_latency.py`) ships green from Phase 1 closeout.

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| **[Behaviour change — cost gate]** every LLM call acquires a wallet lease | All five origins (workflow task, chat dispatch, autonomous TICK, sub-agent, channel-message reply) now go through `WalletClient.AcquireLease` before issuing. A wallet outage surfaces as `reply_status="error"` on chat and `idle_reason=budget_denied` on TICK ([RFC 0023 §F](docs/rfcs/0023-llm-call-leasing.md#f-failure-modes)). Closes the v0.2.3 chat-bypass known limitation. |
| New `wallet:` block in `config/optimization.yaml` | Top-level sibling of `cost:`. Keys: `ttl_seconds` (default `60`, = 2× the 30 s per-call timeout — [RFC 0023 OQ §2](docs/rfcs/0023-llm-call-leasing.md#open-questions)), `reaper_interval_seconds` (default `5`), `max_active_leases` (default `16`, per-agent DoS ceiling). Absent block / key falls back to defaults; no operator action required. |
| New `BudgetExceededError` failure surface | `WalletClient` raises it on `LeaseDenied`. The chat path re-surfaces it as a structured `reply_status="error"` payload carrying `LeaseDenied.message` (HTTP 200, not 500); autonomous TICK short-circuits to idle with `idle_reason=budget_denied`. Sub-agent and channel-message origins propagate the same error envelope. |
| `MemoryFacade` → `MemoryStore` rename (Phase 1 facade freeze) | `agents.memory.MemoryFacade` is preserved as a deprecation alias for one minor version (removal in v0.3.3 per [RFC 0029 §Phased Implementation Plan](docs/rfcs/0029-personal-society-storage-split.md#phased-implementation-plan)); new code must import `MemoryStore`. Society-tier methods raise `SocietyBackendUnavailable` — single-agent mode never opens Postgres. |
| New direct-`aiosqlite` import lint rule | `import aiosqlite` outside `agents/memory/` fails CI (new ruff rule). Direct `EpisodicMemory` / `RelationshipMemory` construction emits a `DeprecationWarning`; route through `MemoryStore`. |
| `tiktoken` promoted to a hard runtime dep | Per [RFC 0023 OQ #5](docs/rfcs/0023-llm-call-leasing.md#open-questions), the `accurate-tokens` extra is removed and `tiktoken` is now a required runtime dependency. Builds depending on `[accurate-tokens]` must drop the extra from their install command. The `cl100k_base` encoding is what `LLMClient` already used — no behaviour change. |
| Wallet lease log shape finalised | `lease_acquire` / `lease_settle` / `lease_reap` events carry a stable field set ([RFC 0023 PR 7](https://github.com/mkhomutov/Persatrix/pull/391)); downstream log consumers can pin field names. |
| gRPC server panic-recovery interceptor | A server-side recovery interceptor ([ISSUE-0059](https://github.com/mkhomutov/Persatrix/pull/379)) converts an unhandled handler panic into a structured `Internal` status with a redacted message instead of tearing the connection down. Operator-visible: panics now show up as gRPC errors in client logs, not socket resets. |
| Repo-root `tests/` tree under lint/type gates | `ruff` ([ISSUE-0056](https://github.com/mkhomutov/Persatrix/pull/381)) and `mypy` ([ISSUE-0062](https://github.com/mkhomutov/Persatrix/pull/382)) now cover the top-level `tests/` tree, closing a CI blind spot. Out-of-tree forks carrying patches under `tests/` may see new lint/type findings. |

### 🚀 Features

- *(v032)* RFC 0029 Phase 1 PR 1 — `MemoryStore` facade promotion (#370)
- *(v032)* RFC 0029 Phase 1 PR 2 — lint rule + deprecation warnings (#372)
- *(v032)* RFC 0029 Phase 1 PR 3 — downstream call-site refactor (#373)
- *(v032)* RFC 0029 Phase 1 PR 4 — review follow-ups (#375)
- *(v032)* RFC 0029 Phase 1 PR 5 — closeout + perf gate (#376)
- *(v032)* RFC 0023 PR 1 — proto surface + `WalletService` skeleton (#378)
- *(v032)* RFC 0023 PR 2 — wallet enforcement + TTL reaper (#384)
- *(v032)* RFC 0023 PR 3 — `WalletClient` + workflow-task lease wiring (#385)
- *(v032)* RFC 0023 PR 4 — chat-path wallet lease wiring (closes v0.2.3 bypass) (#387)
- *(v032)* RFC 0023 PR 5 — autonomous TICK + sub-agent lease wiring (#388)
- *(v032)* RFC 0023 PR 6 — channel-message origin lease wiring (#389)
- *(v032)* RFC 0023 PR 7 — review follow-ups (finalize log shape) (#391)

### 🐛 Bug Fixes

- *(tests)* Isolate fact-store audit redactor-warning test from global log state (#374)
- *(v032)* ISSUE-0059 — add gRPC server panic-recovery interceptor (#379)
- *(v032)* ISSUE-0055 — drain the RETURNING cursor racing close-path COMMIT (#380)
- *(v032)* ISSUE-0056 — ruff-gate the repo-root tests/ tree (#381)
- *(v032)* ISSUE-0062 — mypy-gate the repo-root tests/ tree (#382)
- *(v032)* ISSUE-0064 — persona-as-sub-agent attribution gap (#390)
- *(v032)* ISSUE-0065 — publish chat-error reply on channel under wallet denial (#395)
- *(v032)* ISSUE-0066 — publish chat-error reply on channel under wallet lease-cap / rate-limit (#396)
- *(v032)* ISSUE-0066 reopen — handle `AioRpcError(RESOURCE_EXHAUSTED)` in `action_loop` (#398)

### 📚 Documentation

- *(release)* Post-release follow-up for v0.3.1 (#366)
- *(v032)* Open v0.3.2 master plan — Cost Gate + Facade Freeze (#367)
- *(rfcs)* RFC 0040 — agent–orchestrator transport unification (#368)
- *(v032)* RFC 0023 + RFC 0029 PR plans (Phase 1 combined scaffold) (#369)
- *(v032)* Resolve RFC 0023 open questions + v0.3.2 tracking hygiene (#371)
- Note full-suite runtime + long-command guidance in CLAUDE.md (#377)
- *(rfcs)* RFC 0031 Phase 2 PR plan — recall filtering + dementia-test bridge (#383)
- *(v032)* RFC 0023 PR 8 — full-RFC closeout (#392)
- *(v032)* Release-prep plan (master-plan Phase 3) (#393)
- *(v032)* Release-prep PR 1 — manual test execution report (release gate not met) (#394)
- *(v032)* Release-prep PR 1 — manual test re-execution report (release gate met) (#397)
- *(v032)* Release-prep PR 2 — docs refresh + release checklist (#400)

### 🧪 Testing

- New manual tests [`MT-COST-003`](docs/manual-tests/MT-COST-003.md) (chat budget exceed surfaces as `reply_status="error"`, RFC 0023 Phase 4) and [`MT-COST-004`](docs/manual-tests/MT-COST-004.md) (TICK budget exhaustion records idle with `idle_reason=budget_denied` and no provider spend, RFC 0023 Phase 5).
- Wallet acquire+settle p99 loopback measurement added to the v0.3.2 execution report as an informational target (RFC 0023 §Goal #6 — ≤ 5 ms p99). Regression is a release-review finding, not a build gate.
- Personal-tier recall-latency regression gate (`tests/perf/personal_tier_latency.py`) shipped from RFC 0029 PR 5 ([#376](https://github.com/mkhomutov/Persatrix/pull/376)) to lock in the facade-freeze baseline ahead of the v0.4.0 personal/society split.
- Full 32-test manual-test surface (30 v0.3.1 carry-forward + 2 new MTs) re-executed against the v0.3.2 release-candidate tip; release gate met ([#397](https://github.com/mkhomutov/Persatrix/pull/397)).

### 🏗️ Build

- *(deps)* Bump openssl from 0.10.79 to 0.10.80 in /cli (#386)

[0.3.2]: https://github.com/mkhomutov/Persatrix/compare/v0.3.1...v0.3.2

## [0.3.1] - 2026-05-17

> **Codename:** Memory Quality

### Highlights

- **Declarative facts tier — the persona remembers stated facts about you across interactions** (RFC 0026). The persona extracts stated `(subject, predicate, object)` facts at interaction close — names, preferences, commitments — and persists them to a new `facts` table indexed by subject, separate from the prose episode summary. At recall time `MemoryFacade` injects them into the persona prompt through a dedicated `facts_context` section, so a fact stated days ago survives idle-gap closure and resurfaces by subject rather than by keyword overlap. `MT-MEMORY-005` (the dementia test) is promoted from a v0.3.0 baseline measurement to a pass/fail acceptance gate.
- **Persona conversational working memory for DM channels** (RFC 0034 Phase 1). A persona now follows the conversation it is currently having: the persona LLM `messages` array carries the reconstructed in-progress DM transcript instead of a single isolated message, so the persona recalls its own prior question and resolves referential follow-ups within the session. DM channels only in v0.3.1 — group channels keep the single-message behaviour (Phase 2). Operator escape hatch: `conversation_window.enabled: false`.
- **Per-session storage namespacing — write-path plumbing** (RFC 0031 Phase 1). A new `sessions` table plus `session_id` columns on channels, messages, episodes, and relationships tag every storage write with the `PERSATRIX_SESSION_ID` env var, read at orchestrator and persona-runtime boot. Phase 1 is write-path only — recall does not yet filter by session, and `make reset` remains the cross-run isolation path. The operator CLI and recall filtering land in later v0.3.x phases.

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| **[Breaking]** chat-session wire-field rename | `ChatRequest.session_id` / `ChatResponse.session_id` JSON keys renamed to `chat_session_id` ([RFC 0031 OQ #8](docs/rfcs/0031-per-session-namespacing-channels.md#open-questions)) to disambiguate from RFC 0031's operator-namespace `session_id`. Proto field numbers are preserved; JSON / proto-text consumers must migrate. The Rust CLI and the in-tree REST flow are already updated. |
| `channels.db` schema bump (v2→v3) | Adds the `sessions` table and `session_id` columns on channel/message rows. Forward-only migration; existing rows pick up the `legacy` session carve-out. |
| `memory.db` schema bump (v6→v8) | Two forward-only migrations: v7 adds `session_id` on `episodes` and `relationships`; v8 creates the RFC 0026 `facts` table (which carries `session_id` from creation). Existing rows pick up the `legacy` carve-out. |
| New `PERSATRIX_SESSION_ID` env var | Read at orchestrator + persona-runtime boot, stamped on every storage write. Unset ⇒ the `legacy` carve-out with an INFO boot line. Set the **same value in both processes** to keep a run coherent. Phase 1 is write-path only — recall does not yet filter by session. |
| New declarative facts tier (RFC 0026) | The persona extracts stated `(subject, predicate, object)` facts at interaction close and injects them into the persona prompt via a new `facts_context` section. Out-of-tree prompt evaluators will see a new tier in the system prompt. `memory.facts.enabled: false` disables fact recall/injection per-agent; the close-path extractor keeps writing facts regardless. |
| New `conversation_window` config block (RFC 0034) | A top-level `conversation_window` block lands in `config/agents.yaml` + `config/optimization.yaml`, validated by the new `schemas/optimization.schema.json`. The persona LLM `messages` array now carries the in-progress DM transcript instead of a single isolated message. Operator escape hatch: `conversation_window.enabled: false`. DM channels only in v0.3.1. |
| `make reset` deprecation breadcrumb | `make reset` remains the supported cross-run storage-isolation path. RFC 0031 Phase 3's `persatrix session …` CLI (`persatrix session new --activate`) will succeed it for run isolation in a later v0.3.x patch; `make reset` then becomes the deprecated nuclear option for clearing all volumes across all sessions. |

### 🚀 Features

- *(v031)* RFC 0031 PR 1 — rename chat session_id → chat_session_id (#333)
- *(v031)* RFC 0031 PR 2 — sessions table + Go session_id columns + PERSATRIX_SESSION_ID (#335)
- *(v031)* RFC 0031 PR 3 — Python session_id columns + persona-runtime PERSATRIX_SESSION_ID (#336)
- *(v031)* RFC 0031 PR 4 — Phase 1 review follow-ups (#337)
- *(v031)* RFC 0026 PR 1 — facts schema + FactStore + erasure primitive (#339)
- *(v031)* RFC 0026 PR 2 — extractor + predicate allowlist + audit (#340)
- *(v031)* RFC 0026 PR 3 — FactStore.recall + MemoryBudget tier slot + config (#341)
- *(v031)* RFC 0026 PR 4 — reinforcement + retraction + tier provenance + MT update (#342)
- *(v031)* RFC 0026 PR 5a — symmetric latest-asserted-wins + source_interaction_id nullability (#344)
- *(v031)* RFC 0026 PR 5b — envelope parse-failure observability (#345)
- *(v031)* RFC 0026 PR 5c — PR 3 review storage/render defensive fixes (#346)
- *(v031)* RFC 0026 PR 5d — PR 3 review tests + counter polish (#347)
- *(v031)* RFC 0026 PR 5e — PR 4 review audit/chunking/edge cases (#348)
- *(v031)* RFC 0034 PR 1 — channel-history fetcher behind Protocol (#351)
- *(v031)* RFC 0034 PR 2 — conversation-window module + config/schema (#352)
- *(v031)* RFC 0034 PR 3 — wire conversation window + DM itest (#356)
- *(v031)* RFC 0034 PR 4 — review follow-ups (history-fetcher hardening) (#357)

### 🐛 Bug Fixes

- *(v031)* ISSUE-0054 — RFC 0026 facts tier extracted no facts at interaction close (markdown-fence strip + extractor message-content input + summarise/extract output-token cap 256 → 1024) (#361)
- *(v031)* Single-turn interactions extract facts (MT-MEMORY-005 F-6) (#362)

### 📚 Documentation

- *(release)* Post-release follow-up for v0.3.0 (#330)
- *(v0.3.1)* Re-sequence v0.3.x and open v0.3.1 master plan (#331)
- *(v0.3.1)* RFC 0026 + RFC 0031 PR plans (Phase 1 combined scaffold) (#332)
- *(v031)* RFC 0031 PR 5 — Phase 1 closeout (#338)
- *(rfcs)* RFC 0033 — provider-agnostic model alias layer (#343)
- *(rfcs)* RFC 0032 stub — channel interaction layer unification (#334)
- *(v0.3.1)* Absorb RFC 0034 - persona conversational working memory (#349)
- *(rfc-0034)* Resolve open questions and add Phase 1 PR plan (#350)
- *(rfcs)* RFC 0035 + RFC 0036 — membership ledger and persona message recall (#353)
- *(rfcs)* RFC 0012 + 0037 + 0038 — confidentiality, authority & concurrent-context awareness (#354)
- *(rfcs)* RFC 0039 — user accounts & authentication foundation (#355)
- *(v031)* RFC 0034 PR 5 — Phase 1 closeout (#358)
- *(v031)* RFC 0026 PR 6 — RFC close (#359)
- *(v031)* V0.3.1 release-prep plan (master-plan Phase 3) (#360)
- *(v031)* V0.3.1 release-prep PR 1 — manual test execution report (#361)
- *(v031)* V0.3.1 release-prep PR 2 — docs refresh + release checklist (#363)

### 🧪 Testing

- New manual tests `MT-SESSION-001` (`PERSATRIX_SESSION_ID` cross-process write contract, RFC 0031 Phase 1) and `MT-PERSONA-CONVERSATION-001` (persona conversational continuity over a DM channel, RFC 0034 Phase 1)
- `MT-MEMORY-005` (the dementia test) promoted from a v0.3.0 baseline measurement to a pass/fail acceptance gate — Legs 1, 2, 5 are release blockers
- Facts-tier, conversation-window, and `session_id` integration suites added across the RFC 0026 / 0034 / 0031 PR clusters

[0.3.1]: https://github.com/mkhomutov/Persatrix/compare/v0.3.0...v0.3.1

## [0.3.0] - 2026-05-12

> **Codename:** Agent Conversations

### Highlights

- **Internal channels — agents and humans share a typed conversation surface.** Group channels, DMs, threads, and the chat-as-DM façade ship as the v0.3.0 user-facing promise (RFC 0011 internal scope). `POST /api/v1/channels`, `POST /api/v1/channels/{id}/messages`, `GET /api/v1/channels/{id}/messages/{msg_id}/thread`, plus a publish → fanout → response-gate → LLM action → publish-back loop wired end-to-end across the Go orchestrator, the Python persona runtime, and a new `persatrix channel` CLI subcommand (`list` / `join` / `send` / `reply` / `history` / `watch`). The legacy `SendChatMessage` gRPC path is dead code — every `/chat` REST call now flows through the channels publish-and-await façade ([RFC 0011 amendment](docs/rfcs/0011-amendment-chat-as-dm.md)).
- **Interaction-bounded episodic memory** (RFC 0020). A multi-turn dialogue is now one episode (open → multi-turn → close → summarize), not one per inbound event. The close path is two-phase + async — synchronous `[summary pending]` sentinel inside the per-agent lock, then a background LLM summariser updates the row — so a follow-up message no longer queues head-of-line behind the summariser. Janitor cleanup wired into `on_tick` recovers crash-stuck `[summary pending]` rows.
- **Persona temporal awareness, Phase 1** (RFC 0021). Persona prompts now emit a `now-anchor` line and render episode + relationship timestamps as relative time ("yesterday", "3 days ago") instead of raw epoch seconds. Structured commitment tracking + scheduled callbacks are deferred to v0.4.0 (Phases 2–4).
- **Memory facade + per-step context-budget allocation** (RFC 0008). `MemoryFacade` becomes the single boundary between persona-runtime + memory tiers (working / relationship / facts / notes / episodic); `attachContextPackage` allocates a typed budget per workflow step and routes it through cross-language wire shape pinned by [`tests/fixtures/context_package_v1.json`](tests/fixtures/context_package_v1.json). Procedural tier ships read-time exponential confidence decay (default 69-day half-life) + revalidation; shared-memory pools with deny-by-default ACL + provenance ship behind a new `shared_memory_pools:` config block.
- **Security Phases 1–2** (RFC 0009). Audit logger with checksum-chained tamper evidence (JSONL append-only sink, per-event fsync for security-class events, three-state startup recovery), `SecretRedactor` with five default patterns + cycle-safe reflective walk, per-agent sliding-window REST + gRPC rate limiter with circuit-breaker quarantine, `<external_data>` envelope wrapping at the LLM-content boundary, Go↔Python sanitizer-pattern parity enforced at build time. Sandbox isolation + token auth (Phases 3–4) deferred to v0.4.0.
- **Externally inspectable persona prompt sections** (RFC 0022). Every persona prompt fragment lives under [`prompts/runtime/persona/sections/`](prompts/runtime/persona/sections/) as a separate markdown file — assembly order pinned by golden tests, forks and out-of-tree tooling that override prompt assembly can pin against this directory shape. New safety snippets `reply-discretion.md` + `conversational-pacing.md` shape the persona's channel-reply behaviour from the prompt layer rather than the executor.

### Upgrade Notes

| Notable change | Detail |
|----------------|--------|
| Channel-event enum hard-rename | `EventType.MESSAGE_RECEIVED` → `CHANNEL_MESSAGE` and `ActionType.SEND_MESSAGE` → `SEND_CHANNEL_MESSAGE` across all Python producers (chat ingest, persona-runtime response gate, dispatch executor, action validators, prompt assembly, state persistence, memory routing). v0.2 enum aliases dropped. `ActionExecutor` result dict now carries `"action_type": "send_channel_message"`. Out-of-tree consumers must update event-type filters and result-dict consumers. |
| Chat REST endpoint migrated to channels | `POST /api/v1/agents/{id}/chat` now goes through the channels publish-and-await façade ([RFC 0011 amendment](docs/rfcs/0011-amendment-chat-as-dm.md)). JSON contract on `chatRequest` / `chatResponse` is preserved — Rust CLI and existing REST clients are unaffected — but the legacy `SendChatMessage` gRPC path is dead code (cleanup tracked in ISSUE-0035). |
| New `SECURITY_RATE_LIMIT_*` env vars | RFC 0009 PR 2 ships a per-agent sliding-window rate limiter with circuit breaker. `SECURITY_RATE_LIMIT_ENABLED`, `SECURITY_RATE_LIMIT_CALLS`, `SECURITY_RATE_LIMIT_WINDOW_SECONDS`, `SECURITY_RATE_LIMIT_MAX_AGENTS` configure the limiter at startup (defaults: enabled, 60 calls / 60-s window). Opting out emits a one-shot `rate_limit.disabled` audit event so the choice is visible in the audit log. Operators see HTTP 429 + `Retry-After` on REST and `ResourceExhausted` / `PermissionDenied` on gRPC after threshold violations; `POST /api/v1/agents/{id}/unquarantine` clears a quarantined agent (call it with a non-`anonymous` `X-Agent-ID` header so the operator's own request is not rate-limited as `anonymous`). |
| `<external_data>` envelope wrapping | RFC 0009 PR 3 wraps `http_request` / `file_read` tool results in an `<external_data>…</external_data>` envelope at the LLM-content boundary, with close/open-tag escaping (`_EXTERNAL_DATA_TAG_RE`) so untrusted content cannot break out. Out-of-tree LLM evaluators or post-processors that grepped on raw tool-output strings must move to the wrapped form. The unconditional `external-data-handling` prompt fragment teaches the model the contract. |
| Channels REST surface is unauthenticated | A startup `WARN` notice fires whenever the channels subsystem is enabled. `sender_id` is body-trusted in v0.3.0 — firewall the port or front with an authenticating reverse proxy until [RFC 0009 Phase 4](docs/rfcs/0009-security-sandboxing.md) lands in v0.4.0. The notice is intentionally not suppressible from config. |
| Persona prompt-section directory is public surface | Every persona prompt fragment now lives under [`prompts/runtime/persona/sections/`](prompts/runtime/persona/sections/) ([RFC 0022](docs/rfcs/0022-persona-prompt-section-templating.md)). Forks and out-of-tree tooling that override prompt assembly should pin against this directory shape. |
| Now-anchor in persona prompts | Persona prompts emit a `now-anchor` line and render episode + relationship timestamps as relative time ("yesterday", "3 days ago") instead of raw epoch seconds (RFC 0021 Phase 1). Out-of-tree prompt evaluators that key on absolute timestamps must move to the rendered form, or read the underlying epoch from the episodic store directly. |
| Episodic write cadence changed | RFC 0020 collapses multi-turn dialogues to **one** episodic entry per interaction (open → multi-turn → close → summarize) instead of one entry per inbound event. Out-of-tree memory-inspection tools that counted episodes-per-message will see the count drop sharply on chatty channels — this is by design. The `interaction_id` + scope tag on each episode is the new lookup key. |
| `relationships.interaction_count` unit changed | `interaction_count` and `auto_reflect_after` are now per-closed-interaction, not per-message. A 10-message DM session now bumps `interaction_count` by 1 (previously by 10). Operators with bespoke trust thresholds calibrated against the per-message scale should consult the Migration Notes in [docs/rfcs/0020-interaction-lifecycle.md](docs/rfcs/0020-interaction-lifecycle.md). |
| `memory.min_score` schema default changed | `null` → `0.20` (RFC 0008 PR 2a). Operators with `memory.enabled: true` who did not previously set `memory.min_score` will see strictly fewer recall results — low-score entries are no longer concatenated into the system prompt. Restore the pre-PR-221 behaviour by setting `memory.min_score: null` in [`config/agents.yaml`](config/agents.yaml). |
| `MemoryFacade.store_procedure` key validation | Validates `key` against `^[A-Za-z0-9._-]+$` (max 256 chars) and raises `ValueError` on non-conforming keys. Callers persisting procedural keys with spaces, slashes, percent-signs, non-ASCII characters, or newlines must rename them before upgrading. |
| `pytest-timeout` test dependency added | Transitive dev dep added per ISSUE-0024 to stop the Python unit-suite from hanging on the full-suite invocation. Genuinely MIT-licensed; the `check-licenses-python` Makefile target carries an `--exception pytest-timeout` with justification (pip-licenses `--from=mixed` concatenates the legacy `License :: DFSG approved` Trove classifier producing a token the strict allow-list doesn't accept). |

### 🚀 Features

- *(memory)* RFC 0020 PR 1 - InteractionTracker + episodes schema v5 (#214)
- *(memory)* RFC 0020 PR 2 — route TICK + tool-only events through InteractionTracker (#215)
- *(memory)* RFC 0020 PR 3 - multi-turn aggregation for human-chat + DM (#216)
- *(rfc0008)* PR 1 - context budget allocator + packaging foundation (#218)
- *(rfc0008)* PR 1b — context metrics emission + remaining-budget persistence (#219)
- *(rfc0008)* PR 2 — MemoryFacade for task agents (#220)
- *(rfc0008)* PR 2a - episodic-tier eviction + PR 2 follow-up findings (#221)
- *(rfc0008)* PR 3 - delegation contract + merge engine (#222)
- *(rfc0008)* PR 4 - shared pool ACL + provenance (#223)
- *(rfc0008)* PR 3a - delegation metrics + PR 3 follow-up findings (#224)
- *(rfc0008)* PR 5 - confidence decay + procedural revalidation (#225)
- *(rfc0008)* PR 6a - Go scheduler hygiene + sampler bookkeeping (#227)
- *(rfc0008)* PR 6b - Python procedural memory + log-safety cleanup (#228)
- *(rfc0020)* PR 4 — summarization-on-close + janitor + record_interaction move (#229)
- *(rfc0011)* PR 1 - channel store + SQLite migration + schema rewrite (#231)
- *(rfc0009)* PR 1 - AuditLogger + SecretRedactor (security package + unit tests) (#233)
- *(rfc0009)* PR 1b — audit wiring + default redactor + chmod self-heal (#234)
- *(rfc0009)* PR 1c — RedactStruct hardening + audit metrics (#236)
- Externalize hardcoded literals to prompt snippets and config (#239)
- *(rfc0009)* PR 2 - RateLimiter + CircuitBreaker + REST/gRPC middleware (#244)
- *(rfc0011)* PR 2 — channels REST + router + config reconciliation (#245)
- *(rfc0011)* PR 3 — proto + RPC for ChannelMessageEvent (#246)
- *(rfc0011)* PR 4a-i — ReceiveChannelMessage real handler + additive enums (#248)
- *(rfc0011)* PR 4a-ii-α — hard rename CHANNEL_MESSAGE/SEND_CHANNEL_MESSAGE + SF-3 mentions validation (#249)
- *(rfc0011)* PR 4a-ii-β-1 — real Go gRPC MessageDispatcher + Python REST publish rewire (#250)
- *(rfc0011)* PR 4a-ii-β-2 — chat-as-DM rewrite (Go-side waiter + PublishAndAwait) (#251)
- *(rfc0011)* PR 4b — channels response gate + DELETE endpoints (#252)
- *(rfc0009)* PR 3 — InputSanitizer + ContextItem + external_data envelope (#253)
- *(rfc0021p1)* PR 1 — Clock seam + temporal rendering pure functions (#256)
- *(rfc0021p1)* PR 2 — now-anchor + episode/relationship recency rendering (#260)
- *(rfc0021p1)* PR 3 — review follow-ups + RFC Phase-1 close (#261)
- *(rfc0020)* PR 5 — per-channel scoping + closing-row recall filter (#262)
- *(rfc0011)* PR 5 — channel ingest sanitization + gate-suppress memory (#263)
- *(rfc0011)* PR 5 follow-up — channel-history tier in MemoryBudget (#264)
- *(rfc0011)* PR 5 follow-up — on-startup catch-up fetch (OQ #8) (#265)
- *(rfc0020)* PR 6 slice 1 — Phase-2/janitor race + PR 4 review follow-ups (#266)
- *(channels)* Close ISSUE-0015 — paginate ListChannels via keyset cursor (#280)
- *(channels)* ISSUE-0032 — emit channel.dispatch OTel span (Go side) (#286)
- *(agents)* Close ISSUE-0032 — emit channel.publish OTel span (Python side) (#287)
- *(rfc0020)* PR 6 slice 2 — typed CloseReason + table-driven _emit_closed dispatch (#296)
- *(rfc0011)* PR 6 — Rust CLI channel subcommands (list/join/send/reply/history/watch) (#302)
- *(rfc0009)* PR 4 — review follow-ups + Phases 1-2 close (#306)
- *(v030)* Demo personas + planning channel + walkthrough guide (#316)
- *(persona)* Reply-discretion + conversational-pacing prompt snippets (#327)

### 🐛 Bug Fixes

- *(agent)* Include prompts in image and configure audit log path (#235)
- *(security)* Dedupe ContextSource validation + codegen enum parity (#254) (#255)
- *(security)* Close ISSUE-0001 — CircuitBreaker rejects Window/Count <= 0; add Disabled flag (#270)
- *(security)* Close ISSUE-0007 — propagate request ctx through RateLimiter/CircuitBreaker audit emits (#272)
- *(channels)* Close ISSUE-0034 — demote chat-DM user to RespondNever (#276)
- *(agents)* Close ISSUE-0027 — symmetrize SEND_CHANNEL_MESSAGE result dicts (#277)
- *(docker)* Close ISSUE-0046 + ISSUE-0047 — get compose stack functional for v0.3.0 (#279)
- *(agents)* Close ISSUE-0026 — sticky-disable HTTPChannelPublisher on first 503 (#281)
- *(agents)* Close ISSUE-0048 — synthesise SEND_CHANNEL_MESSAGE for plain-text persona replies (#282)
- *(scripts)* Close ISSUE-0036 — switch doc_links collector to `git ls-files` (#284)
- *(channels)* Close ISSUE-0049 — buildDSN merges caller query params instead of double-? concatenation (#294)
- *(channels)* Close ISSUE-0050 — soft byte cap on msg.Content at the SQLite store boundary (#295)
- *(v030)* Channel cascade-depth wire propagation — amendment + schemas (PR 1) (#318)
- *(v030)* Channel cascade-depth Go orchestrator enforcement (PR 2) (#319)
- *(v030)* Channel cascade-depth Python round-trip (PR 3) (#321)
- *(v030)* Channel cascade-depth cross-process integration pin (PR 4) (#322)
- *(v030)* Channel persona impersonation — grounding clause (PR 5) (#323)
- *(v030)* Channel state-reset Make target + operator-guide notes (PR 6) (#324)

### 🔒 Security

- *(server)* Close ISSUE-0004 — hash bearer token before constant-time compare (#275)
- *(ratelimit)* Close ISSUE-0005 — emit rate_limit.reset audit event from RateLimiter.Reset (#285)

### ⚡ Performance

- *(security)* Close ISSUE-0003 — RateLimiter.evictOlderThan in-place compaction (#274)
- *(channels)* Close ISSUE-0014 — bounded-concurrency fanout in ChannelRouter (#283)

### 🔧 Refactoring

- *(tests)* Split test_persona_runtime.py into focused modules (#195)
- *(tests)* Split test_episodic_memory.py into focused modules (#196)
- *(tests)* Split test_event_dispatch_tick.py into focused modules (#197)
- *(tests)* Split scheduler_test.go into focused modules (#198)
- *(tests)* Split server_test.go into focused modules (#199)
- *(tests)* Split executor_test.go into focused modules (#200)
- *(tests)* Split test_validate.py and planner_test.go into focused modules (#201)
- *(tests)* Split state_test.go and test_server.py into focused modules (#202)
- *(tests)* Split test_chat_servicer.py, encoder_test.go, cost_test.go into focused modules (#203) (#203)
- *(tests)* Split oversized Python test files to comply with 500-line policy (#204)
- *(prompts)* Externalize task-agent instructions into prompts/runtime/ (#210)
- *(prompts)* Externalize safety snippets into prompts/runtime/safety/ (#211)
- *(prompts)* Externalize behavior-dimension descriptions into prompts/runtime/persona/sections/ (#212)
- *(prompts)* Externalize persona section composer (RFC 0022, PR C) (#213)
- *(orchestrator)* Close ISSUE-0008 — extract startup helpers, drop main.go below 500 lines (#292)
- *(memory)* Drop file-size grandfather entries — split memory_context, episodic + verify facade (#293)
- *(rfc0020)* PR 6 slice 3 — migration no-op cleanup + autouse metrics fixture (#297)
- *(rfc0020)* PR 6 slice 4 — PR-2 review #6/#7/#9/#10/#11 + episode-routing mixin extraction (#298)
- *(rfc0020)* PR 6 slice 5 — clock seam + cross-scope idle-flush attribution (#299)
- *(rfc0020)* PR 6 slice 6 — inline MaxTurns cap + multi-turn close-path coverage (#300)
- *(rfc0020)* PR 6 slice 7 — tighten _llm_client to LLMClient + drop dead silent-drop branches (#301)

### 📚 Documentation

- *(release)* Post-release follow-up for v0.2.3 (#192)
- Apply Priority 1 + 2.2 + 3.2 cleanup recommendations (#194)
- *(rfcs)* V0.3.0 planning kickoff — RFC 0011 (Channels) and roadmap corrections (#205)
- *(planning)* Add v0.3.0 master plan (#206)
- *(planning)* Scaffold the six v0.3.0 RFC PR plans (#207)
- *(ai)* Enforce brevity policy and trim prompt footprint (#208)
- *(ai)* Add canonical AI glossary and enforce it in assistant instructions (#209)
- *(rfc0008)* Flesh out RFC 0008 PR plan from scaffold (#217)
- *(rfc0008)* Triage accumulated PR 1-5 follow-ups before RFC close (#226)
- *(rfc0011)* Flesh out RFC 0011 PR plan from scaffold (#230)
- *(rfc0009)* Resolve in-scope open questions and flesh out PR plan (#232)
- Memory quality roadmap (assess draft RFCs 0023-0025, propose alternatives) (#237)
- *(instructions)* Adopt TDD from v0.3.0 onward (#240)
- Introduce docs/issues/ finding tracker with make issues target (#241)
- *(memory-quality)* Integrate roadmap into v0.3.x and v0.4.0 plans (#238)
- *(rfc)* Propose RFC 0028 agent decision policy engine (#242)
- *(storage)* Propose storage architecture roadmap discussion doc (#243)
- *(rfc)* Amend RFC 0011 with chat-as-DM unification (RFC 0016 reconciliation) (#247)
- *(rfc)* V0.3.0 readiness hygiene — status flips, stale Decision/Next Steps, OQ resolutions (#258)
- *(scope)* Retarget RFC 0007 from v0.3.0 to v0.4.0 (#259)
- *(security)* Close ISSUE-0002 — align GRPCRateLimitInterceptor godoc with grpc.SetHeader + add client-side contract test (#273)
- *(proto)* Close ISSUE-0019 + ISSUE-0022 — TaskAck reuse policy + timestamp format cross-reference (#291)
- *(rfc0011)* PR 7 — Phase 4b human participation MTs + channels guide + diagram (#303)
- *(rfc0011)* PR 8 — internal-scope close (NTH dispatch + status flips) (#304)
- *(rfc0020)* PR 7 — RFC close (status flips for v0.3.0 scope) (#305)
- *(rfc0023)* Introduce LLM call leasing RFC (#307)
- *(rfc0024)* Propose event-driven agent scheduling (#308)
- *(rfc0029)* Propose personal/society storage split (#309)
- *(rfc0023)* Review follow-ups — 3 correctness fixes + 8 clarifications (#310)
- *(v0.3.x)* Sequence RFCs 0023/0024/0026/0029 across v0.3.1-v0.3.3 (#311)
- *(v030)* Release-prep plan + walk back RFC 0008 OQ #12 calibration-window gate (#312)
- *(rfc0008)* PR 6 — review follow-ups absorbed + RFC close (#313)
- *(v030)* Release-prep PR 2 — README + ROADMAP + guide callouts + diagram refresh + release checklist (#315)
- *(v030)* PR plan for v0.3.0 channel test findings (#317)
- *(rfc0030)* Propose multi-agent conversation governance (#320)
- *(rfcs)* RFC 0031 — per-session namespacing for channels + persona memory (#325)
- *(rfcs)* YAML front-matter + auto-generated INDEX.md (#326)

### 🧪 Testing

- *(proto)* Close ISSUE-0021 — pin ChannelMessageEvent + TaskAck wire shape (#278)
- *(channels)* Close ISSUE-0025 — full-chain REST→fanout→gRPC integration test (#290)
- *(v030)* Release-prep PR 1 — manual test execution report + 3 release-prep regression fixes (#314)

### 🏗️ Build

- *(proto)* Close ISSUE-0017 — auto-generate agents/generated/*.pyi via mypy-protobuf (#288)
- *(proto)* Close ISSUE-0023 — gate proto/ source-of-truth (Python freshness + orphan detection) (#289)

### 📦 Miscellaneous

- *(deps)* Bump rustls-webpki from 0.103.12 to 0.103.13 in /cli (#193)
- *(deps)* Bump openssl from 0.10.78 to 0.10.79 in /cli (#257)
- *(rfc0021p1)* Close #261 review follow-ups (ISSUE-0042/0043/0044/0045) (#267)
- *(rfc0011)* Close ISSUE-0028/0030/0031 — channels dispatcher observability + test gaps (#268)
- *(rfc0011)* Close ISSUE-0010/0011/0013 — PR #245 review follow-ups (#269)
- *(rfc0009)* Close ISSUE-0006 — WARN on invalid SECURITY_RATE_LIMIT_* env values (#271)

[0.3.0]: https://github.com/mkhomutov/Persatrix/compare/v0.2.3...v0.3.0

## [0.2.3] - 2026-04-24

> **Codename:** Observability Foundation

### Highlights

- Structured JSON logs on a versioned schema across the Go orchestrator, Python
  agents, and the `persatrix` CLI — every entry carries `schema_version`,
  `service.kind`, `service.instance`, `source`, and the four reserved correlation
  IDs (`execution_id`, `step_id`, `agent_id`, `workflow_id`) (RFC 0018).
- Distributed OpenTelemetry traces end-to-end from REST handler to LLM call, with
  Gen-AI semantic conventions on every `agent.llm.call` span and Span Links for
  cross-tree causality (RFC 0019).
- OTLP metrics (counters + histograms) on both Go and Python runtimes, with
  histogram exemplars that point Prometheus click-throughs back to the
  originating Jaeger trace.
- W3C Baggage + the four reserved correlation IDs propagate across the Go →
  Python gRPC boundary via a dedicated `internal/observability/grpcmeta`
  surface and a Python `LoggingMetadataInterceptor`.
- Tail-sampling OpenTelemetry Collector pipeline
  (`config/observability/otel-collector.yaml`), with dev-stack `otel-collector`
  + `prometheus` + `loki` wired into `docker-compose.yaml`.
- `persatrix logs <execution_id>` CLI with REST query, `--follow` SSE stream,
  disk-store durability, filter flags, and `jq`-friendly JSON output.

### Upgrade Notes

- **Jaeger OTLP host ports unpublished** (RFC 0019 PR 4): the `jaeger`
  service in `docker-compose.yaml` no longer publishes `4317`/`4318` on
  the host. The OTEL Collector now owns the host-facing OTLP ingress
  (also on `4317`/`4318`) and forwards traces to Jaeger over the
  internal compose network. Dev tooling that previously sent OTLP
  directly to `localhost:4317` against Jaeger must either be retargeted
  at the Collector (no other change required — the host ports are the
  same) or pin its Jaeger endpoint to the in-network `jaeger:4317`. See
  [`docs/observability.md` § 11.1](docs/observability.md).

- **Python OTLP exporter transport changed** (`grpc` → `http`):
  `opentelemetry-exporter-otlp-proto-grpc` is replaced with
  `opentelemetry-exporter-otlp-proto-http`. Collector configs pointing the
  Python exporter at `:4317` must switch to `:4318`. The Go exporter was
  already HTTP; both runtimes now use the same endpoint.
- **Go package rename** `internal/telemetry` → `internal/observability`:
  internal only; forks importing it directly must update import paths.
- **Go zap log field keys renamed** to the RFC 0018 schema (`docs/observability.md`).
  The **reserved correlation IDs** (`execution_id`, `agent_id`, `workflow_id`,
  `step_id`) are renamed at every Go call site, with the encoder's
  `legacyRenames` map as a defence-in-depth backstop. Site-local attributes
  (`inputTokens` / `outputTokens`, `retryCount`, `wallTimeMs`, `estimatedCost`,
  `serviceName`, …) **remain camelCase on the wire** pending a future PR that
  nests them under the schema's `attributes` slot. Downstream consumers (log
  shippers, `jq` queries, dashboards) filtering on the renamed correlation IDs
  must switch to the new keys.

  | Old (legacy) | New (RFC 0018 § B) |
  |--------------|--------------------|
  | `runID` | `execution_id` |
  | `executionID` | `execution_id` |
  | `agentID` | `agent_id` |
  | `workflowID` | `workflow_id` |
  | `stepID` | `step_id` |

  Every Go log line now also carries the RFC 0018 required-field group:
  `schema_version: "1"`, `service.kind: "orchestrator"`,
  `service.instance: <hostname>`, and a `source: {file, line, function}`
  object from `zap.AddCaller`. Custom forks constructing their own zap logger
  should switch to
  [`internal/observability/zapenc.NewEncoder`](internal/observability/zapenc/encoder.go)
  for schema-conformant output.

- **`PERSATRIX_LOG_FORMAT=pretty`** selects a human-readable console encoder
  for local debugging. Default (unset or `json`) emits the RFC 0018 wire
  format. Pretty mode is **not** consumed by the `persatrix logs` endpoint —
  leave unset in production.

- **`PERSATRIX_SERVICE_INSTANCE`** overrides the orchestrator's
  `service.instance` log field (defaults to `os.Hostname()`). Useful in
  containerised deployments where the hostname is an ephemeral pod ID.

- **`PERSATRIX_TRACE_TOOL_PAYLOADS`** controls `agent.tool.execute` span
  detail. Defaults to `none` (only `tool.name`). `metadata` adds
  `tool.arguments.<arg>.type`; `full` emits redacted argument values via
  the same `Redactor` Protocol used for log redaction. Use `full` only
  with a configured redactor — the default `NoopRedactor` echoes values
  verbatim and may capture secrets.

### 🚀 Features

- *(logs)* Schema doc + Python structlog chain + redactor surface (RFC 0018 PR 1/7) (#164)
- *(logs)* Go zap rename + pretty + redactor wired + source (RFC 0018 PR 2/7) (#165)
- *(logs)* Cross-process correlation IDs + OTEL trace IDs on logs (RFC 0018 PR 3/7) (#168)
- *(logs)* `log_service.proto` + ring buffer + disk store + rate limiter (RFC 0018 PR 4/7) (#172)
- *(logs)* `LogService` server + agent shipper + REST + SSE (RFC 0018 PR 5/7) (#173)
- *(cli)* `persatrix logs` rewrite — filters + SSE follow + E2E (RFC 0018 PR 6/7) (#174)
- *(observability)* `internal/telemetry` → `internal/observability` rename + Python OTEL init + gRPC + Baggage (RFC 0019 PR 1/5) (#163)
- *(observability)* Semantic spans + Span Links + Gen-AI conventions (RFC 0019 PR 2/5) (#167)
- *(observability)* OTLP metrics (Python + Go) with exemplars (RFC 0019 PR 3/5) (#170)
- *(observability)* Collector pipeline + docker-compose + E2E + schema-parity test (RFC 0019 PR 4/5) (#171)
- *(docker)* Wire persona agent ember-owl into compose stack (#188)

### 🐛 Bug Fixes

- *(logs)* Zap encoder correctness cluster — Must-style ctor + reserved-key shadowing (issue #178) (#183)
- *(logs,observability)* Should-Fix correctness cluster — sentinel collision + timestamp policy + SSE write deadline (issue #179) (#182)
- *(logs)* Tee orchestrator zap entries into log buffer — MT-LOGS-001 follow-up (#184)
- *(observability)* MT-OTEL-001 walkthrough alignment + propagation-gap surfacing (#185)
- *(logs)* RFC 0018 closeout — review follow-ups + status flip (PR 7/7) (#180)
- *(observability)* RFC 0019 closeout — review follow-ups + status flip (PR 5/5) (#181)

### 🔧 Refactoring

- *(logs)* Log buffer + shipper polish (RFC 0018 PR 8, optional polish) (#177)
- *(observability)* Tracing/spans review follow-ups (RFC 0019 PR 6, optional polish) (#176)

### 📚 Documentation

- *(rfcs)* Joint PR plans for RFC 0018 + RFC 0019 (v0.2.3 Observability Foundation) (#161)
- *(rfcs)* Describe closeout PR scope in plans (#175)
- *(release)* v0.2.3 release preparation plan (#186)
- *(release)* v0.2.3 MT execution report + release-prep fixes (#187)
- *(release)* v0.2.3 README + ROADMAP + guide refresh + observability diagram + release checklist (#189)

### 🧪 Testing

- *(observability)* Schema-parity, log↔trace correlation, and compose-gated E2E (RFC 0019 PR 4) (#171)
- *(logs)* `logbuffer` ring + disk-store + rate-limiter unit tests (RFC 0018 PR 4) (#172)
- *(logs)* `LogService` server + agent shipper + REST + SSE tests (RFC 0018 PR 5) (#173)
- *(logs)* `persatrix logs` REST round-trip + SSE follow E2E (RFC 0018 PR 6) (#174)

### 📦 Miscellaneous

- *(deps)* Upgrade `tabled` 0.16 → 0.20, resolve RUSTSEC-2024-0370 (#162)

[0.2.3]: https://github.com/mkhomutov/Persatrix/compare/v0.2.2...v0.2.3

## [0.2.2] - 2026-04-22

> **Codename:** Bounded Persona Memory Injection

### Highlights

- Persona-agent memory injection now enforces a per-event token budget. A new
  `MemoryBudget` allocator distributes available tokens across the three memory
  tiers (episodic, relationship, working) and truncates injected context to fit.
- Episodic and relationship `recall` / `recall_notes` calls now accept a
  `min_score` relevance threshold, reducing noise in injected memory.
- TICK events that admit zero memory items after budget allocation are
  short-circuited before reaching the LLM, eliminating spurious cost on
  persona agents with empty context windows.

### Upgrade Notes

- **No breaking changes.** All RFC 0017 changes are internal to the Python
  agent runtime. No proto changes, no new REST endpoints, no config schema
  changes.
- **Optional:** `min_score` defaults to `0.0` (matches previous behaviour).
  Set it in `recall`/`recall_notes` tool calls to filter low-relevance
  memories proactively.

### 🚀 Features

- *(agents)* `MemoryBudget` allocator + token-aware truncation (RFC 0017 PR 1/7) (#145)
- *(agents)* `_inject_memory_context` allocate-loop rewrite (RFC 0017 PR 2/7) (#146)
- *(memory)* `min_score` relevance threshold on `recall`/`recall_notes` (RFC 0017 PR 3/7) (#147)
- *(agents)* Wire `min_score` and remove legacy gates (RFC 0017 PR 4/7) (#148)

### 🐛 Bug Fixes

- *(agents)* Short-circuit empty-context TICKs (RFC 0017 PR 5/7) (#149)
- *(agents)* RFC 0017 PR 6 review follow-ups (#152)

### 📚 Documentation

- *(safety)* Add cost warning, responsible-use section, and runtime cost notice (#150)
- *(rfcs)* Close RFC 0017 — Persona Memory Injection Token Budget (#153)
- *(manual-tests)* Add MT-MEMORY-004 and MT-PERSONA-003 runbooks for RFC 0017 (#154)
- *(release)* v0.2.2 release checklist + prep plan + README/guide refresh (#156)

### 🧪 Testing

- *(manual)* v0.2.2 execution report — 18 pass, 1 accepted-with-known-gap (#155)

### 📦 Miscellaneous

- *(deps)* Bump `rustls-webpki` from 0.103.10 to 0.103.12 in `/cli` (#139)

[0.2.2]: https://github.com/mkhomutov/Persatrix/compare/v0.2.1...v0.2.2

## [0.2.1] - 2026-04-21

> **Codename:** Talk to Your Agents

### Highlights

- Human-agent chat is now part of the core surface. Open a terminal and run
  `persatrix chat <agent_id>` to start an interactive conversation with any persona agent.
- A new `Participant` protocol and `UserParticipant` implementation give the system a
  first-class model for human participants, with relationship-memory tracking per user-agent pair.
- The `POST /api/v1/agents/{id}/chat` REST endpoint and the `SendChatMessage` gRPC RPC
  are both live and tested (see MT-CHAT-001 through MT-CHAT-004 in the manual-test suite).
- Binary renamed from `orch` to `persatrix` — the CLI is now a single, coherent tool.

### Upgrade Notes

- **New gRPC RPC:** `SendChatMessage` added to `AgentService` (proto/task.proto). Regenerate
  gRPC stubs if you maintain a custom client.
- **New REST endpoint:** `POST /api/v1/agents/{id}/chat` — accepts `{message, user_id, session_id}`
  and returns `{reply, session_id, agent_display_name, reply_status}`.
- **Binary rename:** the CLI binary is now `persatrix` (previously `orch`). Update any scripts
  or CI steps that reference the old name.
- **RelationshipMemory generalised:** `RelationshipMemory` now models arbitrary participant pairs
  (agent↔agent or user↔agent). Existing agent-agent relationship data is unaffected.

### 🚀 Features

- *(agents)* Participant Protocol + UserParticipant + UserStore (RFC 0016 PR 1/7) (#119)
- *(agents)* Generalize RelationshipMemory to participant pairs (RFC 0016 PR 2/7) (#120)
- *(agents)* SendChatMessage gRPC servicer + EventDispatcher flag (RFC 0016 PR 3/7) (#121)
- *(server)* Add REST chat endpoint and gRPC chat executor (RFC 0016 PR 4) (#123)
- *(cli)* Add `persatrix chat` command and rename binary (RFC 0016 PR 5/7) (#125)

### 🐛 Bug Fixes

- *(agents,cli)* Address PR 1–5 review follow-ups (RFC 0016 PR 6/7) (#127)
- *(persona-runtime)* Apply PR #131 deep-review follow-ups (#133)

### 🔧 Refactoring

- *(executor)* Split executor.go into executor.go + dispatch.go (#124)

### 📚 Documentation

- *(rfcs)* Correct author attribution across all RFCs (#115)
- *(rfc)* RFC 0015 — Process Automation & Pattern Extraction (#114)
- *(rfc)* RFC 0016 — Human Participant & Chat Interface (#116)
- *(rfc)* Accept RFC 0016 and add PR implementation plan (#118)
- *(rfc)* Close RFC 0016 — Human Participant & Chat Interface (PR 7/7) (#128)
- *(diagrams)* Architecture diagram refresh for v0.2.1 chat surface (#132)
- *(guide)* Add chat walkthrough to persona-agents guide (#135)
- *(readme)* Refresh README for v0.2.1 chat surface (#136)
- *(release)* Add v0.2.1 release checklist (#137)

### 🧪 Testing

- Author manual tests — chat & participant surface (MT-CHAT-001..004) (#130)
- Execute manual test suite, record results (#131)

[0.2.1]: https://github.com/mkhomutov/Persatrix/compare/v0.2.0...v0.2.1

## [0.2.0] - 2026-04-18

> **Note:** Persatrix was previously developed internally under a different name.
> The project was renamed in April 2026 prior to this first public release.

### Highlights

- Persona-agent runtime is now part of the core surface for v0.2, including event-driven behavior,
  autonomous ticks, and integrated memory tools.
- Memory capabilities now include episodic, relationship, and working tiers with persistence,
  context-window management, and summarization paths.
- Workflow execution now includes execution limits, cost tracking, budget enforcement,
  response caching, and a cost summary API.
- Default `max_tokens` for task agents raised from **4096** to **8192**, improving out-of-box
  capacity for code review and generation workloads.

### Upgrade Notes

- **Behavior change:** task-agent default `max_llm_calls` is reduced from **10** to **5**.
  If your workflows relied on the previous default for long tool/LLM loops, set an explicit
  `max_llm_calls` override in workflow step config or agent config.

### 🚀 Features

- *(agents)* Data-driven TaskAgent + agent type system (#47)
- *(cli)* Wire v0.1 REST endpoints (RFC 0005, PR 1b) (#48)
- *(memory)* Working memory + token estimation (RFC 0005, PR 2) (#49)
- *(memory)* Schema migration + episodic memory core (RFC 0005, PR 3a) (#50)
- *(memory)* Agent-initiated memory tools (RFC 0005, PR 3b) (#51)
- *(memory)* Episode auto-summarization (RFC 0005, PR 3c) (#52)
- *(memory)* Relationship memory (RFC 0005, PR 4) (#53)
- *(agents)* PersonaAgent runtime core (#54)
- *(agents)* Event dispatch + tick loop integration (RFC 0005 PR 5b) (#55)
- *(agents)* Config validation + schema wiring (RFC 0005, PR 6a) (#56)
- *(cli)* Wire validate + test --persona commands (RFC 0005, PR 6b) (#57)
- *(persona,validate)* Persona + validation review fixes (PR 7b) (#60)
- Add defaults package, step limit fields, and schema updates (RFC 0006 PR 1a) (#79)
- Wire execution limits through executor and scheduler (RFC 0006 PR 1b) (#81)
- Implement Python defaults and limit validation (RFC 0006 PR 1c) (#83)
- *(executor)* Derived deadline mode with shared retry budget (RFC 0006 PR 2) (#84)
- *(cost)* Implement TokenCounter and BudgetEnforcer (RFC 0006 PR 3a) (#85)
- *(cost)* CostReporter + scheduler budget integration (RFC 0006 PR 3b) (#86)
- *(state)* StepExecutionMetadata + observability (RFC 0006 PR 4a) (#87)
- *(cost)* RFC 0006 PR 4b — Response Cache + Cost Summary Endpoint (#88)

### 🐛 Bug Fixes

- *(memory)* Memory tier review fixes (RFC 0005, PR 7a) (#59)
- *(cli)* Rust CLI review fixes (RFC 0005, PR 7c) (#62)
- Resolve Windows setup, Docker service discovery, and tool schema bugs (#71)
- *(executor,scheduler,state)* RFC 0006 PR 5a — execution follow-up fixes (#90)
- *(cost)* Atomic budget snapshot, BudgetError struct, config validation (RFC 0006 PR 5b) (#91)
- *(cost)* Remove dead rawPricing field, fix CacheKey non-deterministic hashing
- *(planner,agents)* RFC 0006 PR 5c — Planner/Schema + Python Fixes (#92)
- *(agents)* Surface invalid_fields in negative-limit error metadata (RFC 0006 PR 5c N-01, N-02) (#93)

### 🔧 Refactoring

- *(persona)* Split persona.py into focused modules (RFC 0005, PR 8a) (#64)
- *(persona)* Extract _LLMPersonaAgent to persona_runtime.py (RFC 0005, PR 8d) (#65)
- *(memory)* Split episodic.py into focused modules (RFC 0005, PR 8b) (#66)
- *(cli)* Split main.rs into modules (RFC 0005, PR 8c) (#67)
- Rename project to Persatrix (#70)
- *(agents)* Split persona_runtime.py into package (#95)
- *(scheduler)* Split scheduler.go into stage_runner.go and budget.go (#96)
- *(agents)* Split episodic.py and server.py (v0.2 release prep A-3) (#97)

### 📚 Documentation

- *(rfc)* RFC 0005 — Persona Agent & Memory System (v0.2 planning) (#45)
- *(rfc0005)* Add PR implementation plan for Persona Agent & Memory System (#46)
- *(rfc0005)* Add PR 3a review findings to PR plan
- *(roadmap)* Update episodic memory component status for PR 3c
- *(roadmap)* Add persona.py component status, fix PR #54 link
- Update ROADMAP last-updated date to 2026-04-13
- Fix PR #56 link in ROADMAP merged PR history
- *(rfc0005)* Split PR 7 into 4 sub-PRs (7a-7d) (#58)
- Add development workflow lifecycle guide (#61)
- Add documentation & diagrams phase to workflow and PR plan (RFC 0005, PR 9) (#68)
- Close RFC 0005 — Persona Agent & Memory System (PR 7d, 20/20) (#69)
- *(rfc)* Propose RFC 0006 (Efficiency & Execution Limits) and RFC 0007 (Conditional & Looped Control Flow) (#72)
- *(rfc)* Add RFC 0008 for agent memory and context optimization (#73)
- *(rfc)* Add RFC 0009 — Agent Identity, Security & Sandboxing (#74) (#74)
- *(rfc0006)* Resolve open questions, accept RFC (#75)
- *(rfc0008)* Resolve open questions and accept RFC (#76)
- *(rfc)* RFC 0013 — Legal, Ethical & Regulatory Compliance Framework (#77)
- *(rfc0006)* Add PR implementation plan for Efficiency & Execution Limits (#78)
- *(rfc)* RFC 0014 — Agent Skill Registry & Lifecycle (#80)
- *(roadmap)* Restructure versioning strategy for release velocity (#82)
- *(rfc0006)* Add detailed follow-up PR descriptions (5a-5c) and update status (#89)
- Add v0.2.0 release preparation plan (#94)
- *(tests)* Author manual tests for v0.1 surface (v0.2 release prep C-8) (#98)
- *(tests)* Author manual tests for v0.2 surface (PR 9) (#99)
- README overhaul for v0.2.0 (v0.2 release prep B-4) (#102)
- *(guides)* Persona & memory user guide (v0.2 release prep B-5) (#103)
- *(diagrams)* Phase-neutral architecture diagrams (v0.2 release prep B-7) (#104)

### 📦 Miscellaneous

- Ongoing manual test campaign and fixes (WIP) (#101)
- Move repository to BUSL 1.1 (#63)

[0.2.0]: https://github.com/mkhomutov/Persatrix/compare/v0.1.0...v0.2.0

## [0.1.0] - 2026-04-11

### 🚀 Features

- Scaffold initial project structure (#1)
- Adopt blueprint tooling for project governance and quality gates (#2)
- *(state)* Implement InMemoryStateStore (RFC 0001, PR 1/5) (#6)
- *(registry)* Implement InMemoryRegistry (RFC 0001, PR 2/5) (#7)
- *(planner)* Implement YAMLPlanner Parse+DAG+Plan (RFC 0001, PR 3a/5) (#8)
- *(planner)* Implement ResolveInputs template resolution (RFC 0001, PR 4/5) (#9)
- *(orchestrator)* Wire state, registry, planner into main.go (RFC 0001, PR 5/5) (#10)
- *(server)* HTTP server scaffolding + workflow handlers (RFC 0002, Phase 1) (#14)
- *(server)* Implement agent registry endpoints (RFC 0002, PR 3/4) (#16)
- *(server)* Stub endpoints + main.go wiring + Docker fix (RFC 0002, PR 4/4) (#17)
- *(proto)* Generate Go gRPC stubs from protobuf definitions (#21)
- *(executor)* GRPCExecutor core with retry logic (#22)
- *(state)* Add RunRetrying, SetRunTimestamps, SetRunError (RFC 0003, PR 4/7) (#24)
- *(scheduler)* WorkflowScheduler core with polling, parallel stages, dedup (RFC 0003, PR 3a/7) (#25)
- *(orchestrator)* Wire scheduler + executor into main.go (RFC 0003, PR 5/7) (#27)
- *(agents)* PermissionGate + PathValidator (RFC 0004, PR 2/7) (#36)
- *(agents)* Built-in tools + PR 2 follow-up fixes (RFC 0004, PR 3/7) (#37)
- *(agents)* LLM client + TaskInputConfig + base handle loop (RFC 0004, PR 4a/7) (#38)
- *(agents)* CoderAgent, ReviewerAgent, PlannerAgent (RFC 0004, PR 4b/7) (#39)
- *(agents)* GRPC server + agent loading + proto stubs (RFC 0004, PR 5a) (#40)
- *(agents)* Self-registration + integration tests + follow-up fixes (RFC 0004, PR 5b/7) (#41)

### 🐛 Bug Fixes

- Address accumulated review findings (RFC 0001, PR 6/6) (#12)
- Address accumulated review findings (RFC 0002, PR 5/5) (#18)
- *(state)* Replace rune-based test IDs with fmt.Sprintf (RFC 0001, F-06) (#30)
- *(executor)* Additive dial options, mid-dispatch cancel & retry stress tests (RFC 0003, PR 6) (#31)
- *(orchestrator)* Graceful shutdown drain + absolute workflowsDir (RFC 0003, PR 8) (#33)
- *(agents)* Registration follow-ups + RFC 0004 close (PR 6/7) (#42)
- *(lint)* Resolve all golangci-lint, ruff, mypy, clippy warnings (#44)
- *(agents)* Surface `invalid_fields` in `TaskOutput.metadata` when negative
  execution limits are rejected, to aid operator diagnosis of misconfigured
  `TaskConfig` values. Strengthen explicit-limit test to verify the loop is
  capped at the configured value (RFC 0006 PR 5c follow-ups N-01, N-02)

### 📚 Documentation

- RFC 0001 Core Orchestration Pipeline (#3)
- PR implementation plan for RFC 0001 (#5)
- *(plan)* Update PR plan with PR #8 review follow-ups
- RFC 0002 REST API Server (#4)
- PR implementation plan for RFC 0002 (#11)
- RFC 0003 Scheduler & Executor (#13)
- RFC 0004 Python Agent gRPC Server (#15)
- RFC 0004 PR implementation plan (#19)
- Add ROADMAP.md, status hygiene rules, fix pre-commit checks (#20)
- Update PR plan with PR #22 review findings (N-06..N-11)
- Add follow-up PRs 6-9 to RFC 0003 PR plan (#28)
- Close RFC 0002 PR plan — mark PR 5 as superseded
- *(rfc0001)* Complete PR 6 follow-up scope with all carry-forward findings (#29)
- RFC 0003/0004 status updates, multi-provider LLM design, v0.2 deferrals (#35)
- Update progress tracking for PR #39 merge (RFC 0004, 5/7)
- *(roadmap)* Add missing merged PRs #28, #29, #30, #35 to history table
- Add v0.1 release checklist (#43)

### 🧪 Testing

- *(executor)* IsTransient table-driven tests, retry edge cases, concurrent dispatch (#23)
- *(scheduler)* Step execution, template resolution, error path coverage (RFC 0003, PR 3b/7) (#26)
- Observability improvements — concurrent race tests, log assertions, zaptest logger (#32)

### 🏗️ Build

- *(proto)* Split make proto into go/python targets + CI staleness check (RFC 0003, PR 9) (#34)

### 📦 Miscellaneous

- Update FILEMAP.md

[0.1.0]: https://github.com/mkhomutov/Persatrix/releases/tag/v0.1.0


