---
# Allowed values are documented in README.md. Comments above fields
# (not inline) so that the front-matter parser does not pick them up.
id: ISSUE-NNN
# status: open | in_progress | resolved
status: open
# severity: low | medium | high | critical
severity: low
# area: internal/ package or agent subsystem (cost, persona, memory, grpc, ...)
area: ""
# created: YYYY-MM-DD when the finding was first captured
created: YYYY-MM-DD
# closed: YYYY-MM-DD — set only when status == resolved
# closed_pr: closing PR number (no leading "#"), set only when resolved
refs:
  - docs/rfcs/NNNN-rfc-title.md
  - docs/rfcs/NNNN-pr-plan.md
---

## Summary

One-line description of the finding.

## Context

Where and how it was found. Link to the relevant file or symbol.

## Impact

What breaks or degrades if left unaddressed.

## Proposed fix / investigation path

Optional. Code sketch or pointer to the right package/function.

## Notes

Running notes as investigation continues. Append with date prefixes, e.g.:

> 2026-05-02 — initial capture during PR #72 review.
