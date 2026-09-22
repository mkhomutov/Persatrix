"""A memory tier reads its start-up scope only through a call-time resolver.

Each tier keeps the session, tenant (principal) and epoch it was built
with, in ``_active_session_id`` / ``_session_id``, ``_active_principal_id``
/ ``_principal_id`` and ``_active_epoch_id`` / ``_epoch_id``.  They are
only the fallback: a turn binds its own with ``session_scope``,
``principal_scope`` and ``epoch_scope``, and every read and write must
prefer that.  So the snapshot goes to a resolver
(``_resolve_session_list``, ``resolve_active_principal``,
``resolve_active_epoch``, ``resolve_write_principal``) or sits behind
``current_session_id() or …`` on its own axis.  Putting it straight into a
query is the `ISSUE-0164
<../../../docs/issues/ISSUE-0164-persona-cannot-edit-or-delete-its-channel-notes.md>`_
bug, which single-session tests cannot see: there the snapshot and the
bound value are the same.

The scan reads the syntax tree of every module under ``agents/`` except
``agents/tests/``.  Any other read of a snapshot fails
:class:`TestScopeSnapshotReads` until it is listed in
:data:`SNAPSHOT_READ_SITES` with the reason it is safe, and a listed site
that no longer reads its snapshot fails it too, so the list cannot go stale.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

# Each snapshot attribute, and the call-time reader of the same axis.
SNAPSHOTS: dict[str, str] = {
    "_active_session_id": "current_session_id",
    "_session_id": "current_session_id",
    "_active_principal_id": "current_principal_id",
    "_principal_id": "current_principal_id",
    "_active_epoch_id": "current_epoch_id",
    "_epoch_id": "current_epoch_id",
}

# Each resolver, and the position and name of its fallback argument.
RESOLVERS: dict[str, tuple[int, str]] = {
    "_resolve_session_list": (1, "active_session_id"),
    "resolve_active_principal": (0, "snapshot"),
    "resolve_active_epoch": (0, "snapshot"),
    "resolve_write_principal": (1, "snapshot"),
}

# (module, function, snapshot) -> why reading the snapshot itself is right.
SNAPSHOT_READ_SITES: dict[tuple[str, str, str], str] = {
    ("agents/memory/episodic.py", "EpisodicMemory.initialize", "_active_session_id"):
        "handed to the NoteStore it builds, which resolves it per call",
    ("agents/memory/episodic.py", "EpisodicMemory.initialize", "_active_principal_id"):
        "handed to the NoteStore it builds, which resolves it per call",
    ("agents/memory/episodic.py", "EpisodicMemory.initialize", "_active_epoch_id"):
        "handed to the NoteStore it builds, which resolves it per call",
    ("agents/memory/store.py", "MemoryStore.session_id", "_session_id"):
        "the public property that reports the snapshot itself",
    ("agents/persona_runtime/state_persistence.py",
     "_StatePersistenceMixin.initialize_memory", "_session_id"):
        "start-up: nothing is bound yet, so a resolver would return the same value",
}


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _resolved(node: ast.Attribute, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when the read is a resolver's fallback, or the right-hand side
    of ``current_<axis>_id() or …`` on its own axis."""
    parent = parents.get(node)
    if isinstance(parent, ast.Call):
        spec = RESOLVERS.get(_callee(parent) or "")
        return spec is not None and parent.args[spec[0]:spec[0] + 1] == [node]
    if isinstance(parent, ast.keyword):
        call = parents.get(parent)
        spec = RESOLVERS.get(_callee(call) or "") if isinstance(call, ast.Call) else None
        return spec is not None and parent.arg == spec[1]
    if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.Or):
        first = parent.values[0]
        return (
            parent.values[-1] is node
            and isinstance(first, ast.Call)
            and _callee(first) == SNAPSHOTS[node.attr]
        )
    return False


def _function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return ".".join(reversed(names)) or "<module>"


def _raw_reads(source: str) -> Counter[tuple[str, str]]:
    """(function, snapshot) for each snapshot read no resolver wraps."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    reads: Counter[tuple[str, str]] = Counter()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in SNAPSHOTS
            and isinstance(node.ctx, ast.Load)
            and not _resolved(node, parents)
        ):
            reads[(_function(node, parents), node.attr)] += 1
    return reads


@pytest.fixture(scope="module")
def found() -> Counter[tuple[str, str, str]]:
    # ``tests/unit/python/<this>`` → the repo root is three parents up.
    repo_root = Path(__file__).resolve().parents[3]
    reads: Counter[tuple[str, str, str]] = Counter()
    for path in sorted((repo_root / "agents").rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        if rel.startswith("agents/tests/"):
            continue
        for (function, attr), n in _raw_reads(path.read_text(encoding="utf-8")).items():
            reads[(rel, function, attr)] += n
    return reads


class TestScopeSnapshotReads:
    """The allow-list matches the code, in both directions."""

    def test_no_unlisted_read(self, found: Counter[tuple[str, str, str]]) -> None:
        unlisted = sorted(found.keys() - SNAPSHOT_READ_SITES.keys())
        assert not unlisted, (
            f"{unlisted} read a scope snapshot directly: pass it to a call-time "
            "resolver (ISSUE-0164), or list it here with the reason it is safe."
        )

    def test_each_listed_site_reads_once(
        self, found: Counter[tuple[str, str, str]],
    ) -> None:
        repeated = sorted(site for site, n in found.items() if n > 1)
        assert not repeated, f"{repeated} read the same snapshot more than once."

    def test_no_stale_entry(self, found: Counter[tuple[str, str, str]]) -> None:
        stale = sorted(SNAPSHOT_READ_SITES.keys() - found.keys())
        assert not stale, f"{stale} no longer read a snapshot directly — remove them."


class TestRawReadFinder:
    """The scanner tells a resolved read from a raw one."""

    @pytest.mark.parametrize(
        "source",
        [
            "def f(self):\n    return _resolve_session_list(None, self._active_session_id)\n",
            "def f(self):\n"
            "    return _resolve_session_list(None, active_session_id=self._active_session_id)\n",
            "def f(self):\n    return resolve_active_principal(self._active_principal_id)\n",
            "def f(self):\n    return resolve_write_principal(None, self._principal_id)\n",
            "def f(self):\n    return current_session_id() or self._session_id\n",
        ],
    )
    def test_ignores_a_resolved_read(self, source: str) -> None:
        assert _raw_reads(source) == Counter()

    @pytest.mark.parametrize(
        ("source", "read"),
        [
            ("async def f(self, db):\n    await db.execute('q', (self._active_session_id,))\n",
             ("f", "_active_session_id")),
            ("def f(self):\n    return current_principal_id() or self._active_epoch_id\n",
             ("f", "_active_epoch_id")),
            ("def f(self):\n    return _resolve_session_list([self._session_id], 'x')\n",
             ("f", "_session_id")),
            ("class Store:\n    def f(self):\n        return self._epoch_id\n",
             ("Store.f", "_epoch_id")),
        ],
    )
    def test_flags_a_raw_read(self, source: str, read: tuple[str, str]) -> None:
        assert _raw_reads(source) == Counter({read: 1})
