---
id: ISSUE-0113
summary: "The four-vendor blueprint's shared role lanes cannot run cross-vendor on the shipped one-client-per-agent runtime: the RFC 0051/0030 salience-bid lane (`fast`) and the RFC 0020 close-summary lane (`summarizer`) resolve the alias's MODEL NAME but send it through the persona's single LLMClient (built from its seat alias's provider) — a non-matching seat gets a provider 404 (e.g. the OpenAI seat asked OpenAI for `claude-haiku-4-5-20251001`), the bid fails closed to silence, and the whole four-vendor roster sits mute through every governed round. Live workaround: `respond: always` + `reasoning.mode: off` bypass the bid lane (discussion runs four-vendor); non-matching seats' close summaries still degrade to the placeholder. Fix direction: lanes construct/cache a provider from the RESOLVED alias record when it differs from the agent's own (llm_factory + a per-provider client cache), or the blueprint drops shared lanes for per-seat ones."
status: open
severity: medium
area: agents
created: 2026-07-21
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

## Notes

> 2026-07-21 — found at the first live four-vendor run (v0.3.11 release-prep
> execution report F-4). The headline MT records Accepted-with-known-gap: the
> cross-vendor discussion + per-seat billing + shared cap proven live under
> the bid-lane bypass; the governed-bidding + all-N-summaries legs slip to the
> fix (the master plan's explicit slip posture — the tag is not held).
