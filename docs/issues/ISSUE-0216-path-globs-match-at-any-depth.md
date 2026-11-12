---
id: ISSUE-0216
summary: "A tool path rule's `*` matches across folder separators, so the shipped `/workspace/*.py` rule allows a write to any `.py` file at any depth. Separately, the deny entry `/workspace/.env` is one literal path and does not cover `/workspace/sub/.env` or `/workspace/.env.local`. The comment beside the shipped rules says they cover the top level only."
status: resolved
severity: medium
area: agents/tools
created: 2026-11-10
closed: 2026-11-12
closed_pr: 1102
refs:
  - agents/tools/sandbox.py
  - config/agents.yaml
  - schemas/agent.schema.json
  - tests/unit/python/test_sandbox.py
  - docs/ai-agents-orchestration-spec.md
---

# ISSUE-0216: Path rules match at any depth, though the shipped config says top level only

## Summary

`PathValidator` (`agents/tools/sandbox.py`) tests a resolved path against
each allow and deny pattern with `fnmatch.fnmatchcase`. In that function
`*` matches any characters, `/` included. A shell would stop `*` at a
folder boundary; this does not.

## Context

With the shipped `code-writer` rules in `config/agents.yaml`:

| Pattern | Path | Matches |
|---|---|---|
| `/workspace/*.py` (write) | `/workspace/deploy/hooks/post_install.py` | yes |
| `/workspace/*.md` (write) | `/workspace/.github/workflows/notes.md` | yes |
| `/workspace/.env` (deny) | `/workspace/sub/.env` | no |
| `/workspace/.env` (deny) | `/workspace/.env.local` | no |

The last two rows are a separate fault. The deny entry has no wildcard,
so it names one file whichever way `*` is matched.

The comment above those rules says the root-level globs "allow the agent
to create top-level files such as setup.py or README.md", and that write
control at the root is governed by the `*.py` and `*.md` globs alone.

The matching rule is stated in neither the schema nor the guides; the
orchestration spec says only "glob patterns".
`tests/unit/python/test_sandbox.py` pins no single-`*` pattern against a
nested path.

## Impact

An operator who reads the example believes writes are limited to named
folders plus top-level files. In fact the agent may write a `.py` or
`.md` file anywhere under the workspace outside a `.git` folder, and a
secrets file that is not exactly `/workspace/.env` is readable.

## Proposed fix / investigation path

Either match segment by segment, so `*` stops at `/` and `**` is the
only way to cross folders, or keep the current rule, document it in the
schema and the spec, and fix the example and its comment. Under either
choice, change the deny example to a pattern that covers nested and
suffixed secrets files, such as `**/.env*`. Add nested-path cases to the
sandbox tests either way.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; the matches above were run with `fnmatch.fnmatchcase`.

> 2026-11-12 — **resolved by [#1102](https://github.com/mkhomutov/Persatrix/pull/1102), which takes the second choice: the
> matching stays, and is now stated.** `*` still matches `/`; nothing in `PathValidator` changed, so
> no operator's rules change meaning. The schema's `read`, `write` and
> `deny` descriptions and the spec's security section state the rule, and
> `test_sandbox.py` pins it. The shipped example now does what its comment
> says: writes go under `src/` and `tests/` and to `setup.py` and
> `README.md`, named exactly, and `**/.env*` denies a `.env` file at any
> depth and with any suffix. That pattern is wider than secrets files: it
> also refuses `.envrc`, a file named `.envoy.py`, and everything under a
> folder whose name starts with `.env`. The maintainer has not ruled out
> the first choice. Making `*` stop at `/` would also narrow every deny
> rule written with one `*`, so it needs a pass over operators' configs.
