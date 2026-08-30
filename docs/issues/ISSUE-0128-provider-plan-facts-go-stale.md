---
id: ISSUE-0128
summary: "The model-providers guide states third-party billing facts that only IBM controls — watsonx.ai Runtime plan names, Lite's 20 CUH / 300 000 tokens / 2 req/s limits, and which tiers carry a flat monthly instance fee — dated 'as of August 2026' with no process that re-checks them. The same exposure exists for the alias pricing tables (config/optimization.yaml and every config/demo/*/optimization.yaml), which the missing-price guard checks for presence, never accuracy, and which a lock-step test pins to a checked-in projection. Nothing in CI can detect drift: a vendor price change or plan restructure leaves the docs confidently wrong and the wallet/cap arithmetic quietly mis-scaled, and the failure is silent in both directions."
status: open
severity: low
area: docs/guides
created: 2026-08-13
refs:
  - docs/guides/model-providers.md
  - config/optimization.yaml
  - config/demo/watsonx/optimization.yaml
  - agents/model_aliases.py
  - docs/rfcs/0033-model-alias-layer.md
  - docs/rfcs/0053-gemini-watsonx-providers.md
---

## Summary

We publish numbers that belong to other companies, and nothing re-checks
them.

## Context

Two surfaces carry third-party facts that only the vendor controls.

**1. The watsonx cost callout** in
[`docs/guides/model-providers.md`](../guides/model-providers.md), added by
PR #827. It names IBM's watsonx.ai Runtime plan tiers, states Lite's
published limits (20 CUH, 300 000 tokens/month, 2 requests/second), and
says that Standard and the enterprise tiers carry a flat monthly instance
fee while Essentials does not. It is written defensively — the durable
claim leads (Persatrix meters inference, so non-per-token charges are
outside every cap; that is about our code and cannot go stale), the IBM
specifics are attributed to their published plan documentation, stamped
**as of August 2026**, linked to the live plan page, and explicitly
disclaimed as not ours to be the authority on.

That framing bounds the damage. It does not stop the drift.

**2. The alias pricing tables** — `models.aliases.*.input_per_1m_tokens` /
`output_per_1m_tokens` in [`config/optimization.yaml`](../../config/optimization.yaml)
and each `config/demo/*/optimization.yaml`. These are already declared
operator-maintained in the demo configs' own header comments ("keep them
current with IBM's published rates"), and RFC 0033 §D is explicit that the
missing-price guard ([`agents/model_aliases.py`](../../agents/model_aliases.py))
checks **presence, not accuracy**.

Neither surface has a re-verification trigger. The guide's date stamp is
the only staleness signal anywhere, and it is one a reader has to notice.

## Impact

Silent in both directions, which is what makes it worth tracking rather
than just fixing once:

- **Docs go confidently wrong.** If IBM restructures its plans, renames a
  tier, or changes Lite's allowance, the callout keeps asserting the old
  shape in the same authoritative voice. The date stamp tells a careful
  reader the facts are from August 2026; it does not tell them the facts
  have since changed. The reputational and (per the PR discussion) legal
  exposure of writing about a large vendor's billing is precisely in the
  gap between "was true when written" and "is presented as true now".
- **Cap arithmetic mis-scales.** The alias prices feed the RFC 0023
  wallet, the derived `cost.pricing.models` projection, and every cost
  cap built on them. A vendor price change does not break a build, fail a
  test, or raise a warning — it silently re-scales what a cap means. A
  cap set to bound a demo at $2 bounds it at whatever the stale rate
  implies.
- **The lock-step test does not help.** `TestShippedCostPricingDerivedFromAliases`
  pins the derived table to the alias map, so both stay internally
  consistent while being externally wrong together.

Severity is low because neither failure loses data or breaks a run, and
the guide's framing already limits the blast radius of the doc half.

## Proposed fix / investigation path

The honest options, cheapest first — this issue is the decision, not a
committed design:

1. **A release-checklist row.** Add "re-verify third-party pricing and
   plan facts" to the release-prep checklist, alongside the existing
   gates. Costs one manual check per release, catches drift at a known
   cadence, and needs no new machinery. Probably the right answer.
2. **A dated-claim convention + grep-able marker.** Standardise the
   `as of <month year>` stamp (the callout is currently the only user)
   and add a check that flags any stamp older than N months. Detects
   staleness without knowing the truth — no network, CI-safe.
3. **Live verification.** Fetch vendor pricing pages and diff. Rejected
   as a default: a network-dependent gate that fails on someone else's
   page redesign is worse than the drift it detects.

Whichever lands, the callout's closing "check the current plan page
before you provision" stays — the reader's own check is the last line of
defence and the only one that is current by construction.

## Notes

> 2026-08-13 — filed from the PR #827 review. The PR itself fixed two
> factual errors in the first draft of the callout (a flat-fee claim that
> was wrong for the Essentials tier, and a Lite recommendation that
> omitted Lite's limits), which is the concrete evidence that this class
> of fact is easy to get wrong even when written carefully and checked
> against the vendor's own documentation on the day.
