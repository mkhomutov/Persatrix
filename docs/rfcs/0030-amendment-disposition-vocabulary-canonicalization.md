# RFC 0030 Amendment — Canonicalize the Disposition Vocabulary (Deprecate the Legacy Config Synonyms)

**Type**: amendment to the [relevance-gated-response amendment](0030-amendment-relevance-gated-response.md) §"Membership becomes a disposition, not a trigger", narrowing the config-surface contract of [RFC 0011 §A](0011-channels-bridges.md) (the member `{id, respond}` shape)
**Status**: 📋 Proposed
**Author**: Maksim Khomutov
**Date**: 2026-06-10
**Target**: warn phase v0.3.9; reject phase no earlier than v0.4.0 (OQ 3)
**Trigger**: The respond-policy type review (PRs [#597](https://github.com/mkhomutov/Persatrix/pull/597), [#598](https://github.com/mkhomutov/Persatrix/pull/598)) catalogued the vocabulary across every surface and found that two of the legacy values are *pure synonyms* at the config surface: `when_mentioned` ≡ `addressed` and `never` ≡ `observer` — [`Normalize()`](../../internal/channels/channels.go) maps them 1:1 and [`ResolveSalienceSignal`](../../internal/channels/channels.go) derives nothing extra from either spelling. Offering both indefinitely is migration debt: every operator-facing doc, schema description, and review conversation must explain two names for one behaviour, and the [relevance amendment](0030-amendment-relevance-gated-response.md)'s "recommended vocabulary" framing never terminates.
**Supersedes**: the open-ended back-compat promise in the [relevance amendment](0030-amendment-relevance-gated-response.md) and [`channel.schema.json`](../../schemas/channel.schema.json) ("the legacy values `always`/`when_mentioned`/`never` keep working") — for **`when_mentioned` and `never` only**, and **only at the config surface**. `always` stays first-class (§B), and the legacy triple as the internal/DB/wire **encoding** is explicitly not deprecated (§C).

---

## Context

The respond vocabulary today decomposes onto two axes — *when do I take the floor* (open / addressed / silent) and *is floor-taking bid-gated* — and the seven accepted values map onto them as:

| Declared | Floor | Bid-gated | Notes |
|---|---|---|---|
| `participant` | open | yes | unset threshold biases to silence |
| `chair` | open | yes | low default threshold ([`DefaultChairThreshold`](../../internal/channels/config.go)) |
| `always` | open | **no** (opt-in via explicit `threshold`) | **no disposition equivalent** — see §B |
| `addressed` / `when_mentioned` | addressed | n/a | pure synonyms |
| `observer` / `never` | silent | n/a | pure synonyms |

The synonym pairs exist because the [relevance amendment](0030-amendment-relevance-gated-response.md) deliberately *renamed* the vocabulary (trigger-words → dispositions) while keeping the old spellings loadable. That was the right migration posture for v0.3.7; this amendment is the migration's planned terminal step, now that the vocabulary's cross-surface relationships are pinned by the [#597 drift tests](../../tests/unit/python/test_cross_language_respond_policy_drift.py) and its write boundaries are centralized behind [`ResolveMemberPolicy`](../../internal/channels/member_policy.go) (#598).

## A. What is deprecated

The legacy spellings **`when_mentioned`** and **`never`** as *declared member values* at the two operator-facing surfaces:

1. **Config** — `members[].respond` in `config/channels.yaml` ([RFC 0011 §A](0011-channels-bridges.md)).
2. **REST** — the `respond` field of member entries on `POST /api/v1/channels` and `POST /api/v1/channels/{id}/members` ([`channel_handlers.go`](../../internal/server/channel_handlers.go)) — the only two REST surfaces that accept a declared `respond` today. There is **no member-policy update endpoint**; the store's `SetMemberPolicy` is an encoding-side write, not a declared surface (§C), and its one current caller (the chat façade's fanout suppression in [`chat_handler.go`](../../internal/server/chat_handler.go)) passes the canonical `never`, which must stay warning-free. A future update endpoint would inherit the deprecation by construction, by resolving declared values through the same [`ResolveMemberPolicy`](../../internal/channels/member_policy.go) boundary.

"Declared" means *written by the operator*. An omitted `respond` — the string-shorthand member form, or an empty REST `respond` field — defaults to `when_mentioned` ([RFC 0011 §A](0011-channels-bridges.md); [`MemberConfig.UnmarshalYAML`](../../internal/channels/config.go), [`wireRespondPolicy`](../../internal/server/channel_types.go)), but the default is applied *after* the declared surface: defaulting is not a declaration, so it neither warns in Phase 1 nor rejects in Phase 2. (Whether the default's internal *spelling* should someday read `addressed` is cosmetic — identical post-`Normalize()` — and out of scope.)

Operators write `addressed` and `observer` instead — same behaviour, by construction (`Normalize()` is the identity proof: both spellings resolve to the identical persisted triple).

## B. What is NOT deprecated

- **`always`.** It has no disposition equivalent: a bare `always` replies unconditionally, while `participant` always runs the Tier B salience bid (unset threshold = bias-to-silence). Deprecating `always` would force every unconditional-replier through a rename that *changes behaviour* — exactly what a vocabulary deprecation must never do. (The [relevance amendment](0030-amendment-relevance-gated-response.md)'s back-compat line — "`always` maps to `participant` with a permissive threshold" — predates implementation and did not survive it: the PR 2b design decision ([Tier B PR plan](0030-amendment-relevance-gated-response-tierb-pr-plan.md), 2026-06-07) kept a bare `always` unconditional so v0.3.7 channels are byte-identical, with `always` + explicit `threshold` as the bid opt-in — the semantics [`ResolveSalienceSignal`](../../internal/channels/channels.go) implements and the Context table records. This amendment's PR adds the correction note in place there.) Two options were considered:
  - *(chosen)* `always` remains a first-class config value indefinitely. The vocabulary's irregularity (six disposition-era names plus one legacy survivor) is honest: unconditional reply is a deliberate, distinct behaviour, and its legacy name signals "pre-bid semantics" accurately.
  - *(rejected for now)* mint a new disposition name for unconditional reply (e.g. `speaker`). A new name with zero new semantics expands the closed vocabulary — a governance review surface ([RFC 0030 §B](0030-multi-agent-conversation-governance.md)) — for purely cosmetic symmetry. Revisit only if Layer 5 work gives the name real semantic content (OQ 1).
- **The legacy triple as the internal encoding.** The membership-table CHECK constraint, the wire `respond_policy`, and the Python gate's canonical vocabulary all keep speaking `when_mentioned`/`always`/`never` — permanently, as far as this amendment is concerned. The encoding-vs-surface distinction is the load-bearing design fact: dispositions are *declared*, the triple is *persisted*; deprecating a surface spelling has zero wire/DB/mixed-version footprint. (Consequence: the [#597 pins](../../tests/unit/python/test_cross_language_respond_policy_drift.py) asserting the DB CHECK, wire enum, and gate vocabulary are untouched by both phases.)
- **The agent-side gate's recognition** of every spelling ([`agents/response_gate.py`](../../agents/response_gate.py)). The gate is defence-in-depth for abnormal inputs; it never narrows.

## C. Mechanism

Two phases, each independently shippable, the second deliberately distant:

### Phase 1 — warn (target v0.3.9)

1. **Config load**: [`Config.Validate`](../../internal/channels/config_validate.go) logs one structured deprecation warning per member declaring `when_mentioned`/`never` (channel/member-indexed, like its errors): *"respond: never is deprecated as a config spelling; write observer (same behaviour)"*. Validate (not `UnmarshalYAML`) so the warning carries the index and fires once per load, not per YAML decode. One wrinkle the implementation must handle: Validate today sees only the *post-Normalize* triple — [`MemberConfig.UnmarshalYAML`](../../internal/channels/config.go) collapses the declared spelling at decode time — so a declared `when_mentioned`, a declared `addressed`, and the shorthand default are already indistinguishable by the time validation runs. The warn therefore needs `UnmarshalYAML` to additionally record the raw declared spelling on `MemberConfig` (decode-time bookkeeping; nothing downstream reads it), left empty for defaulted members so they never warn (§A). Phase 2's loader rejection reuses the same field.
2. **REST**: the same warning logged at the two member-accepting handlers (`POST /api/v1/channels`, `POST /api/v1/channels/{id}/members`), which see the raw declared string before [`wireRespondPolicy`](../../internal/server/channel_types.go) defaulting — no bookkeeping field needed there. The warn lives in the handlers, never in the store's write methods: the store speaks the encoding (§C), and its internal callers legitimately write canonical legacy values (the chat façade's `SetMemberPolicy(…, never)` fanout suppression in [`chat_handler.go`](../../internal/server/chat_handler.go)). No response-shape change; a 4xx here would be the reject phase smuggled in early.
3. **Telemetry**: a bounded-label counter — `channel.members.deprecated_spelling{spelling, surface}`, `spelling ∈ {when_mentioned, never}`, `surface ∈ {config, rest}` — incremented wherever the warning fires, following the repo's counter idiom (`channel.messages.gated{policy}`, `governance_drop{layer}`). This is the instrument OQ 3 consumes: a log line means log archaeology and a response header produces no queryable record at all, while the counter makes "are the spellings gone?" decidable from a dashboard. Because it increments server-side on every REST-declared spelling regardless of whether anyone reads the orchestrator log, it also closes the REST under-count blind spot OQ 3 previously noted.
4. **In-tree emitters migrate first**: the web console *declares* the deprecated spelling today — [`ChannelMembers.svelte`](../../web/src/panels/ChannelMembers.svelte) hardcodes `addRespond = "when_mentioned"` as its initial/reset value and lists it first in the dropdown — which is a declared value in §A's sense, so without this step every routine console member-add fires the warning and pollutes the very telemetry the phase exists to collect. The console default/dropdown ordering and the CLI's help-text defaults ([`channel_dispatch.rs`](../../cli/src/commands/channel_dispatch.rs), [`channel_manage.rs`](../../cli/src/commands/channel_manage.rs) — documentation strings only; the CLI passes tokens through) move to disposition spellings **before or with** the warn landing.
5. **Docs**: [`channel.schema.json`](../../schemas/channel.schema.json) `respond` description marks the two spellings deprecated (the `enum` itself is unchanged in this phase) — and the same edit drops the description's stale "`always`/`when_mentioned`/`never` … map to `participant`/`addressed`/`observer` respectively" claim, which overstates the mapping (`always` ≢ `participant`, §B); [channels guide](../guides/channels.md) examples migrate to disposition spellings; CHANGELOG upgrade-notes row.

Phase 1 changes no behaviour, no schema enum, no test pins. The `deprecated_spelling` counter (does anyone still declare these spellings, on either surface?) is the evidence OQ 3 consumes.

### Phase 2 — reject (no earlier than v0.4.0; OQ 3)

1. Config load **error** and REST **400** (`ErrInvalidRespondPolicy` family) for the two spellings at the declared surface.
2. Schema `enum` narrows to the five surviving values; the [#597 schema-enum pin](../../tests/unit/python/test_cross_language_respond_policy_drift.py) is updated in the same PR — the pin forcing that deliberate co-edit is it working as designed.
3. `RespondPolicy.Valid()` / `Normalize()` and everything downstream are **unchanged**: the spellings remain valid *encoding* values (they are the encoding), so a pre-deprecation DB row, an old wire peer, or a hand-rolled store caller keeps working. Only the YAML/REST *declared-value* acceptance narrows, at the same boundary that already distinguishes declared from canonical ([`MemberConfig.UnmarshalYAML`](../../internal/channels/config.go), [`resolveMemberRequests`](../../internal/server/channel_types.go)).

## D. Back-compat and mixed-version analysis

The entire amendment is config-surface-only. Nothing on the wire, in the DB, or in the Python agent changes in either phase, so the mixed-version deployment matrix — the case the [#597 drift pins](../../tests/unit/python/test_cross_language_respond_policy_drift.py) exist for — is unaffected: an old orchestrator and a new agent (or vice versa) never exchange a deprecated *declared* spelling, because declared spellings never leave the orchestrator's load boundary. The only breakage surface is an operator's unmigrated `channels.yaml` meeting a Phase 2 orchestrator, which fails loudly at load time with the rename spelled out in the error.

## E. Test strategy

- **Phase 1**: unit tests pinning the warning fires (config load with each deprecated spelling; REST paths) **and the `deprecated_spelling` counter increments with the matching `{spelling, surface}` labels**, and — equally — that `addressed`/`observer`/`always` **and the omitted-`respond` defaults** (string-shorthand member, empty REST field) neither warn nor count. The in-tree migration (item 4) is pinned by the console's existing component tests (the member-add default posts `addressed`) and the CLI help-text snapshot. No drift-pin changes.
- **Phase 2**: the loader/REST rejection tests flip from warn-pinned to error-pinned; the schema-enum drift pin is co-edited; a regression test pins that a persisted legacy row still round-trips (encoding unaffected).

## Open questions

1. Should `always` eventually get a disposition-era name? Deferred until Layer 5 (v0.4.0) shows whether unconditional reply acquires new semantics worth naming. Default: no.
2. ~~Should Phase 1's REST warning also surface to the caller (e.g. a `Deprecation` response header) rather than log-only?~~
   **Resolved**: log-only; no header (2026-06-10). A header's only audience is a client that reads it, and the callers of the two REST surfaces are owned in-tree — the web console and the CLI — where the correct move is migrating their defaults off the deprecated spellings (§C Phase 1 item 4), after which a header has nobody left to inform; for the out-of-tree remainder, the API caller and the orchestrator-log reader are the same operator in the [RFC 0048](0048-operator-tester-web-console.md) single-operator deployment model. Standards-wise there is no clean fit either: RFC 9745's `Deprecation` header denotes deprecation of the *resource*, so sending it would tell tooling the endpoint itself is sunsetting (false), and the value-level `Warning` header is itself deprecated (RFC 9111). The two needs the header was reserved for are each served better elsewhere: *measurement* by the §C Phase 1 `deprecated_spelling` counter, and *reaching a log-blind caller* by Phase 2's loud 400 with the rename spelled out in the error — a one-word fix at a clear boundary, never a silent break. Revisit only if the API gains genuinely external consumers (the [RFC 0039](0039-user-accounts-authentication.md) era).
3. Phase 2 timing: gated on Phase 1 warn telemetry showing the spellings are gone from real configs, with v0.4.0 as the earliest candidate — never the same release as Phase 1. The previously noted blind spot (log-only warns under-count REST-originated declarations from automation whose operator never reads orchestrator logs) is closed by the §C Phase 1 counter, which increments server-side on every declared spelling regardless of who reads the log: "gone" means the counter stays flat at zero on **both** surfaces across the observation window, not log silence.

## Related documentation

- [RFC 0030 relevance amendment](0030-amendment-relevance-gated-response.md) — introduced the disposition vocabulary this amendment canonicalizes
- [RFC 0011 §A](0011-channels-bridges.md) — the member `{id, respond}` config shape
- [`test_cross_language_respond_policy_drift.py`](../../tests/unit/python/test_cross_language_respond_policy_drift.py) — the cross-surface pins (PR #597)
- [`internal/channels/member_policy.go`](../../internal/channels/member_policy.go) — the centralized write-boundary constructor (PR #598)
- [channels guide](../guides/channels.md) — operator-facing vocabulary docs
