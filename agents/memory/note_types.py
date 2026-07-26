"""Data model + column constants for the notes tier.

Split out of :mod:`agents.memory.notes` when the RFC 0037 PR 4 §C/§D
columns pushed that module past the project's 500-line review-friendly
cap (see ``scripts/checks/file_size.py``), mirroring the
:mod:`agents.memory.fact_types` precedent — the dataclass + SELECT-column
constants live beside the tier they describe but out of the CRUD body.
:mod:`agents.memory.notes` re-exports :class:`Note` so
``from agents.memory.notes import Note`` keeps working for every existing
call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..session_id import LEGACY_SESSION_ID
from ._migration_protection import PROTECTION_LEVEL_DEFAULT

__all__ = ["Note", "_NOTE_COLS", "_NOTE_SELECT"]


@dataclass
class Note:
    """An agent-initiated note persisted via memory tools.

    ``session_id`` (RFC 0031 Phase 2 PR 2) is on the recall projection;
    defaults to :data:`agents.session_id.LEGACY_SESSION_ID` so a hand-
    constructed test fixture round-trips without opting in.
    """

    id: str
    agent_id: str
    topic: str
    content: str
    tags: list[str] = field(default_factory=list)
    access_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    session_id: str = LEGACY_SESSION_ID
    # RFC 0037 §C/§D (migration v16, PR 4): the note's protection level and
    # (always-NULL-for-notes today; the §C synthesized-note shape) source
    # channel, projected onto recall so the §D gate can rank each candidate.
    # Defaults match the v16 column DEFAULT so hand-built fixtures
    # round-trip; a corrupted stored label fails closed at the gate.
    protection_level: str = PROTECTION_LEVEL_DEFAULT
    source_channel_id: str | None = None


# Column list for SELECT queries on the notes table.  RFC 0031 Phase 2
# PR 2: ``session_id`` joined the projection — the dataclass, the
# projection, and ``_row_to_note`` MUST move together (contract pin at
# ``test_session_id_notes_migration.TestNotesProjectionContract``).
_NOTE_COLS = (
    "id", "agent_id", "topic", "content", "tags_json",
    "access_count", "created_at", "updated_at",
    "session_id",
    # RFC 0037 §C (v16): surfaced to the §D gate + the recall predicate.
    "protection_level", "source_channel_id",
)
_NOTE_SELECT = ", ".join(_NOTE_COLS)
