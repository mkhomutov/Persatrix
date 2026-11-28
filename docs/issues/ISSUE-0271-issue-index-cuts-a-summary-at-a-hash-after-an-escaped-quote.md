---
id: ISSUE-0271
summary: "The reader that builds the issue and RFC indexes decides where a comment starts by counting quotation marks, and counts an escaped one. A summary in double quotes that holds an escaped quotation mark and, later, a hash sign is cut at the hash sign, so its row in the index ends in the middle of a sentence. The check that guards the index compares it with the same reader's output and passes."
status: open
severity: low
area: scripts
created: 2026-11-16
refs:
  - scripts/_doc_index.py
  - scripts/issues.py
  - docs/issues/INDEX.md
  - docs/issues/ISSUE-0267-extends-template-references-are-documented-and-not-built.md
  - https://github.com/mkhomutov/Persatrix/pull/1133
---

# ISSUE-0271: The issue index cuts a summary at a hash sign after an escaped quote

## Summary

`scripts/_doc_index.py` reads the front matter of issue and RFC files
without a YAML library. `strip_inline_comment` walks a value character
by character, flips "inside double quotes" at every `"` and treats a
`#` outside quotes as the start of a comment.

It does not know about the backslash. In a double-quoted value, `\"`
is a quotation mark that belongs to the text; the reader takes it for
the end of the quoted part. A `#` that follows is then "outside quotes"
and everything from it on is dropped. `unquote` afterwards finds no
closing quote, so the value keeps its opening one.

## Context

Seen in [#1133](https://github.com/mkhomutov/Persatrix/pull/1133): the first summary of
[ISSUE-0267](ISSUE-0267-extends-template-references-are-documented-and-not-built.md)
quoted a config value that holds a hash sign, inside escaped quotation
marks. Its row in `docs/issues/INDEX.md` ended at
`"templates/personas.yaml`, in the middle of the sentence. That
summary is reworded; the reader is unchanged.

- `scripts/issues.py --check`, which CI runs as `make issues-check`,
  regenerates the index with the same reader and compares. A cut row
  is what the reader produces, so the check passes.
- A YAML parser reads the same line whole. Only the generated index is
  wrong; the issue file is not.
- The reader does not undo escapes either. It shows `\"` and a
  doubled backslash as they are written, where YAML means one
  character each.
- What this does to today's index was checked on the pull request's
  branch: each of the 261 summaries was read with a YAML parser and
  compared with the reader's value. No other row is cut. Twelve rows
  show an escape as written: ten a backslash before a quotation mark,
  two a doubled backslash.

## Impact

A row of the index, which is what a person or an assistant reads first,
can say half of what the issue says, with no check failing; twelve
rows carry stray backslashes today. Proposed low: a cut takes an
escaped quotation mark followed by a hash sign in one line, and the
file itself is right. The severity is proposed here, not
ruled.

## Proposed fix / investigation path

In `strip_inline_comment`, skip the character after a backslash while
inside double quotes, and have `unquote` turn `\"` into `"` and `\\`
into `\`. A test with a summary that holds both. Then let the check
compare with a YAML parse of each summary too, so that a reader fault
cannot hide behind its own output.

## Notes

> 2026-11-16 — found by the review of [#1133](https://github.com/mkhomutov/Persatrix/pull/1133).
> Filed there; the reader is not changed by that pull request.

> 2026-11-28 — severity ruled `low`, as proposed: the maintainer's
> ruling at the
> [strategy review of 2026-11-28](../project-strategy-review-2026-11-28.md#3-the-maintainers-rulings),
> ratified by ruling (ab) of the
> [sequencing Amendment 2026-11-28](../v0.4.x-sequencing-2026-11-28.md#rulings).
