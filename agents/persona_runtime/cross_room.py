"""Shared ``memory.{facts,episodic}.cross_room`` mode vocabulary (RFC 0049).

One closed mode set for both cross-room knobs, hoisted out of
:mod:`.facts_shadow` when PR 4 added ``"live"`` (the #783 review
follow-up: two tiers sharing one vocabulary must not each own a copy of
it).  The three modes:

* ``off`` — no cross-room pass at all.
* ``shadow`` — the PR 2/PR 3 posture: the widened recall runs beside the
  live tiers and records what it *would* have injected as a structured
  log trace; the live prompt keeps the room-walled recall byte-for-byte.
* ``live`` — the PR 4 promotion (the shipped v0.3.12 default): the
  widened recall IS the live recall — L2 facts recall cross-room
  (`RFC 0031 fact-scope amendment
  <../../docs/rfcs/0031-amendment-fact-scope-by-consolidation-level.md>`_)
  and L1 episodic recall runs room-first-RANKED (`RFC 0049 L1 amendment
  <../../docs/rfcs/0049-amendment-l1-cross-room-availability.md>`_),
  every candidate still passing the RFC 0037 §D gate before the RFC 0017
  budget.  The shadow passes do not run in this mode — the widened read
  happens once, on the live path (the #783 "fold live+widened into one
  query" follow-up).

The promotion is measurement-gated: ``"live"`` joined this vocabulary
only after the RFC 0044 golden-trace shadow verdict ran green
(``evaluators/shadow_measurement.py``); the resolvers below were the
enforcement point that rejected it until then.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "CROSS_ROOM_LIVE",
    "CROSS_ROOM_MODES",
    "CROSS_ROOM_OFF",
    "CROSS_ROOM_SHADOW",
    "DEFAULT_EPISODIC_CROSS_ROOM",
    "DEFAULT_FACTS_CROSS_ROOM",
    "resolve_episodic_cross_room",
    "resolve_facts_cross_room",
]

CROSS_ROOM_OFF: Final[str] = "off"
CROSS_ROOM_SHADOW: Final[str] = "shadow"
CROSS_ROOM_LIVE: Final[str] = "live"
CROSS_ROOM_MODES: Final[frozenset[str]] = frozenset(
    {CROSS_ROOM_OFF, CROSS_ROOM_SHADOW, CROSS_ROOM_LIVE},
)

#: Live is the shipped v0.3.12 posture for both tiers — the RFC 0049
#: PR 4 promotion, flipped from ``shadow`` on the green golden-trace
#: verdict.  Kept as two names (not one shared constant) so a future
#: per-tier rollback is a one-line, one-tier change.
DEFAULT_FACTS_CROSS_ROOM: Final[str] = CROSS_ROOM_LIVE
DEFAULT_EPISODIC_CROSS_ROOM: Final[str] = CROSS_ROOM_LIVE


def _resolve_cross_room(config: dict, *, tier: str, default: str) -> str:
    """Resolve ``memory.<tier>.cross_room`` from a persona config.

    Absent / ``None`` → ``default`` (the ``resolve_facts_config``
    null-collapse precedent).  An unknown string raises ``ValueError``
    at agent construction — deliberately louder than a silent floor:
    silently degrading a requested mode misreports what the deployment
    is doing.  Production configs are already schema-gated to the enum;
    this is the programmatic-path twin of that gate.
    """
    tier_cfg = (config.get("memory") or {}).get(tier) or {}
    raw = tier_cfg.get("cross_room")
    if raw is None:
        return default
    # ``isinstance`` narrows the untyped config value for mypy AND folds
    # non-str garbage into the same loud rejection as an unknown mode.
    if not isinstance(raw, str) or raw not in CROSS_ROOM_MODES:
        raise ValueError(
            f"memory.{tier}.cross_room must be one of "
            f"{sorted(CROSS_ROOM_MODES)}, got {raw!r}",
        )
    return raw


def resolve_facts_cross_room(config: dict) -> str:
    """Resolve ``memory.facts.cross_room`` (L2 — the fact-scope amendment)."""
    return _resolve_cross_room(
        config, tier="facts", default=DEFAULT_FACTS_CROSS_ROOM,
    )


def resolve_episodic_cross_room(config: dict) -> str:
    """Resolve ``memory.episodic.cross_room`` (L1 — the L1 amendment)."""
    return _resolve_cross_room(
        config, tier="episodic", default=DEFAULT_EPISODIC_CROSS_ROOM,
    )
