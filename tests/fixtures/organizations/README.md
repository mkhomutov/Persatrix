# Organization fixtures

Shared test input for the two checkers of `config/organizations.yaml`
([RFC 0012](../../../docs/rfcs/0012-protocols-organizations.md) §B): the
Python validator `make validate` runs (`agents/org_validate.py`, PR 1) and
the Go loader (`internal/protocols/`, PR 2). Both read these files, so they
cannot drift.

- `agents.yaml` — the agents the fixtures name: five personas and one task
  agent. It stands in for `config/agents.yaml`.
- `valid/` — each file passes.
- `invalid/` — each file fails for one rule only. The file name names the
  rule: the part before any `--`. A graph rule's message starts with its
  name, such as `second-manager: …`. A `schema-<key>` file breaks the shape
  the JSON Schema checks, and its message names `<key>`.
