"""
Organization graph checks (RFC 0012 §B, PR 1).

``schemas/organization.schema.json`` checks the shape of
``config/organizations.yaml``; this module checks the graph it declares,
which a JSON Schema cannot: ids that must be unique, references that must
resolve, the rules each topology sets, no loop in ``manages``, and the
members against ``agents.yaml``. It runs only on a file the schema accepted.

Each message starts with the name of the rule it breaks, such as
``second-manager:``. The fixtures in ``tests/fixtures/organizations/`` hold
one file per rule; RFC 0012 PR 2's Go loader is to be tested on the same
files, so the two checkers are held to the same rules.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterator, Mapping
from pathlib import Path
from typing import Any, NamedTuple

import yaml


class GraphError(NamedTuple):
    """One broken rule: where it is (a dotted path) and what it is."""

    path: str
    message: str


class _RepeatedKeyError(Exception):
    def __init__(self, key: object, line: int) -> None:
        super().__init__(key, line)
        self.key = key
        self.line = line


class _UniqueKeyLoader(yaml.SafeLoader):
    """A loader that refuses a key given twice in one mapping. PyYAML keeps
    the last value, so the schema pass may have checked a graph the file
    does not plainly say; Go's YAML parser refuses such a file."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise _RepeatedKeyError(key, key_node.start_mark.line + 1)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def check_file(organizations_path: Path, agents_path: Path) -> list[GraphError]:
    """Check an organizations file the schema has accepted."""
    text = organizations_path.read_text(encoding="utf-8")
    try:
        doc = yaml.load(text, Loader=_UniqueKeyLoader)
    except _RepeatedKeyError as repeated:
        return [
            GraphError(
                "",
                f"duplicate-key: {repeated.key!r} is given twice in one mapping "
                f"(line {repeated.line}); YAML would keep only the last",
            )
        ]
    if not doc:
        return []
    agents = agent_types(agents_path)
    if agents is None:
        return [
            GraphError(
                "",
                f"unknown-agent: {agents_path.name} is missing or unreadable, "
                "so no member can be checked",
            )
        ]
    return check_organizations(doc, agents)


def agent_types(agents_path: Path) -> dict[str, str] | None:
    """Each agent id in ``agents.yaml`` with its type, ``task`` by default;
    None when the file is missing, unreadable or lists no agents."""
    try:
        content = yaml.safe_load(agents_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    agents = content.get("agents") if isinstance(content, dict) else None
    if not isinstance(agents, list):
        return None
    return {
        agent["id"]: agent.get("type", "task")
        for agent in agents
        if isinstance(agent, dict) and isinstance(agent.get("id"), str)
    }


def check_organizations(doc: Mapping[str, Any], agents: Mapping[str, str]) -> list[GraphError]:
    """Every broken graph rule in a schema-valid organizations document."""
    errors: list[GraphError] = []
    org_ids: set[str] = set()
    home: dict[str, str] = {}  # each persona's first organization
    for i, org in enumerate(doc["organizations"]):
        at = f"organizations.{i}"
        if org["id"] in org_ids:
            errors.append(
                GraphError(f"{at}.id", f"duplicate-organization: {org['id']!r} is declared twice")
            )
        org_ids.add(org["id"])
        errors.extend(_check_members(org, at, agents, home))
        errors.extend(_check_edges(org, at))
    return errors


def _check_members(
    org: Mapping[str, Any], at: str, agents: Mapping[str, str], home: dict[str, str]
) -> Iterator[GraphError]:
    org_id = org["id"]
    roles: set[str] = set()
    for j, role in enumerate(org["roles"]):
        if role["id"] in roles:
            yield GraphError(
                f"{at}.roles.{j}.id",
                f"duplicate-role: {role['id']!r} is declared twice in {org_id!r}",
            )
        roles.add(role["id"])

    members: set[str] = set()
    for j, member in enumerate(org["members"]):
        where = f"{at}.members.{j}"
        agent = member["agent_id"]
        if agent in members:
            yield GraphError(where, f"duplicate-member: {agent!r} is listed twice in {org_id!r}")
            continue
        members.add(agent)
        if member["role"] not in roles:
            yield GraphError(
                f"{where}.role",
                f"unknown-role: {agent!r} holds {member['role']!r}, "
                f"a role {org_id!r} does not declare",
            )
        kind = agents.get(agent)
        if kind is None:
            yield GraphError(
                f"{where}.agent_id", f"unknown-agent: {agent!r} is not an agent in agents.yaml"
            )
        elif kind != "persona":
            yield GraphError(
                f"{where}.agent_id",
                f"not-a-persona: {agent!r} is a {kind} agent; only a persona joins an organization",
            )
        first = home.setdefault(agent, org_id)
        if first != org_id:
            yield GraphError(
                where,
                f"second-organization: {agent!r} is a member of {first!r} and {org_id!r}; "
                "a persona belongs to one organization",
            )


def _check_edges(org: Mapping[str, Any], at: str) -> list[GraphError]:
    errors: list[GraphError] = []
    org_id, topology = org["id"], org["topology"]
    members = {member["agent_id"] for member in org["members"]}
    edges: set[tuple[str, str, str]] = set()
    manager_of: dict[str, str] = {}  # a hierarchy member's first manager
    reports: dict[str, list[str]] = {}  # the edges the loop check follows
    for j, member in enumerate(org["members"]):
        manager = member["agent_id"]
        for k, edge in enumerate(member.get("manages", [])):
            where = f"{at}.members.{j}.manages.{k}"
            report, scope = _edge(edge)
            if report not in members:
                errors.append(
                    GraphError(
                        where,
                        f"unknown-member: {manager!r} manages {report!r}, "
                        f"who is not a member of {org_id!r}",
                    )
                )
                continue
            if (manager, report, scope) in edges:
                in_scope = f" in scope {scope!r}" if scope else ""
                errors.append(
                    GraphError(
                        where, f"duplicate-edge: {manager!r} manages {report!r}{in_scope} twice"
                    )
                )
                continue
            edges.add((manager, report, scope))
            if topology == "flat":
                errors.append(
                    GraphError(
                        where,
                        f"flat-manages: {manager!r} manages {report!r}, "
                        "but in a flat organization no member manages another",
                    )
                )
                continue
            if topology == "matrix" and not scope:
                errors.append(
                    GraphError(
                        where,
                        f"unscoped-matrix-edge: {manager!r} manages {report!r} with no scope; "
                        "each matrix edge names its dimension",
                    )
                )
            if topology == "hierarchy" and scope:
                errors.append(
                    GraphError(
                        where,
                        f"scoped-hierarchy-edge: {manager!r} manages {report!r} "
                        f"in scope {scope!r}; a hierarchy manager directs on every "
                        "matter, so its edges take no scope",
                    )
                )
            first = manager_of.setdefault(report, manager)
            if topology == "hierarchy" and first != manager:
                errors.append(
                    GraphError(
                        where,
                        f"second-manager: {report!r} is managed by {first!r} and {manager!r}; "
                        "in a hierarchy a member has at most one manager",
                    )
                )
            reports.setdefault(manager, []).append(report)

    loop = _find_loop(reports)
    if loop:
        errors.append(
            GraphError(
                f"{at}.members",
                f"cycle: {' → '.join(loop)}; no member may manage someone "
                "who manages them, directly or through others",
            )
        )
    return errors


def _edge(edge: str | Mapping[str, str]) -> tuple[str, str]:
    """An edge's report and scope. No scope is the empty label, as the
    society tables store an unscoped edge."""
    if isinstance(edge, str):
        return edge, ""
    return edge["agent_id"], edge.get("scope", "")


def _find_loop(reports: Mapping[str, list[str]]) -> list[str]:
    """The first loop in ``manages``, whatever the edges' scopes, as a path
    that ends where it starts; empty when there is none.

    Iterative, so a long management chain cannot exhaust the stack.
    """
    done: set[str] = set()
    for root in reports:
        if root in done:
            continue
        path = [root]
        branches = [iter(reports[root])]
        while branches:
            nxt = next(branches[-1], None)
            if nxt is None:
                branches.pop()
                done.add(path.pop())
            elif nxt in path:
                return [*path[path.index(nxt) :], nxt]
            elif nxt not in done:
                path.append(nxt)
                branches.append(iter(reports.get(nxt, ())))
    return []
