---
id: ISSUE-0101
summary: "The persona action parser only matched a block-form ```json\\n..\\n``` fence, so a chair end_interaction_vote emitted as a ONE-LINE fence (```json [..] ```) parsed to nothing and published as raw JSON channel text with no vote metadata — read downstream as an ISSUE-0099 hand-off misfire, causing a visible double-synthesis. Fixed by making the fence extraction tolerant of inline fences / CRLF / stray inner whitespace (possessive quantifiers keep it linear). A sibling of MT-CHANNEL-GOV-004 Edge Case 2."
status: resolved
severity: low
area: agents/persona
created: 2026-06-14
closed: 2026-06-14
refs:
  - docs/manual-tests/MT-CHANNEL-GOV-004.md
  - docs/issues/ISSUE-0099-ce5-ration-spent-on-provably-failed-handoff.md
  - docs/issues/ISSUE-0097-persona-vote-and-bid-calibration.md
  - docs/rfcs/0030-amendment-chair-stall-escalation.md
---

## Summary

`parse_actions` (`agents/persona_runtime/action_parser.py`) extracts the
first fenced JSON action block from an LLM completion. Its fence anchor
required a newline immediately after the language tag and before the
closing fence — `` ```json\n…\n``` `` — so a fence whose markers and body
share a single line, `` ```json [..] ``` `` (spaces, not newlines), did
not match. The branch fell through to the raw-text fallback: the literal
fenced JSON was published as channel text with **no** structured action
and **no** vote metadata.

## Context

Observed live on MT-CHANNEL-GOV-004, branch
`feat/issue-0099-resynthesize-trigger` @ `b817344` (2026-06-14, channel
`group:planning`, interaction `5485a09a`, Run A). The chair
(`nova-sparrow`) cast its forced-turn `end_interaction_vote` action
wrapped in a one-line ` ```json ` fence; it published raw as msg
`5c2e0a61`. Because no vote metadata was set, the orchestrator read the
turn as a provable hand-off misfire (`mentions name no floor-capable
member`) and — correctly, given what it could see — re-forced a
synthesize-only turn under the ISSUE-0099 resynthesize path. The
resynthesize fix recovered the arc into a recorded close, but the **root
cause** was the unparsed fence, and the user-visible symptom was a double
synthesis (the raw-JSON turn, then the re-forced clean one).

This is a sibling of the MT doc's **Edge Case 2: "The chair narrates
instead of voting"** — there the chair emits no vote at all; here the
chair *does* emit a structured vote, but a parser gap demotes it to prose.

## Impact

A correctly-formed `end_interaction_vote` (or any fenced action) that the
model happens to render on one line is silently dropped from the
structured path: the vote loses its metadata, the synthesis leaks onto
the channel as raw JSON, and the orchestrator mis-reads the turn as a
misfire. The ISSUE-0099 re-force masks the worst outcome (the interaction
still closes) but at the cost of a duplicated synthesis and a spurious
`misfired` classification. The same gap would silently drop a one-line
fenced `send_channel_message`, `do_nothing`, etc.

## Proposed fix / investigation path

Resolved in this change. The fence extraction in `parse_actions` now
matches `` ```json `` followed by optional spaces/tabs and an optional
CRLF/newline, a lazy body, then optional trailing whitespace/newline and
the closing `` ``` `` — covering the block form, the one-line form, the
no-separator form, and CRLF. Possessive whitespace quantifiers
(`[ \t]*+`) keep the match linear on pathological many-backtick input,
preserving the polynomial-backtracking guard the original newline anchor
bought (PR #54 review) without the one-line false negative. The
prose-preservation seam (surrounding prose folded into an
`END_INTERACTION_VOTE`) is unaffected — it keys off the same match span.

Regression coverage: `tests/unit/python/test_action_parser_prose.py`
— `test_one_line_fenced_vote_parses_into_structured_vote` and
`test_one_line_fenced_vote_preserves_surrounding_prose`.

## Notes

> 2026-06-14 — captured from MT-CHANNEL-GOV-004 Run A (the "Secondary
> finding (filed, non-blocking)" note on that build's Test Results row).
> Fix + regression tests authored on
> `feat/issue-0099-resynthesize-trigger`; full Python unit suite green
> except 5 pre-existing `test_builtin_tools_filesystem_shell.py`
> shell-exec failures unrelated to this change.
