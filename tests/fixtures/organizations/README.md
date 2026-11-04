# Organization fixtures

Shared test input for the checkers of `config/organizations.yaml`
([RFC 0012](../../../docs/rfcs/0012-protocols-organizations.md) §B): the
Python validator `make validate` runs (`agents/org_validate.py`, PR 1),
and the Go loader (`internal/protocols/`) that PR 2 is to test on the same
files, so the two are held to the same rules. Only these cases are shared:
where the YAML parsers or regex engines differ, add a fixture here.

- `agents.yaml` — the agents the fixtures name: five personas and one task
  agent. It stands in for `config/agents.yaml`.
- `valid/` — each file passes.
- `invalid/` — each file fails for one rule only. The file name names the
  rule: the part before any `--`. The message starts with that name, such
  as `second-manager: …`. A `schema-<key>` file breaks a shape the JSON
  Schema checks with its own message, which is at `<key>` or names it.
