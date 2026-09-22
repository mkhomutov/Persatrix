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

A read of a row whose key has no session is not a widening and is not
listed: the relationship row (trust, notes, identity) reads the same
from every session with no session predicate at all (ISSUE-0165), and
only its interaction history takes ``sessions``.  That row needs no gate
only while nothing said in a channel reaches the trust score or the
trust note, so :class:`TestTrustWriteSites` below holds the other half
of the rule: it fails when ``update_trust`` or ``apply_decay`` gains a
caller in ``agents/``.  The module docstring of
``agents/persona_runtime/relationship_section.py`` says what such a
caller has to do first.
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

# The two writers of the relationship row's trust score and trust note.
# The row reads across sessions with no §D gate, which is safe only while
# nothing said in a channel reaches either field — that is, while these
# have no caller outside the tier itself (ISSUE-0165).  The aliases are
# the ``as _name`` imports the facade delegates through.
TRUST_WRITERS = frozenset({
    "update_trust", "_update_trust", "apply_decay", "_apply_decay",
})

# The tier's own definition + facade modules are not callers.
_TRUST_WRITER_HOMES = frozenset({
    "agents/memory/relationship.py",
    "agents/memory/relationship_mutations.py",
})

# (module, function) -> what keeps that writer's text out of another
# session's prompt.  Empty today.  Adding an entry is the moment to name
# the RFC 0037 §C write-side rule it follows (write only when the acting
# channel is ``internal`` or below, as the identity write-through does),
# or the read gate that covers the row.
TRUST_WRITE_SITES: dict[tuple[str, str], str] = {}


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
        #: Functions that call a writer of trust / the trust note.
        self.writer_sites: set[str] = set()
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
        if callee in TRUST_WRITERS:
            self.writer_sites.add(".".join(self._scope) or "<module>")
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


def _writer_sites_in(source: str) -> set[str]:
    finder = _SiteFinder()
    finder.visit(ast.parse(source))
    return finder.writer_sites


def _scan_trust_writers(repo_root: Path) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted((repo_root / "agents").rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        if rel.startswith("agents/tests/") or rel in _TRUST_WRITER_HOMES:
            continue
        found |= {
            (rel, site)
            for site in _writer_sites_in(path.read_text(encoding="utf-8"))
        }
    return found


@pytest.fixture(scope="module")
def found() -> set[tuple[str, str]]:
    # ``tests/unit/python/<this>`` → repo root is three parents up.
    return _scan_agents(Path(__file__).resolve().parents[3])


@pytest.fixture(scope="module")
def found_writers() -> set[tuple[str, str]]:
    return _scan_trust_writers(Path(__file__).resolve().parents[3])


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


class TestTrustWriteSites:
    """Nothing said in a channel reaches trust or the trust note.

    That is what lets the relationship row read across sessions without
    the RFC 0037 §D gate (ISSUE-0165).  It is a property of the *writers*,
    so it is checked here rather than left to a docstring.
    """

    def test_no_unlisted_writer(self, found_writers: set[tuple[str, str]]) -> None:
        unlisted = sorted(found_writers - TRUST_WRITE_SITES.keys())
        assert not unlisted, (
            f"{unlisted} write the relationship row's trust or trust note. "
            "That row reads across sessions with no §D gate, so add each to "
            "TRUST_WRITE_SITES with the RFC 0037 §C write-side rule it "
            "follows (write only when the acting channel is 'internal' or "
            "below), or gate the read first — see the module docstring of "
            "agents/persona_runtime/relationship_section.py."
        )

    def test_no_stale_writer_entry(
        self, found_writers: set[tuple[str, str]],
    ) -> None:
        stale = sorted(TRUST_WRITE_SITES.keys() - found_writers)
        assert not stale, f"{stale} no longer write trust — remove them."

    @pytest.mark.parametrize(
        "source",
        [
            "async def f(rel):\n    await rel.update_trust('a', 0.1, 'why')\n",
            "async def f(db):\n    await _update_trust(db, 'a', 'b', 0.1, 'why')\n",
            "async def f(rel):\n    await rel.apply_decay(decay_rate=0.01)\n",
        ],
    )
    def test_finder_flags_a_writer(self, source: str) -> None:
        assert _writer_sites_in(source) == {"f"}

    def test_finder_ignores_a_read(self) -> None:
        assert _writer_sites_in("async def f(rel):\n    await rel.get_trust('a')\n") == set()


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
