---
id: ISSUE-0234
summary: "A path rule is matched case-sensitively against the path as the model spelled it. On a file system where names are not case-sensitive (macOS and Windows by default, and a Linux container whose workspace is bind-mounted from one), `file_read .ENV` returns the `.env` file a deny rule names, and `file_write src/.ENV` overwrites `src/.env`. The agent container's own Linux file system is not affected."
status: open
severity: medium
area: agents/tools
created: 2026-11-12
refs:
  - agents/tools/sandbox.py
  - agents/tools/builtin.py
  - config/agents.yaml
  - schemas/agent.schema.json
  - tests/unit/python/test_sandbox.py
  - docker-compose.yaml
  - docs/issues/ISSUE-0216-path-globs-match-at-any-depth.md
  - https://github.com/mkhomutov/Persatrix/pull/1102
---

# ISSUE-0234: Deny rules miss a path spelled in another case

## Summary

`PathValidator` (`agents/tools/sandbox.py`) resolves the path a tool call
gives and matches the result against each rule with `fnmatch.fnmatchcase`.
`Path.resolve()` keeps the letter case it was given, and the match is
case-sensitive. Where the file system treats `.ENV` and `.env` as one
file, the deny rule `**/.env*` does not match `.ENV`, a `read` rule such
as `/workspace/**` does, and the tool opens the denied file.

## Context

Run on macOS (APFS, the default case-insensitive volume) with the real
`file_read` and `file_write` tools, under the shipped `code-writer`
rules with `/workspace` moved to a scratch folder:

| Call | Result |
|---|---|
| `file_read .env` | refused |
| `file_read .ENV` | the contents of `.env` |
| `file_read .Env` | the contents of `.env` |
| `file_read .git/config` | refused |
| `file_read .GIT/config` | the contents of `.git/config` |
| `file_write src/.ENV` | written; `src/.env` now holds the new text |

A second spelling is missed the same way. A deny rule that names
`clé.pem` with a composed `é` refuses that spelling, and the same name
written with `e` plus a combining accent opens the file: the file system
treats the two as one name and the match does not.

The same holds inside a Linux container when the workspace is a folder
bind-mounted from a macOS host: `cat /workspace/.ENV` in an `alpine`
container printed the host's `.env`, while a file the container created
on its own file system could not be opened by a differently-cased name.

Who is affected:

- An agent run directly on macOS or Windows (a contributor's
  `python -m persatrix_agents.server`).
- An agent in a container whose workspace is bind-mounted from a macOS or
  Windows host.

Who is not: the shipped `docker-compose.yaml` gives the agents a named
Linux volume, where names are case-sensitive and the rules hold.

Allow rules are affected in the safe direction only: `/workspace/SRC/x.py`
is not matched by `/workspace/src/**`, so a write the rules allow is
refused. No allow rule is widened.

The comment above `_fnmatch` in `sandbox.py` says case-sensitive matching
was chosen because agents run in Linux containers. That is the reason
the gap was not seen; nothing in the config, the schema or the guides
said the rules assume a case-sensitive file system until the pull request
that filed this issue added one clause beside the shipped deny rules.

Not checked: Windows. Besides letter case, Windows opens a file by a name
with a trailing dot or space, by its short `8.3` name, and with a stream
suffix (`.env::$DATA`). Each would pass a deny rule the same way if
`Path.resolve()` leaves it as given; none was run.

## Impact

A deny rule is the one promise the file rules make without exception
("never read or written"). On an affected host, text that steers the
model can read a secrets file by changing the case of one letter, and can
overwrite it where a write rule covers the folder. `.git/` contents are
reachable the same way. The agent needs no shell tool for this.

## Proposed fix / investigation path

The matcher was out of scope for the pull request that found this. Two
ways, not exclusive:

- Match the path the file system holds, not the one the model typed: for
  each component that exists, take its name from the folder's own listing
  before matching. A new file's name has no stored spelling, so its
  folders are corrected and its own name is not.
- Make deny rules match without regard to case, and to the Unicode form,
  on every platform. On a case-sensitive file system this refuses `.ENV`
  as well as `.env`, which errs on the safe side. Allow rules stay as
  they are.

Either way, add the `.ENV` and combining-accent cases to
`tests/unit/python/test_sandbox.py`, skipped where the file system is
case-sensitive, and the Windows cases above once someone can run them.

## Notes

> 2026-11-12 — found in the review of
> [#1102](https://github.com/mkhomutov/Persatrix/pull/1102), which rewrote
> the shipped deny rules; the fault is older than that pull request and
> the same on `main`. Reproduced on macOS with the real file tools, and
> through a bind mount in a local Linux container. Severity follows
> ISSUE-0216, the nearest like issue; it is the maintainer's to change.
