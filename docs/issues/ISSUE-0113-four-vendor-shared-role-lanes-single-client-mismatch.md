---
id: ISSUE-0113
summary: "The four-vendor blueprint's shared role lanes cannot run cross-vendor on the shipped one-client-per-agent runtime: the RFC 0051/0030 salience-bid lane (`fast`) and the RFC 0020 close-summary lane (`summarizer`) resolve the alias's MODEL NAME but send it through the persona's single LLMClient (built from its seat alias's provider) — a non-matching seat gets a provider 404 (e.g. the OpenAI seat asked OpenAI for `claude-haiku-4-5-20251001`), the bid fails closed to silence, and the whole four-vendor roster sits mute through every governed round. Live workaround: `respond: always` + `reasoning.mode: off` bypass the bid lane (discussion runs four-vendor); non-matching seats' close summaries still degrade to the placeholder. Fix direction: lanes construct/cache a provider from the RESOLVED alias record when it differs from the agent's own (llm_factory + a per-provider client cache), or the blueprint drops shared lanes for per-seat ones. FIXED via direction (a): LLMClient routes a cross-vendor model_alias through a provider built from the resolved record (cached per record; build failure raises a plain Exception so lanes fail closed); live-verified — the governed four-vendor arc converges with all four seats bidding and all four close summaries real."
status: resolved
severity: medium
area: agents
created: 2026-07-21
closed: 2026-07-22
refs:
  - blueprints/autonomous-multivendor/blueprint.yaml
  - docs/manual-tests/MT-AUTONOMOUS-MULTIPROVIDER-001.md
  - agents/salience_bid.py
  - agents/llm_client.py
  - docs/manual-tests/v0.3.11-execution-report.md
---

## Summary

First live four-vendor run (v0.3.11 release-prep, MT-AUTONOMOUS-MULTIPROVIDER-001):
the discussion arc went **all-silent through every round** — the convener walked
the full anti-collapse ladder (reinvite/advance ×5 → chair escalation) with zero
roster replies. Root cause: every persona holds **one** `LLMClient` bound to the
provider of its own `model:` alias (`create_provider(agent_config)`), and the
shared role lanes resolve only the **model name** from their alias:

- the Tier-B / RFC 0051 **bid lane** (`fast`, here `provider: anthropic`,
  `claude-haiku-4-5-20251001`) → the OpenAI/Gemini/watsonx seats sent that
  model id to **their own** vendor → 404 (`Tier B salience bid: provider error
  … model_not_found; staying silent`) → the conservative fail-closed posture
  reads as `nothing_to_add` for three of four seats, every round;
- the **summarizer lane** (RFC 0020 close summaries; also the RFC 0051 critic)
  has the same shape → non-matching seats degrade to
  `[interaction summary unavailable]`.

The blueprint's "summarizer … any keyed cloud vendor works" claim and the MT's
"all four personas' summaries are real" pass bar assume per-alias provider
construction that the runtime does not do. The per-vendor demos never hit this
(their overlays repoint *all* aliases to one vendor); the blueprint CI test
validates alias resolution + pricing, not runtime lane routing.

## Impact

The four-vendor **discussion** itself works (each seat's main turns run on its
own vendor) once the bid lane is bypassed — `respond: always` members +
`reasoning.mode: off` (both shipped operator knobs) — proven live in the
execution report. What cannot run cross-vendor today: governed bidding
(semantic silence) on a mixed-vendor roster, and the all-N real-summary close
(non-matching seats placeholder). Single-vendor autonomous channels (the
v0.3.11 headline MTs 001–003) are unaffected.

## Proposed fix / investigation path

Either (a) runtime: when a lane's resolved alias names a provider different
from the agent's own, build (and cache) a provider for the resolved record —
`create_provider` already knows every vendor; the watsonx per-model client
cache is the in-provider precedent; mind the per-vendor S-09 key postures — or
(b) config: per-seat lane aliases (e.g. `fast@<vendor>`) so a blueprint pins
every lane to the seat's own vendor. (a) honours RFC 0033's "the alias entry
is the joint declaration of provider + model + pricing" as stated.

## Resolution

**Fixed via direction (a)** — runtime lane routing, honouring RFC 0033 §D as
stated ("the alias entry is the joint declaration of provider + model +
pricing"):

- `LLMClient._provider_for_alias` (`agents/llm_client.py`): a
  `create_message` whose `model_alias` resolves to a **different vendor**
  than the primary provider's `name` routes through a provider built from
  the resolved record. Every other path — no alias, same-vendor alias,
  test-double primary, resolve failure — keeps the primary provider
  byte-for-byte, so the persona's own turns and every single-vendor overlay
  are untouched. All four lanes (`fast` bid, `summarizer` close, RFC 0051
  critic, memory compression) already pass `model_alias`, so one cut point
  fixes them all.
- `agents/llm_factory.provider_for_resolved`: builds the lane client via the
  same `create_provider` path personas use (identical S-09 key postures),
  cached process-wide **per resolved record** (an alias repoint misses the
  stale entry). A client that cannot be built raises `LaneProviderError` — a
  plain `Exception`, never `SystemExit` — so lane callers degrade
  fail-closed per their own contracts instead of riding the guaranteed-404
  primary.
- Pinned by `agents/tests/test_lane_provider_routing.py` (routing matrix,
  cache identity, failure posture, and an end-to-end Tier B bid on a
  mixed-vendor roster).

**Live-verified 2026-07-22** on the four-vendor stack (governed re-run of
the MT's two slipped legs, NO bypass — blueprint dispositions
`participant`/`chair`, reasoning at the governed default):

- The arc that pre-fix sat **all-silent** (anti-collapse ladder ×5 → chair
  escalation, zero replies) now converges organically: 8 messages, every
  seat authored ≥1, **zero** ladder events, cascade bound → chair
  (`ember-owl`, OpenAI) synthesis final → `interaction closed … 
  trigger=structural` in ~99 s.
- **Governed bidding cross-vendor**: the wallet ledger shows
  `claude-haiku-4-5-20251001` (the Anthropic-pinned `fast`/`summarizer`
  lanes) reconciled against **ember-owl (OpenAI seat) ×2, iron-fox (Gemini
  seat) ×2, slate-heron (watsonx seat) ×2** alongside each seat's own-vendor
  main turns (`gpt-4o` / `gemini-3.5-flash` / `llama-3-3-70b` /
  `claude-sonnet-4-6`) — five physical models, four vendors, one
  interaction.
- **All-N summaries**: all **four** personas' closed-interaction summaries
  real (pre-fix: 1 real + 3 `[interaction summary unavailable]`).
- Zero `model_not_found` / `staying silent` / lease-denial lines across all
  four agent logs; 66,900 tokens ≤ the 200,000 shared cap, ~$0.13 total.

## Notes

> 2026-07-21 — found at the first live four-vendor run (v0.3.11 release-prep
> execution report F-4). The headline MT records Accepted-with-known-gap: the
> cross-vendor discussion + per-seat billing + shared cap proven live under
> the bid-lane bypass; the governed-bidding + all-N-summaries legs slip to the
> fix (the master plan's explicit slip posture — the tag is not held).
