"""Every place in ``agents/`` that reads across sessions, and what guards it.

A persona's memory is split by session.  Two things lift that split:
the ``sessions="*"`` sentinel
(:data:`agents.memory._session_filter.SESSIONS_ALL`) and the query
helpers that take an already resolved session list, called with none
(:data:`RESOLVED_LIST_HELPERS`), which is how the room-first-ranked
episodic read works.  Since RFC 0049 PR 4 the persona prompt path uses
both, so the F-3 rule is "no *ungated* widening": each such read must be
listed below with the test that holds the rule for it.

The scan reads the syntax tree of every module under ``agents/`` except
``agents/tests/``.  A new cross-session read fails
:class:`TestCrossSessionReadSites` until it is added to
:data:`CROSS_SESSION_READ_SITES` — the moment to name its gate.  A
listed site that no longer reads across sessions fails it too, so the
list cannot go stale.  It sees the sentinel, or a missing list, only
where the call itself spells it: a width worked out elsewhere and passed
in under another name does not show here.  On the prompt path
``tests/integration/test_prompt_path_sessions.py`` checks the width each
tier read actually gets.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# The query helpers that take an already resolved session list, keyed by
# the name of that parameter; ``None`` (or leaving it out) means "no filter".
# The relationship query helpers are left out: they share their names with
# the scoped ``RelationshipMemory`` methods, where leaving it out is the
# §D default.  ``refresh_confidence`` is a write, not a read, but ``None``
# widens it the same way, and an unscoped refresh drops the caller's own
# procedure write.
RESOLVED_LIST_HELPERS: dict[str, str] = {
    "recall_fts5": "sessions",
    "recall_like": "sessions",
    "recall_recency": "sessions",
    "_recall_notes_fts5": "sessions",
    "_recall_notes_like": "sessions",
    "_recall_notes_recency": "sessions",
    "recall_procedures": "session_list",
    "_recall_procedures": "session_list",
    "refresh_confidence": "session_list",
    "_refresh_confidence": "session_list",
    "topic_subjects_for_agent": "session_list",
    "_topic_subjects_for_agent": "session_list",
}

# (module, function) -> the test or policy that keeps its widened read
# out of an ungated prompt.
CROSS_SESSION_READ_SITES: dict[tuple[str, str], str] = {
    ("agents/persona_runtime/memory_context.py",
     "_MemoryContextMixin._inject_memory_context"):
        "the live facts recall, §D-gated before the budget — "
        "test_cross_room_live.py::TestLiveCrossRoomInjection and "
        "tests/integration/test_prompt_path_sessions.py",
    ("agents/memory/episodic_room_ranked.py", "recall_room_ranked"):
        "live: test_cross_room_live.py::TestLiveCrossRoomInjection and "
        "tests/integration/test_prompt_path_sessions.py; "
        "shadow: test_episodes_shadow.py::TestShadowNeverEntersPrompt",
    ("agents/persona_runtime/facts_shadow.py", "_widened_candidates"):
        "log-only — test_facts_shadow.py::TestShadowNeverEntersPrompt",
    ("agents/memory/shared_pool.py", "SharedMemoryPool.read"):
        "cross-session by design (ISSUE-0078 Policy A); no prompt caller",
    ("agents/memory/shared_pool_facade.py", "read_via_facade"):
        "cross-session by design (ISSUE-0078 Policy A); no prompt caller",
    ("agents/memory/shared_pool_facade.py",
     "SharedPoolFacadeMixin.read_from_pool"):
        "cross-session by design (ISSUE-0078 Policy A); no prompt caller — "
        "test_session_recall_default_path.py::"
        "TestFacadeReadFromPoolSessionForwarding",
}

# The sentinel's own definition and resolver are not callers.
_SKIPPED = frozenset({"agents/memory/_session_filter.py"})


def _is_star(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == "*"


def _contains_star(node: ast.AST | None) -> bool:
    return node is not None and any(_is_star(n) for n in ast.walk(node))


def _is_none_or_absent(node: ast.expr | None) -> bool:
    return node is None or (isinstance(node, ast.Constant) and node.value is None)


def _callee_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


class _SiteFinder(ast.NodeVisitor):
    """Collect the dotted name of each function that reads across sessions."""

    def __init__(self) -> None:
        self.sites: set[str] = set()
        self._scope: list[str] = []

    def _hit(self) -> None:
        self.sites.add(".".join(self._scope) or "<module>")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        with_defaults = [
            *zip(positional[len(positional) - len(args.defaults):], args.defaults),
            *zip(args.kwonlyargs, args.kw_defaults),
        ]
        self._scope.append(node.name)
        if any(arg.arg == "sessions" and _is_star(default) for arg, default in with_defaults):
            self._hit()
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "SESSIONS_ALL":
            self._hit()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "SESSIONS_ALL":
            self._hit()
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        keywords = {kw.arg: kw.value for kw in node.keywords}
        if _contains_star(keywords.get("sessions")):
            self._hit()
        callee = _callee_name(node)
        param = RESOLVED_LIST_HELPERS.get(callee) if callee else None
        if param is not None and _is_none_or_absent(keywords.get(param)):
            self._hit()
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        names_sessions = any(
            isinstance(target, ast.Name) and target.id == "sessions"
            for target in node.targets
        )
        if names_sessions and any(_is_star(n) for n in ast.walk(node.value)):
            self._hit()
        self.generic_visit(node)


def _sites_in(source: str) -> set[str]:
    finder = _SiteFinder()
    finder.visit(ast.parse(source))
    return finder.sites


def _scan_agents(repo_root: Path) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted((repo_root / "agents").rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        if rel.startswith("agents/tests/") or rel in _SKIPPED:
            continue
        found |= {(rel, site) for site in _sites_in(path.read_text(encoding="utf-8"))}
    return found


@pytest.fixture(scope="module")
def found() -> set[tuple[str, str]]:
    # ``tests/unit/python/<this>`` → repo root is three parents up.
    return _scan_agents(Path(__file__).resolve().parents[3])


class TestCrossSessionReadSites:
    """The allow-list matches the code, in both directions."""

    def test_no_unlisted_site(self, found: set[tuple[str, str]]) -> None:
        unlisted = sorted(found - CROSS_SESSION_READ_SITES.keys())
        assert not unlisted, (
            f"{unlisted} read across sessions but are not in "
            "CROSS_SESSION_READ_SITES — add each with the test that keeps its "
            "widened read out of an ungated prompt (F-3: no ungated widening)."
        )

    def test_no_stale_entry(self, found: set[tuple[str, str]]) -> None:
        stale = sorted(CROSS_SESSION_READ_SITES.keys() - found)
        assert not stale, f"{stale} no longer read across sessions — remove them."


class TestSiteFinder:
    """The scanner recognises each shape, so a clean scan means something."""

    @pytest.mark.parametrize(
        "source",
        [
            'def f(store):\n    return store.recall(sessions="*")\n',
            'def f(sessions="*"):\n    pass\n',
            'def f(*, sessions: list[str] | str | None = "*"):\n    pass\n',
            'def f(sessions=None):\n    sessions = "*" if sessions is None else sessions\n',
            "def f(store):\n    return store.recall(sessions=SESSIONS_ALL)\n",
            "def f(store):\n    return store.recall(sessions=sf.SESSIONS_ALL)\n",
            "async def f(db):\n    return await recall_fts5(db, sessions=None)\n",
            "async def f(db):\n    return await recall_recency(db)\n",
            "def f(store, all_rooms):\n"
            '    return store.recall(sessions="*" if all_rooms else None)\n',
            "async def f(db):\n    return await recall_procedures(db, 'a')\n",
            "async def f(db):\n    return await _recall_procedures(db, 'a', session_list=None)\n",
            "async def f(db):\n"
            "    return await _refresh_confidence(db, 'a', 'k', session_list=None)\n",
            "async def f(db):\n    return await _recall_notes_recency(db, sessions=None)\n",
            "async def f(db):\n    return await _topic_subjects_for_agent(db, session_list=None)\n",
        ],
    )
    def test_flags_a_cross_session_read(self, source: str) -> None:
        assert _sites_in(source) == {"f"}

    @pytest.mark.parametrize(
        "source",
        [
            "def f(store):\n    return store.recall(sessions=None)\n",
            "async def f(db, session_list):\n"
            "    return await recall_like(db, sessions=session_list)\n",
            "async def f(db, session_list):\n"
            "    return await recall_procedures(db, 'a', session_list=session_list)\n",
            "from agents.memory._session_filter import SESSIONS_ALL\n",
        ],
    )
    def test_ignores_a_room_scoped_read(self, source: str) -> None:
        assert _sites_in(source) == set()

    def test_names_methods_by_class(self) -> None:
        source = 'class Pool:\n    def read(self, sessions="*"):\n        pass\n'
        assert _sites_in(source) == {"Pool.read"}
