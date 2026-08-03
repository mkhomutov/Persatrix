"""Cascade-depth default — source-of-truth Python constant.

Mirrors ``internal/defaults/defaults.go::DefaultMaxCascadeDepth``. The
cross-language drift pin
(``tests/unit/python/test_cross_language_max_cascade_depth_drift.py``)
fails if the two values disagree.

Why a standalone module rather than living on ``agents.dispatch``:
``agents.action_executor`` (and other publish-path call sites) need
this value, but they are imported by ``agents.dispatch`` itself —
hosting the constant on ``dispatch`` would create a circular import.
A leaf module is the smallest dependency that does not invite cycles
as the publish path grows future call sites.

Re-exported from ``agents.dispatch`` for caller ergonomics so external
code can keep importing from the historical surface.
"""

from __future__ import annotations

# The cooperative-path cascade-depth cap. Used in two contracts:
#
# * :class:`agents.dispatch.EventDispatcher`'s defense-in-depth backstop
#   (the dispatcher drops events whose inbound ``cascade_depth`` already
#   meets the cap before invoking the persona).
#
#   ISSUE-0114 (v0.3.13) alignment decision, option (c): this Python cap
#   is declared a PER-PROCESS GLOBAL BACKSTOP ONLY — it does not learn
#   the orchestrator's per-channel ``max_cascade_depth`` overrides, and
#   deliberately so (teaching the dispatcher channel config it does not
#   read today was option (b), rejected as coupling). The invariant that
#   keeps the backstop honest is enforced Go-side instead: per-channel
#   caps are validated <= the fleet ``max_cascade_depth`` (which this
#   constant is aligned with by convention), so this backstop can never
#   fire below a legitimately raised channel cap. Raising one channel
#   past the fleet default therefore means raising the fleet cap AND
#   this aligned value first.
# * The "no inbound depth known" safe default on
#   :meth:`agents.action_executor.ActionExecutor.execute` and
#   :meth:`agents.channel_publisher.ChannelPublisher.publish` — call
#   sites that have no inbound event to derive depth from (notably the
#   tick scheduler at ``agents/tick.py``) get the terminate-at-clamp
#   default. The orchestrator's ``clamped >= max_cascade_depth`` check
#   drops fanout, so a tick-originated publish is stored once and the
#   chain terminates instead of resetting cascades in flight.
DEFAULT_MAX_CASCADE_DEPTH = 5

__all__ = ["DEFAULT_MAX_CASCADE_DEPTH"]
