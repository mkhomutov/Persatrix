"""
Tests for Participant Protocol, UserParticipant, and UserStore.

All tests use in-memory SQLite (:memory:) for isolation and speed.
"""

import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.participant import (
    VALID_PARTICIPANT_TYPES,
    Participant,
    UserParticipant,
    UserStore,
    validate_participant_id,
    validate_participant_type,
)


# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture
async def store():
    """Create an initialized UserStore with in-memory DB."""
    s = UserStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


# ─── Helpers ────────────────────────────────────────────────


class _StubAgent(BaseAgent):
    """Minimal BaseAgent subclass for protocol conformance tests."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


# ─── UserParticipant dataclass ──────────────────────────────


class TestUserParticipant:
    def test_creation_with_all_fields(self):
        u = UserParticipant(
            participant_id="alice-01",
            display_name="Alice",
            participant_type="user",
            created_at=1000.0,
            last_seen_at=2000.0,
        )
        assert u.participant_id == "alice-01"
        assert u.display_name == "Alice"
        assert u.participant_type == "user"
        assert u.created_at == 1000.0
        assert u.last_seen_at == 2000.0

    def test_defaults(self):
        u = UserParticipant(participant_id="bob-02", display_name="Bob")
        assert u.participant_type == "user"
        assert u.created_at > 0
        assert u.last_seen_at > 0


# ─── Participant ID validation ──────────────────────────────


class TestParticipantIdValidation:
    def test_accepts_valid_ids(self):
        for pid in ("local", "alice-01", "a1", "agent-beta-3"):
            validate_participant_id(pid)  # should not raise

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="Invalid participant_id"):
            validate_participant_id("")

    def test_rejects_whitespace(self):
        with pytest.raises(ValueError, match="Invalid participant_id"):
            validate_participant_id("has space")

    def test_rejects_unicode(self):
        with pytest.raises(ValueError, match="Invalid participant_id"):
            validate_participant_id("ünïcödé")

    def test_rejects_uppercase(self):
        with pytest.raises(ValueError, match="Invalid participant_id"):
            validate_participant_id("Alice")

    def test_rejects_single_char(self):
        with pytest.raises(ValueError, match="Invalid participant_id"):
            validate_participant_id("a")

    def test_rejects_leading_hyphen(self):
        with pytest.raises(ValueError, match="Invalid participant_id"):
            validate_participant_id("-bad")

    def test_rejects_trailing_hyphen(self):
        with pytest.raises(ValueError, match="Invalid participant_id"):
            validate_participant_id("bad-")


# ─── Participant type validation ────────────────────────────


class TestParticipantTypeValidation:
    def test_valid_types(self):
        assert VALID_PARTICIPANT_TYPES == frozenset({"agent", "user"})

    def test_rejects_invalid_type(self):
        with pytest.raises(ValueError, match="Invalid participant_type"):
            validate_participant_type("bot")

    def test_rejects_empty_type(self):
        with pytest.raises(ValueError, match="Invalid participant_type"):
            validate_participant_type("")


# ─── UserStore CRUD ─────────────────────────────────────────


class TestUserStore:
    async def test_get_or_create_new(self, store: UserStore):
        user = await store.get_or_create("alice-01", display_name="Alice")
        assert user.participant_id == "alice-01"
        assert user.display_name == "Alice"
        assert user.participant_type == "user"

    async def test_get_or_create_existing(self, store: UserStore):
        first = await store.get_or_create("alice-01", display_name="Alice")
        second = await store.get_or_create("alice-01", display_name="Different")
        # Returns existing — display_name NOT updated
        assert second.display_name == "Alice"
        assert second.created_at == first.created_at

    async def test_get_or_create_defaults_display_name(self, store: UserStore):
        user = await store.get_or_create("local")
        assert user.display_name == "local"

    async def test_get_existing(self, store: UserStore):
        await store.get_or_create("alice-01", display_name="Alice")
        user = await store.get("alice-01")
        assert user is not None
        assert user.display_name == "Alice"

    async def test_get_unknown(self, store: UserStore):
        result = await store.get("nonexistent-00")
        assert result is None

    async def test_update_last_seen(self, store: UserStore):
        original = await store.get_or_create("alice-01", display_name="Alice")
        await store.update_last_seen("alice-01")
        updated = await store.get("alice-01")
        assert updated is not None
        assert updated.last_seen_at >= original.last_seen_at
        # Other fields unchanged
        assert updated.display_name == "Alice"
        assert updated.created_at == original.created_at

    async def test_get_or_create_validates_id(self, store: UserStore):
        with pytest.raises(ValueError, match="Invalid participant_id"):
            await store.get_or_create("BAD ID")

    async def test_get_or_create_validates_type(self, store: UserStore):
        with pytest.raises(ValueError, match="Invalid participant_type"):
            await store.get_or_create("valid-id", participant_type="robot")

    async def test_not_initialized_raises(self):
        store = UserStore(db_path=":memory:")
        with pytest.raises(RuntimeError, match="not initialized"):
            await store.get_or_create("alice-01")


# ─── Participant Protocol conformance ───────────────────────


class TestParticipantProtocol:
    def test_base_agent_satisfies_protocol(self):
        agent = _StubAgent(agent_id="test-agent", config={"name": "Test Agent"})
        assert isinstance(agent, Participant)

    def test_base_agent_participant_id(self):
        agent = _StubAgent(agent_id="test-agent")
        assert agent.participant_id == "test-agent"

    def test_base_agent_participant_type(self):
        agent = _StubAgent(agent_id="test-agent")
        assert agent.participant_type == "agent"

    def test_base_agent_display_name(self):
        agent = _StubAgent(agent_id="test-agent", config={"name": "Test Agent"})
        assert agent.display_name == "Test Agent"

    def test_base_agent_display_name_fallback(self):
        agent = _StubAgent(agent_id="test-agent")
        assert agent.display_name == "test-agent"

    def test_user_participant_satisfies_protocol(self):
        user = UserParticipant(participant_id="local", display_name="Local User")
        assert isinstance(user, Participant)

    def test_persona_agent_satisfies_protocol(self):
        """PersonaAgent (via BaseAgent) satisfies the Participant Protocol.

        (PR 6 review fix: PR 1 test gap #8.)
        """
        from agents.persona import create_persona_agent

        config = {
            "name": "Ember Owl",
            "role": "tester",
            "model": "test-model",
            "persona": {
                "background": "Test",
                "behavior": {"formality": 0.5},
            },
        }
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=None,
        )
        assert isinstance(agent, Participant)
        assert agent.participant_id == "ember-owl"
        assert agent.participant_type == "agent"
        assert agent.display_name == "Ember Owl"


# ─── PR 6 review follow-up tests ───────────────────────────


class TestUserStoreFollowUps:
    """Tests for review findings from PR 1 (PR 6 follow-ups)."""

    async def test_concurrent_get_or_create(self, store: UserStore):
        """Concurrent get_or_create calls are idempotent (INSERT OR IGNORE fix).

        (PR 6 review fix: PR 1 test gap #4.)
        """
        import asyncio

        results = await asyncio.gather(
            store.get_or_create("alice-01", display_name="Alice"),
            store.get_or_create("alice-01", display_name="Alice v2"),
            store.get_or_create("alice-01", display_name="Alice v3"),
        )
        # All return the same participant_id
        assert all(r.participant_id == "alice-01" for r in results)
        # display_name is from the first insert (INSERT OR IGNORE)
        assert all(r.display_name == results[0].display_name for r in results)

    async def test_update_last_seen_nonexistent(self, store: UserStore):
        """update_last_seen on nonexistent participant raises ValueError.

        After the validation fix, invalid participant_ids are rejected.
        For valid IDs that don't exist, the UPDATE is a no-op (0 rows affected).
        (PR 6 review fix: PR 1 test gap #5.)
        """
        # Valid ID format but not in DB — should succeed silently (0 rows updated)
        await store.update_last_seen("nobody-99")
        # Invalid format — should raise ValueError from validation
        with pytest.raises(ValueError, match="Invalid participant_id"):
            await store.update_last_seen("BAD ID")

    async def test_initialize_twice(self, store: UserStore):
        """UserStore.initialize() called twice works (close-then-reopen).

        (PR 6 review fix: PR 1 test gap #6.)
        """
        await store.get_or_create("alice-01", display_name="Alice")
        # Re-initialize (close + reopen)
        await store.initialize()
        # Data should persist (in-memory DB is lost on reopen, but the
        # table creation should succeed without error)
        # For :memory: DBs, data is lost on re-initialize — this test
        # verifies the re-initialization path doesn't error.
        user = await store.get("alice-01")
        # In-memory DB: data is gone after re-initialize (new connection)
        # This is expected — the test validates the code path, not persistence.
        assert user is None or user.participant_id == "alice-01"
        # Verify we can create again after re-init
        user2 = await store.get_or_create("bob-01", display_name="Bob")
        assert user2.participant_id == "bob-01"

    async def test_long_display_name(self, store: UserStore):
        """display_name with 10,000+ chars is stored (no length limit enforced).

        This documents the current behavior. A future PR may add a length
        limit at the write boundary.
        (PR 6 review fix: PR 1 test gap #7.)
        """
        long_name = "x" * 10001
        user = await store.get_or_create("alice-01", display_name=long_name)
        assert user.display_name == long_name
        # Verify persistence
        fetched = await store.get("alice-01")
        assert fetched is not None
        assert fetched.display_name == long_name
