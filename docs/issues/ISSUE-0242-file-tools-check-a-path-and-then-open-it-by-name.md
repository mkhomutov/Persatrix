---
id: ISSUE-0242
summary: "`file_read` and `file_write` resolve a path, check it against the agent's allow and deny lists, and then open it by name. If the last part of the path is replaced with a link between the check and the open, the tool reads or writes whatever the link points at, and the lists are not asked again."
status: open
severity: low
area: agents/tools
created: 2026-11-13
refs:
  - agents/tools/builtin.py
  - agents/tools/sandbox.py
  - agents/tools/bounded_io.py
  - docs/issues/ISSUE-0215-tools-buffer-whole-output-before-the-cap.md
  - docs/issues/ISSUE-0234-deny-rules-miss-a-path-spelled-in-another-case.md
  - https://github.com/mkhomutov/Persatrix/pull/1106
---

# ISSUE-0242: The file tools check a path and then open it by name

## Summary

`file_read` and `file_write` (`agents/tools/builtin.py`) call
`PathValidator.validate`, which resolves links and checks the result
against the allow and deny lists, and then open the resolved path by its
name. The check and the open are two steps, and the file system can
change between them.

## Context

Reproduced on `main` at `0f3eb3ac`, and unchanged on the branch that
filed this. With a read allowlist of one folder and a secret file
outside it:

- `file_read` on a file in the folder, with the file replaced by a link
  to the secret after the check returned and before the open, returned
  the secret's text.
- The same call made again, with the link already in place, was refused
  with `Access denied: path is not in read allow list`. So the check
  itself follows links correctly; only the gap is open.

`file_write` has the same two steps (`validate`, then
`resolved.write_text`), and creates missing parent folders in between.

The fix for
[ISSUE-0215](ISSUE-0215-tools-buffer-whole-output-before-the-cap.md)
made `file_read` check on the open file that it is a regular file, so a
pipe cannot be swapped in. It did not close this gap: the allow and deny
lists are still asked about a name, not about the file that was opened.

## Impact

Whoever can change the workspace between the two steps can read a file
the deny list names, or have the agent write outside its allowlist.
That takes a second actor with write access to an allowed folder at the
right moment: a command the agent itself runs through `shell_exec`, or
another process sharing the volume. An agent that can already run
arbitrary commands gains little from it, which is why this is proposed
low; an agent with file tools only, on a volume another writer shares,
is the case that matters.

The severity is proposed here, not ruled.

## Proposed fix / investigation path

Open first, then check what was opened: open the path without following
a final link (`O_NOFOLLOW`), walk its folders by descriptor where the
platform allows it, and compare the opened file's real location with the
lists. Windows needs its own answer. A test can make the swap the way
the reproduction did, by wrapping the validator.

## Notes

> 2026-11-13 — found by an independent review of the fix for
> [ISSUE-0215](ISSUE-0215-tools-buffer-whole-output-before-the-cap.md).
> On `main` already; that fix neither caused nor closed it.
