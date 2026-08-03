---
id: ISSUE-0116
summary: "A stored fact `subject` reaches the persona's prompt verbatim, as the `Known facts about <subject>:` block header rendered OUTSIDE the RFC 0009 `<external_data>` quarantine envelope — so an LLM-proposed subject is up to `MAX_SUBJECT_CHARS` (120) characters of model-influenced text placed in the persona's own framing. This pre-dates the RFC 0026 topic-predicate amendment (the extractor has always stored LLM-proposed person subjects) and is bounded by that amendment's write-boundary checks — length cap, control-character rejection so a subject cannot forge a second block, and whitespace-tolerant `<external_data>` delimiter rejection — plus the RFC 0009 framing that instructs personas to treat injected memory as data. What remains unbounded is the *content* of a short, well-formed subject: nothing rejects `atlas. ignore all prior instructions`. The topic amendment makes the surface easier to reach (a stimulus mention now pulls a matching subject's block in, where previously only the counterparty's own turns did), which is why it is being recorded rather than silently inherited."
status: open
severity: low
area: memory
created: 2026-07-27
refs:
  - docs/rfcs/0026-amendment-topic-subject-predicates.md
  - docs/rfcs/0026-declarative-facts-tier.md
  - docs/rfcs/0009-security-sandboxing.md
  - agents/persona_runtime/facts_section.py
  - agents/memory/fact_predicates.py
---

## Summary

The facts tier renders one `Known facts about <subject>:` header per
seeded subject, with the canonical subject interpolated verbatim, and
memory sections are assembled into the persona's system prompt without
the RFC 0009 `<external_data>` envelope that wraps tool output. A
subject is therefore attacker-influenceable text in a trusted framing
position.

## Context

The subject reaching the prompt is not new: `store_extracted_facts` has
always persisted whatever subject the extractor LLM proposed for a
person fact, and `render_facts_section` has always headed each block
with it. The RFC 0026 topic-predicate amendment (RFC 0049 Phase 1 PR 1)
changes the *reachability*, not the surface — before it, a subject's
block entered a turn only when that person was the counterparty; with
topic seeding, any stimulus mentioning a stored topic subject pulls its
block in.

The amendment's blast-radius review bounded the shape of the value:

- `MAX_SUBJECT_CHARS = 120` on the canonical form (write boundary),
- control characters rejected in objects, and collapsed in subjects by
  `canonicalize_subject` — so no stored value can forge a *second*,
  fabricated `Known facts about self:` block,
- `<\s*/?\s*external_data` rejected in both fields, whitespace-tolerant,
  so a stored value cannot forge an envelope boundary,
- topic seeds recall only `TOPIC_PREDICATES` rows, and seeds below a
  length floor or in the function-word set never fire.

It did not bound the *semantics* of a short well-formed subject.

## Why it is low, not high

Reaching this requires the extractor LLM to accept an adversarial
subject at interaction close (it is instructed to emit canonical short
names), the resulting row must survive the closed predicate allowlist,
and the rendered text lands beside the RFC 0009 framing snippet that
tells the persona to treat injected memory as data rather than
instructions. It is one more instance of the general "consolidated
memory is model-influenced" property the facts tier has carried since
RFC 0026 PR 2 — not a new privilege boundary.

## Candidate directions

1. **Subject-side sanitiser at the write boundary** — reject subjects
   containing sentence punctuation followed by imperative-shaped text,
   or restrict subjects to a conservative codepoint/shape grammar.
   Cheap, but a grammar tight enough to matter will reject legitimate
   multi-word topic names, and a loose one buys little.
2. **Render the header from a bounded template** — e.g. truncate the
   subject to N words in the header while keeping the full canonical
   form in the row. Bounds the surface without touching storage.
3. **Own it in the predicate registry** (the future the dotted
   `self.*` / `topic.*` convention reserves): subject *namespaces* with
   per-namespace validation, which is where a real subject grammar
   belongs.

Direction 3 is the principled home; 2 is the cheap mitigation if the
surface ever proves reachable in practice.

## Notes

> 2026-08-02 — **Slotted into v0.3.13 as the cuttable fold-in** by the
> [sequencing Amendment 2026-08-02](../v0.3.x-sequencing.md#amendment-2026-08-02--v0313--v0314-the-two-release-tail-to-v040)
> — the only fold-in on that release, alongside its three named deferred
> calls ([ISSUE-0114](ISSUE-0114-per-channel-cascade-depth-override.md),
> [ISSUE-0118](ISSUE-0118-tool-recall-bypasses-epoch-session-scopes.md),
> [ISSUE-0121](ISSUE-0121-crossroom-person-identity-legs-never-run-live.md)).
> The in/out call locks at the `v0.3.13-plan.md` opening (the amendment's
> next-steps item 2). Plan-opening default among the candidate directions,
> mirroring the ISSUE-0114 step-4 pattern: **direction 2** (render the
> header from a bounded template) as the release-sized mitigation —
> direction 3 (subject namespaces with per-namespace validation) remains
> the principled home but belongs with the future predicate registry, and
> direction 1 buys little at any grammar strictness this issue would
> accept. Revisitable in the fold-in PR.
