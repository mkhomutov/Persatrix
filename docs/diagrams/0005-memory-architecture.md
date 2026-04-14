# RFC 0005 Memory Architecture

RFC 0005 introduces three complementary memory tiers with different lifetimes and purposes.

```mermaid
graph TD
    A[Persona Runtime]
    W["Working Memory\nworking.py\nshort-term context"]
    E["Episodic Memory\nepisodic.py\nSQLite + FTS5 episodes"]
    R["Relationship Memory\nrelationship.py\ntrust and interactions"]
    N["Agent Notes\nnotes.py\nexplicit note CRUD"]
    DB[(memory.db)]

    A -->|read/write active context| W
    A -->|store/recall episodes| E
    A -->|update trust history| R
    A -->|store_note/recall_notes| N

    E --> DB
    R --> DB
    N --> DB
```

Read/write paths:
- Read: agent injects relevant working, episodic, and relationship context before LLM calls.
- Write: outcomes and reflections are persisted to episodic/notes; interactions update relationship trust.
