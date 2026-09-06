# <Plan or document> amendment — YYYY-MM-DD — <one-line title of the change>

> Status: amendment to `<path to the document being amended>` <and any earlier
> amendment it builds on>.
> Driver: <what forced it — a review finding (PR #), live evidence (report
> §), a released-changelog promise (version) — with the link>.
> Ratified: <merge date and PR of this amendment>.

> Guidance: the text being amended stays verbatim; this document says what
> changes and why. When the amended document is a sequencing doc or an RFC,
> this becomes a dated `## Amendment YYYY-MM-DD — <title>` section appended
> to it instead of a separate file, and the document's reading-order note at
> the top is repointed at it.

## Context

<Two to four paragraphs. What the ratified decision said; what was learned;
why the decision no longer holds as written. Cite the evidence.>

## What changes

| # | Before | After |
|---|--------|-------|
| 1 | <the ratified text or behaviour> | <the new text or behaviour> |
| 2 | … | … |

<One paragraph per row that needs reasoning beyond the table.>

## What does not change

<The neighbouring decisions a reader might assume are affected, stated as
still holding, with the reason.>

## Downstream pointers moved in the same PR

- [ ] ROADMAP rows (<which>)
- [ ] The plan's Master Progress Overview / locks
- [ ] Issue notes (<IDs>) — dated `> YYYY-MM-DD — …` entries
- [ ] RFC status marker / Master Index (if an RFC phase moved)
- [ ] `make issues` / `make rfcs` clean

## Decision / next steps

1. <the first concrete action the amendment enables>
2. …

## Related documentation

- `docs/methodology/decisions.md` §Amendments
- <the amended document; the evidence>
