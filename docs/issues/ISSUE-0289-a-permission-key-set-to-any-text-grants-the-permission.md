---
id: ISSUE-0289
summary: "The permission gate grants a permission when its key in the agent's config holds any truthy value, not only the list or `true` the agent schema allows: `recall: \"false\"`, `write: \"no\"` and `read: 1` all grant. `make validate` refuses such a config, but nothing checks the schema when an agent starts, so a quoted `\"false\"` turns a tool on."
status: open
severity: low
area: agents/tools
created: 2026-11-26
refs:
  - agents/tools/permissions.py
  - agents/tools/tool_list.py
  - agents/server_persona.py
  - schemas/agent.schema.json
  - docs/issues/ISSUE-0214-declared-tool-permissions-are-never-enforced.md
  - docs/issues/ISSUE-0150-schema-valid-config-cannot-enable-shell-exec-or-http-request.md
  - https://github.com/mkhomutov/Persatrix/pull/1148
---

# ISSUE-0289: A permission key set to any text grants the permission

## Summary

`PermissionGate.check` (`agents/tools/permissions.py`) grants a
permission when the key that grants it "holds a truthy value". The agent
schema allows two kinds of value there: a list of rules, or `true`. The
gate does not look at the kind. Any text that is not empty, and any
number but zero, grants.

## Context

Run on 2026-11-26 against the gate and the schema's `permissions` block:

| The agent's `permissions` block | Asked | Gate | Schema |
|---|---|---|---|
| `channels: {recall: "false"}` | `channels:recall` | granted | refused |
| `memory: {write: "no"}` | `memory:write` | granted | refused |
| `memory: {read: 1}` | `memory:read` | granted | refused |
| `filesystem: {read: "no"}` | `filesystem:read` | granted | refused |
| `network: {allow: "*"}` | `network:http` | granted | refused |
| `channels: {recall: false}` | `channels:recall` | denied | accepted |

`make validate` checks a config against the schema, and CI runs it on
the shipped files. An agent that starts does not: `load_agent`
(`agents/server_persona.py`) reads the YAML and hands the `permissions`
block to the gate as it is. So the schema's refusal protects a
deployment only where the operator runs the validator.

The three boolean keys are where it bites. `memory.read`, `memory.write`
and `channels.recall` have no second check behind them: a note tool or
the recall tool runs once the gate says yes. For the list keys a second
check still stands in the way: the file tools match the path against
the rules, `shell_exec` matches the command and `http_request` the
host, and a text where a list should be matches almost nothing.

This is not new. The built-in tools got the same answer from the gate
before the declared-permission check existed. That check
([ISSUE-0214](ISSUE-0214-declared-tool-permissions-are-never-enforced.md))
made the gate the admission for every tool a model calls, a custom
tool's included, which is why it was looked at again. YAML reads an
unquoted `false`, `no` or `off` as false, so the fault needs the quotes
or a number.

## Impact

Proposed low. A config has to be wrong in a way the validator reports,
and the wrong value has to be one a person reads as "off". When both
hold, a persona keeps a memory or recall tool its operator meant to
take away, and nothing says so. The severity is proposed here, not
ruled.

## Proposed fix / investigation path

Grant only on the kinds the schema allows: `true` for the three boolean
keys, a non-empty list for the four list keys. Anything else denies,
with a warning that names the key, since a deny that nobody can explain
is its own fault.

That is a behaviour change and the maintainer's to choose: a deployment
whose config holds `read: 1` or a quoted `"true"` works today and would
stop. The other half would close the gap from the other side: check the
agent's config against the schema when the agent starts, as
`make validate` does, and refuse to start on an error.

## Notes

> 2026-11-26 — found by the review of
> [#1148](https://github.com/mkhomutov/Persatrix/pull/1148), which leaves
> the gate's answer here as it was and corrects the docstring of
> `check()`: it said "a non-empty list or True" where the code grants on
> any truthy value. Older than that pull request.
