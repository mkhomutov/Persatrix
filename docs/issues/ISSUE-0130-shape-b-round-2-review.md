# ISSUE-0130 shape (b) — PR B2 review, round 2

**Companion to**: [ISSUE-0130](ISSUE-0130-catchup-replay-rederives-memory-under-default-principal.md)
· [the build log](ISSUE-0130-shape-b-build-log.md)
**PR**: [#851](https://github.com/mkhomutov/Persatrix/pull/851) — 2026-08-31

Eleven findings on the second review round of PR B2. Below are the ones
that changed a **contract** rather than a comment; the rest were docstring
and cross-reference corrections applied in place.

Split out of the build log rather than appended to it: that log is
chronological and grows once per PR, and it had reached the 3 000-word
documentation cap — the same reason it was itself split out of the issue
on 2026-08-30. A round of review findings is a self-contained document
with its own reason to be cited, so it separates cleanly.

---

* **The catch-up boundary was a ROOM event and should not have been.**
  Making the replay/live split symmetric was right, but it fires through
  `close_stale_records`, which fans every predicate over *every* record in
  the scope — correct for wire rotation, wrong here. A turn can only merge
  into the record under its own `(principal, speaker, scope)` key, so that
  is the only record it can spoil; room-wide, one replayed row closed every
  unrelated live conversation in the room, chopping each into a one-turn
  episode and firing an unmetered summarise for it, on the boot path while
  dispatch was serving. The predicate now takes `is_target_record`.
* **Attribution is half of the disagreement, not decoration.** A seeded
  `'local'` and an unseeded default resolve to the SAME record key, so a
  row that could not name its tenant joined a span opened by one that
  could, and its content derived into the shared tenant under the
  opener's attribution — the leak, through the one field
  `replay_attributed` exists to answer, which freezing at open only
  settles for the OPENING turn. The split now compares the pair, via one
  shared `replay_markers` seam so the freeze and the split cannot drift.
* **The epoch was still read ambient at close.** `principal_id` is frozen
  on the record and re-bound around the derivation; the epoch had no such
  twin, and the symmetric split made that reachable — a replayed event
  carries no epoch key, so force-closing a live record stamped the
  persona's world epoch on a conversation opened under a request epoch,
  which strict-equality recall then hides from its own reader. It is now
  captured by the tracker at open and bound beside the principal. The
  same read made the span digest depend on WHICH close path fired rather
  than on the span, so the guard missed on any boot a live turn
  interrupted; the digest reads the record's epoch too.
* **The v12 backfill fix could not reach the stores that needed it.**
  Inverting `'local'` → `''` in place helps only stores that have not run
  v12: `applyMigration` dispatches on `user_version`, so a store already
  at 12 keeps the bad backfill forever — and the new guard would make the
  resulting wrong-tenant episode permanent by storing its digest.
  Channel-store **v13** detects an affected store exactly (the column's
  recorded DEFAULT is `'local'` there, `''` elsewhere) and rewrites those
  rows, over-correcting a real `'local'` it cannot tell apart — the
  conservative side.
* **Completeness was per AGENT where the hazard is per CHANNEL.** The
  derive gate was one boolean for the whole pass, so a budget overrun in
  the ninth channel discarded the eight windows that had already
  finished, and a row whose `on_event` raised still reported the pass
  complete. `replay_channel_history` now fills a set of finished channel
  ids, and the sweep gates on the record's `source_channel_id`.
* **The boot sweep's throttle had no budget and measured the wrong set.**
  It runs in the caller's `finally`, outside the 60 s catch-up
  `wait_for`, and paced against `_pending_summarize_tasks` — shared with
  the live close path — while each task holds a 30 s LLM round trip, so
  the boot tail grew with the replayed-record count and
  `AgentServer.start` arms the ISSUE-0125 watcher only afterwards. It now
  counts the tasks the sweep itself spawned, gives up after a wall-clock
  budget (closing and persisting regardless; only pacing is dropped), and
  sits outside the per-record `try`, which had reported throttle failures
  as "Catch-up close failed" for records already persisted.
* **The digest read the §G-filtered turn view**, which is a digest over a
  subset by construction — the shape `replay_identity` refuses in its own
  docstring. It agreed with the full view only because a different module
  keeps the room-close fan off replayed records. It now reads
  `interaction.turns`.

`close_path.py` crossed the 500-line cap, so the boot sweep moved to
`replay_sweep.py` — a seam, not a line count: `close_path` owns what
closing ONE record costs, the sweep owns the only close loop whose cost
is startup latency. Every fix is pinned by a test that fails without it
(mutation-checked), in `test_replay_boundary_and_sweep.py` (new),
`test_replay_span_identity.py`, `test_catchup_replay_attribution.py` and
`sqlite_principal_backfill_repair_test.go`.
