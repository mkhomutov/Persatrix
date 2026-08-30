# ISSUE-0130 shape (b) — the build log

**Companion to**: [ISSUE-0130](ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md)
**Milestone**: [v0.3.15 plan](../v0.3.15-plan.md) workstream **B** — B1 (the column) → B2 (the reader)

The dated notes that carried shape (b) from "scheduled" to "shipped": the
sequencing decisions, the scope locks each PR inherited, and what each
landing left for the next one to pick up.

Split out of the issue on 2026-08-30, at B2, for the reason the sibling
[Phase 0 gate record](ISSUE-0082-residuals-phase0-gate.md) was split out of
its plan: the issue had reached the 3 000-word documentation cap, and a log
that grows once per PR turns every later entry into a trim of the diagnosis
above it. The issue keeps the diagnosis, the impact and the fix shapes; this
keeps the build history.

---

> 2026-08-19 — shape **(b)** **slotted v0.3.15** by the [sequencing Amendment 2026-08-19](../v0.3.x-sequencing.md#amendment-2026-08-19--v0315--v0316-attribution-and-audience-before-the-v040-train). It carries the
> channel-store migration on its own: `principal_id` on `messages` is
> `internal/channels/sqlite_schema.go` v11 → v12 (Go). The sibling speaker
> axis ([ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md))
> does **not** ride it — that lands in the Python persona-memory store
> (migration 17 → 18), a disjoint database. The two are bound by the #822
> Phase 0 **record-shape** decision, not by a shared schema.
>
> 2026-08-23 — **(b) has its PR slots.** The [v0.3.15 plan](../v0.3.15-plan.md)
> is open and owns this workstream, as the §Related note above anticipated
> ("no PR slot in [the residuals plan] and is owned by the v0.3.15 milestone
> plan"). It lands as two PRs on the dormant-rail-then-consumer split v0.3.14
> PR 1 / PR 2 established: **B1** carries the channel-store migration
> `v11 → v12` plus the server-side stamp at publish and the
> `channelMessageResponse` field, with no persona-side reader; **B2** seeds
> `_build_replay_event` from it, narrows the shape-(a) derivation skip to
> genuinely unattributable spans, and restores the RFC 0037 replayed-rotation
> classification stamping withdrawn in
> [#834](https://github.com/mkhomutov/Persatrix/pull/834).
> `REASON_CATCHUP_COMPLETE` and the replay-opened-scope boundary are untouched
> — they were never about attribution.
>
> The 2026-08-23 correction above (this migration is (b)'s alone, and (b) is
> not gated on R-2) is carried into the plan as a scope lock, so B1/B2 run in
> parallel with the residuals rather than queueing behind them.
>
> **The live check this issue names as missing gets written.** B2 authors a
> restart leg on [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md)
> — the release's single live gate. No second MT is authored: that arc already
> sets the restart up, and [ISSUE-0125](ISSUE-0125-agents-never-reregister-after-orchestrator-restart.md),
> landing first, is what makes it survivable. **The leg reads both partitions,
> not `local` alone.** This issue framed the missing check as a `local` read
> because shape (a) sent every replayed derivation there; once (b) lands, an
> attributed span derives under its *own* principal, so duplicates appear in
> the attributed partition and a `local`-only assertion passes at exactly the
> moment the regression below is worst.
>
> 2026-08-23 — **narrowing the skip requires an idempotence half, and (b) must
> carry it.** Recorded as scope lock 4 of the [v0.3.15 plan](../v0.3.15-plan.md),
> re-aimed at this issue after the plan-opening audit found the original lock
> guarding only the reconnect path. Shape (a) bounded the unbounded
> re-derivation measured above (`local` episodes `0 → 2 → 5 → 13 → 18` across
> four restarts) by skipping derivation for **every** replayed span. It did not
> make replay idempotent — [`channel_catchup.py`](../../agents/channel_catchup.py)
> still documents "**No watermark, no dedup** … `InteractionTracker.add_turn`
> does **not** deduplicate by `message_id` — it appends every turn. K
> consecutive restarts within the catch-up window produce `K × N` turns", and
> `store_episode` is a plain insert with no conflict clause. B2's job is
> precisely to narrow that skip, which hands the growth curve straight back,
> relocated from `local` to the correct principal. The doors are the ones in
> daily use — the MT's restarts at Legs 0, 7 and 8, the #823 manual
> `docker compose restart agent-<each>` procedure the plan keeps as ISSUE-0125's
> cut fallback, and every operator restart — not the reconnect path, which does
> not exist until ISSUE-0125 lands and is *cuttable*. **So B2 ships a dedup or
> watermark check alongside the narrowed skip** (dedup by `message_id` at
> ingest, or gating the narrowed skip on "this span was not already derived";
> the OQ #8(b) `?since=` watermark remains the deeper fix and stays out of
> scope). The unit bar is that a span replayed twice derives once.
>
> 2026-08-30 — **B1 open for review: the column exists and is written;
> nothing reads it.** Channel store `v11 → v12` adds `principal_id TEXT NOT
> NULL DEFAULT 'local'` to `messages`
> ([`sqlite_principal_migration.go`](../../internal/channels/sqlite_principal_migration.go)),
> stamped inside `sqliteStore.PublishMessage` from `PrincipalFromContext(ctx)`
> and surfaced on `channelMessageResponse`. Four things worth carrying into
> B2:
>
> * **The stamp OVERWRITES rather than defaults.** `PublishMessage` assigns
>   from the context unconditionally instead of filling an empty field, so a
>   caller-set `ChannelMessage.PrincipalID` is discarded. This is the
>   diagnosis above turned into a boundary: the value is the orchestrator's
>   own verification, and the REST body has no field to carry one (the
>   request struct has no counterpart and `decodeJSON` disallows unknown
>   keys, so a claim is a 400). That closes the DIRECT door: no caller can
>   name a principal.
> * **The INDIRECT door is open, and B2 must budget for it.** The R-2
>   re-stamp keys on `msg.SenderID`, and the publish ingress is
>   `policyPublic` — it takes no credential even under `auth.mode: enabled`.
>   As [`principal_restamp.go`](../../internal/channels/principal_restamp.go)
>   already states, a table hit proves the orchestrator DISPATCHED to a
>   registered agent, not that the caller IS that agent. Both the room list
>   and its membership are public GETs, so anyone who can reach the ingress
>   can publish with `sender_id` set to a member agent, inside the TTL of a
>   real authenticated turn, and the re-stamp lands attacker-chosen content
>   on a row stamped with the causing human. **B1 is what makes that
>   durable.** Before v12 the mis-attribution expired with the cascade's
>   dispatch metadata; it is now a permanent `messages.principal_id` value,
>   and B2 re-reads it on every replay. So the seed is exactly as
>   trustworthy as sender authentication at the publish seam — which is
>   RFC 0009's to supply. Until it does, the mitigation is the network
>   restriction `cmd/orchestrator/auth.go` already WARNs about, and B2
>   should treat a seeded principal as attribution evidence of the same
>   grade as the publish that produced it, not as a verified tenant.
> * **A relayed row already carries the causal tenant.** The R-2 re-stamp
>   ([ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md),
>   merged) runs at the head of `publishCommit`, *ahead of* the store commit,
>   so a persona's reply persists the principal of the person who caused it
>   rather than `local`. The wire-side R-2 tests cannot see that ordering —
>   swapping the two leaves them all green — so it is pinned separately by
>   `TestPublishMessage_PersistsTheRestampedCausalPrincipal`. The sequencing
>   preference the 2026-08-23 correction described is now a fact on the rows
>   B2 will read.
> * **The backfill asserts nothing.** Unlike the persona store's v11 — whose
>   `local` backfill hid rows that *did* have an owner — a pre-v12 `messages`
>   row never held a tenant, so `local` is the truth for it and not a
>   partition. There is no activation-day hazard here and no operator action.
> * **The READ side is open, and that is an accepted exposure rather than an
>   oversight.** `GET /api/v1/channels/{id}/messages` is `policyPublic` and
>   has to stay that way — catch-up replay is the consumer this column exists
>   for and the persona fleet holds no accounts — so every value is readable
>   with no credential. Combined with the R-2 re-stamp above, that means an
>   unauthenticated reader can attribute each *agent* utterance to the named
>   human who provoked it; before v12 that link lived only in orchestrator
>   memory. Message content was already public on the same route, so the
>   marginal disclosure is the causal link, not the words. Withholding the
>   field from unauthenticated callers was considered and rejected — it
>   blinds B2. The operative mitigation stays the one
>   `cmd/orchestrator/auth.go` already WARNs about (network-restrict the
>   ingress to the agent fleet); RFC 0009 agent tokens are what would let
>   these routes stop being public at all. **B2 must not widen this**: seeding
>   `principal_scope` from the column is a write-side attribution, and any
>   future recall predicate that keys on `principal_id` would turn a public
>   read into a tenant-selectable one.
>
> Still open, and B2's: `_build_replay_event` seeds nothing from the field,
> the shape-(a) skip is unchanged (every replayed span still derives
> nothing), RFC 0037's replayed-rotation stamping is still withdrawn, and the
> idempotence half above is unwritten. Nothing filters on the column — recall
> stays membership-and-epoch scoped, deliberately: `messages` is the room's
> shared transcript, and who was in the room is a different question from
> which tenant a derived write belongs to.
>
> 2026-08-30 — **B2 open: the column has its reader, and (b) is complete
> in code.** `build_replay_event` seeds `messages.principal_id` onto the
> event metadata key the live gRPC ingress already writes
> (`seed_principal_metadata`), so `on_event`'s existing binder attributes a
> replayed turn exactly as it attributes a live one and the record key picks
> up the tenant with no new mechanism. Four things worth carrying forward:
>
> * **PRESENCE is the signal, not the value.** The narrowed skip fires on a
>   replayed span whose opening row carried no `principal_id` **key** — a
>   pre-v12 orchestrator — and not on one that carried `"local"`. That is
>   the whole of the v0.3.14 cost 2 removal: a present `"local"` is a real
>   answer ("no verified tenant" — an agent publish, or the deployment under
>   `auth.mode: disabled`, where `local` is CORRECT), and only the key's
>   absence means "replay lost it". The record key cannot tell those apart —
>   both resolve `principal_id='local'` — so the presence is recorded at
>   open as `Interaction.replay_attributed`, frozen beside `replayed`.
>   B1's decision not to mark the Go DTO field `omitempty` is what makes
>   this readable at all.
> * **The idempotence half is a derivation-time guard, not ingest dedup.**
>   Scope lock 4 offered either; ingest dedup is the wrong lever, because
>   the tracker is in-memory and a restart starts with an empty one — it
>   cannot see the previous boot at all. What persists is the episode, so
>   the guard is: give a replayed span an identity built from its own
>   content (`(agent, principal, speaker, scope)` + the ordered wire message
>   ids), write it as the row's `interaction_id`, and decline to derive one
>   that is already stored (`replay_identity.py`). **No third migration** —
>   the release ships two and the checklist names both, so the identity had
>   to live in a column that already exists.
> * **The residual is a MOVED window, and it errs toward duplication.** The
>   same window replayed again derives once, however many restarts — the
>   stated acceptance bar. A window that gained or lost messages is a
>   different span and derives again, overlapping the earlier episode. That
>   is bounded by "restarts with traffic in between" rather than by
>   "restarts", and the OQ #8(b) `?since=` watermark remains the real fix.
>   The direction is deliberate: an identity that matched a moved window
>   would silently drop the messages that moved it, and losing memory is
>   worse than duplicating it. A unit control pins that the grown window
>   still derives, so the guard cannot quietly become a second skip.
> * **RFC 0037's replayed-rotation stamping is back**, without a line of
>   stamping code: it was never removed, only starved of a row to write.
>   The `#834` tests that flipped to pinning the skip flip back, plus a
>   third that pins the unattributable case still persisting nothing.
>
> **The read side stayed shut, as B1 required.** The seed is a write-side
> attribution only: the replay handler recalls nothing (the `replay_mode`
> short-circuit returns before the gate and the LLM), no recall predicate
> gained a `messages.principal_id` input, and the one new query — "has this
> span already been derived?" — is agent-scoped, answers a digest the caller
> already holds, and is documented as write-path-only. The indirect
> sender-spoof door B1 named is unchanged and unwidened: a spoofed publish
> can still steer the R-2 re-stamp, and the seeded principal is therefore
> attribution evidence of the same grade as the publish that produced it,
> which is RFC 0009's to raise.
>
> Left for the live arc: MT-MEMORY-GROUP-TENANT-001 gained **Leg 9** (both
> partitions snapshotted across a restart with no traffic in between, then
> a second restart, then a message and a third restart to prove the guard
> did not simply stop deriving), Leg 4 widened to
> `(principal_id, speaker_id, summary)` triples, and the sign-off's leg
> count corrected — it said eight while the procedure held nine, and the
> restart leg makes ten. The issue closes at Phase 4 with the rest.
