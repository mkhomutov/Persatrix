---
id: ISSUE-0141
summary: "channel config unset clears a store override to the FLEET default rather than to the value the channel declares in config/channels.yaml, so an operator undoing a temporary override silently lands on a different value than the one they started from"
status: open
severity: low
area: channels
created: 2026-09-06
refs:
  - docs/manual-tests/v0.3.15-execution-report.md
  - docs/rfcs/0050-extensible-channel-configuration.md
  - docs/guides/channels.md
---

## Summary

`persatrix channel config unset` reads as "undo my override". It is not — it
clears the store row and lets the knob inherit the **fleet** default, which is
not necessarily the value the channel's own YAML declares. An operator who sets
a knob temporarily and unsets it afterwards can end up somewhere they have never
been.

## Context

Hit live during the v0.3.15 release-prep arc
([execution report](../manual-tests/v0.3.15-execution-report.md), finding F-3).
Leg 3 needed a temporary `interaction_idle_timeout_seconds` to force a close, so
the value was `set` and then `unset` to restore it. It resolved to:

```
600  [default]
```

`config/channels.yaml` declares **1800** for `planning`. The channel had not been
returned to its declared state; it had been moved to the fleet inherit. It was
restored by `set`ting 1800 explicitly.

This is the [RFC 0050](../rfcs/0050-extensible-channel-configuration.md) export-first
model behaving as designed: once a channel carries store overrides the store is
canonical, and `unset` removes a row rather than re-reading the declaration.
Nothing is broken — but the verb's name promises a different thing than it does.

## Impact

Low severity, sharp edge. The operator sees `[default]` in the output, which is
accurate and easy to read past when the value beside it looks plausible. The
consequence is a governance knob quietly running at the fleet value on a channel
whose YAML says otherwise — and governance knobs are exactly the settings whose
drift is hard to notice, because the symptom is a conversation behaving slightly
differently rather than an error.

The MT itself hit this and would have carried a wrong `interaction_idle_timeout_seconds`
into later legs had the resolved value not been re-read.

## Proposed fix / investigation path

Options, cheapest first — this is a UX decision, not a mechanism one:

1. **Say so in the output.** When `unset` resolves to a value that differs from
   the channel's declared one, print both: `600 [default] (channels.yaml declares
   1800 — use 'set' to restore it)`. No semantics change.
2. **A `--to-declared` flag** (or a distinct `reset` verb) that re-reads
   `config/channels.yaml` and writes that value back as an explicit override.
3. **Change `unset` to mean "restore the declaration"** where one exists. Closest
   to the name, and the most disruptive — it changes a shipped verb's behaviour,
   so it needs a deprecation path.

(1) is worth doing regardless of whether (2) or (3) follows.

## Notes

Filed at v0.3.15 release-prep PR 2 from the PR 1 arc's F-3. Not release-blocking
— behaviour-as-designed, unchanged by this cycle — and carried as a Known Gap on
the [v0.3.15 release checklist](../v0.3.15-release-checklist.md#6-known-gaps-to-document-in-release-notes).
