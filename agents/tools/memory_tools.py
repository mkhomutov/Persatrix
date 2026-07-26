"""Agent-initiated memory tools (notes) — the closure-based factory.

Moved verbatim from :mod:`agents.tools.builtin` (RFC 0037 PR 4) when the
§C/§D notes-leg wiring pushed that module past the 500-line review cap;
``builtin`` re-exports :func:`create_memory_tools` /
:func:`check_auto_reflect` so every existing import keeps working.

RFC 0037 (PR 4) makes the note tools a **stamped write choke point and a
gated read surface**:

* ``store_note`` stamps ``protection_level`` with the acting turn's
  classification through rule (a) (``normalize_for_stamp`` — absent →
  ``internal``, never ``public``).  Sound because the §D hard gate is
  live in the same PR: a turn acting at ``L`` only ever has ``≤ L``
  memory plus that channel's own content in context, so a note authored
  in it cannot contain anything above ``L`` (§C).
* ``recall_notes`` passes the acting level's injectable-level IN-list as
  the non-optional §D read-surface predicate — an always-dispatchable
  tool that searched all notes unfiltered was the gate bypass v0.3.12
  review item 6 flagged.
* ``update_note`` re-stamps to ``max(existing, acting L)`` — an edit
  never lowers a note's level (§C, item 6).

The acting level rides the task-local :mod:`agents.acting_classification`
axis (bound per event by ``request_scope_from_metadata``); the lattice
resolvers are imported lazily inside each tool because this executor-side
module must not hard-depend on the persona subpackage (the
``identity_write_through`` precedent — by tool-call time the persona
runtime is fully imported).
"""

from __future__ import annotations

import logging
import uuid as _uuid
from typing import TYPE_CHECKING

from ..acting_classification import current_acting_classification
from ..prompt_loader import load_snippet
from ..session_id import current_session_id, resolve_session_id_silent
from .identity_write_through import maybe_write_through_identity
from .registry import ToolDefinition, ToolResult, get_tool, tool

if TYPE_CHECKING:
    from ..memory.episodic import EpisodicMemory
    from ..memory.relationship import RelationshipMemory
    from .permissions import PermissionGate

logger = logging.getLogger(__name__)


def create_memory_tools(
    memory: EpisodicMemory,
    gate: PermissionGate,
    *,
    max_notes: int = 500,
    auto_reflect_after: int = 0,
    relationship: RelationshipMemory | None = None,
) -> list[ToolDefinition]:
    """Create closure-based memory tools bound to a specific EpisodicMemory instance.

    The ``agent_id`` and DB connection are captured in the closure — they are
    NOT controllable by the LLM.  So is every RFC 0037 classification input:
    the acting level is read off the task-local scope the runtime bound from
    the trusted event, never off a tool argument.

    ``relationship`` (RFC 0031 amendment, F-7 Option D, ISSUE-0093 PR D2)
    wires the person-identity write-through: a ``store_note`` call whose
    topic is ``contact:<id>`` additionally upserts structured identity
    (name / role / prefs) onto the cross-room relationship tier so it
    surfaces in every room for that person, not just the room it was
    stated in.  ``None`` (the default for non-persona callers and the
    pre-wiring path) disables the write-through — the note tool behaves
    exactly as before.

    Returns a list of registered ToolDefinition objects.
    """
    tools: list[ToolDefinition] = []

    # RFC 0031 Phase 2 PR 1 — resolve the per-process operator namespace
    # once at tool-construction time (the env var is fixed for the life of
    # a persona-runtime process), mirroring the silent construction-time
    # read the MemoryStore facade does for the episode/relationship write
    # path.  Captured in the closure as the *fallback*; ISSUE-0081 PR 2
    # layers a call-time ``session_scope`` override on top at the write
    # site so a note stored while handling a concurrent conversation is
    # tagged with that conversation's session, not this snapshot.
    session_id = resolve_session_id_silent()

    @tool(
        name="store_note",
        description="Store a note for future reference",
        permissions=["memory:write"],
        tier="builtin",
    )
    async def store_note(topic: str, content: str, tags: str = "") -> ToolResult:
        """Store a note with a topic and content. Tags is a comma-separated string."""
        if not gate.check("memory:write"):
            return ToolResult(success=False, error="Permission denied: memory:write")
        # RFC 0031 amendment (F-7 Option D, ISSUE-0093) PR D3 — a
        # ``contact:<id>`` note's person identity now lives on the cross-room
        # relationship tier *only* (D2's dual-write note is retired).  ``True``
        # means identity was persisted, so skip the note; ``False`` falls
        # through to the note write below as a safety net (see the helper).
        if await maybe_write_through_identity(relationship, topic, content):
            return ToolResult(success=True, data={"topic": topic, "identity_stored": True})
        # RFC 0037 §C (PR 4): THE notes stamp site — rule (a) over the
        # task-local acting level.  Lazy lattice import per the module
        # docstring.
        from ..persona_runtime.classification import normalize_for_stamp

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        try:
            note_id = await memory.store_note(
                topic=topic, content=content, tags=tag_list, max_notes=max_notes,
                # ISSUE-0081 PR 2: call-time scope wins over the
                # construction snapshot captured above.
                session_id=current_session_id() or session_id,
                protection_level=normalize_for_stamp(
                    current_acting_classification(),
                ),
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), error_type="ValueError")
        return ToolResult(success=True, data={"note_id": note_id, "topic": topic})

    @tool(
        name="recall_notes",
        description="Search stored notes by query",
        permissions=["memory:read"],
        tier="builtin",
    )
    async def recall_notes(query: str = "", limit: int = 10) -> ToolResult:
        """Search notes. Empty query returns most recent notes."""
        if not gate.check("memory:read"):
            return ToolResult(success=False, error="Permission denied: memory:read")
        # RFC 0037 §D read surfaces (PR 4, review item 6): the tool query
        # carries the acting level's injectable-level IN-list — an
        # above-``L`` note neither returns nor burns a ``limit`` slot,
        # and an unbound acting level floors to ``public`` (rule (b)).
        from ..persona_runtime.classification import injectable_levels

        try:
            notes = await memory.recall_notes(
                query=query, limit=limit,
                allowed_protection_levels=injectable_levels(
                    current_acting_classification(),
                ),
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), error_type="ValueError")
        return ToolResult(
            success=True,
            data=[
                {
                    "id": n.id,
                    "topic": n.topic,
                    "content": n.content,
                    "tags": n.tags,
                    "access_count": n.access_count,
                }
                for n in notes
            ],
        )

    @tool(
        name="update_note",
        description="Update the content of an existing note",
        permissions=["memory:write"],
        tier="builtin",
    )
    async def update_note(note_id: str, content: str) -> ToolResult:
        """Update a note's content. Topic and tags are preserved."""
        if not gate.check("memory:write"):
            return ToolResult(success=False, error="Permission denied: memory:write")
        try:
            _uuid.UUID(note_id)
        except (ValueError, AttributeError):
            return ToolResult(
                success=False,
                error=f"Invalid note_id (expected UUID): {note_id}",
                error_type="ValueError",
            )
        # RFC 0037 §C (PR 4, review item 6): an edit re-stamps to
        # ``max(existing, acting L)`` — never lowers.  The ``max`` is
        # resolved here to plain data (the stamp + the strictly-lower
        # level set) because the memory layer cannot rank.
        from ..persona_runtime.classification import (
            levels_below_stamp,
            normalize_for_stamp,
        )

        acting = current_acting_classification()
        try:
            found = await memory.update_note(
                note_id=note_id, content=content,
                restamp_protection_level=normalize_for_stamp(acting),
                restamp_below=levels_below_stamp(acting),
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), error_type="ValueError")
        if not found:
            return ToolResult(success=False, error=f"Note not found: {note_id}")
        return ToolResult(success=True, data={"note_id": note_id, "updated": True})

    @tool(
        name="delete_note",
        description="Delete a stored note",
        permissions=["memory:write"],
        tier="builtin",
    )
    async def delete_note(note_id: str) -> ToolResult:
        """Delete a note by ID."""
        if not gate.check("memory:write"):
            return ToolResult(success=False, error="Permission denied: memory:write")
        try:
            _uuid.UUID(note_id)
        except (ValueError, AttributeError):
            return ToolResult(
                success=False,
                error=f"Invalid note_id (expected UUID): {note_id}",
                error_type="ValueError",
            )
        found = await memory.delete_note(note_id=note_id)
        if not found:
            return ToolResult(success=False, error=f"Note not found: {note_id}")
        return ToolResult(success=True, data={"note_id": note_id, "deleted": True})

    # Collect registered tool definitions
    for name in ("store_note", "recall_notes", "update_note", "delete_note"):
        td = get_tool(name)
        if td is not None:
            tools.append(td)

    return tools


async def check_auto_reflect(
    memory: EpisodicMemory,
    auto_reflect_after: int,
) -> str | None:
    """Increment the interaction counter and return a nudge if threshold reached.

    Returns a system prompt nudge string if ``auto_reflect_after > 0`` and the
    counter has reached the threshold, otherwise ``None``.  Resets the counter
    after firing.
    """
    if auto_reflect_after <= 0:
        return None
    count = await memory.increment_interaction_count()
    if count >= auto_reflect_after:
        await memory.reset_interaction_count()
        return load_snippet("reflection-nudge")
    return None
