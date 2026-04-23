"""Test infrastructure helpers shared by all pytest conftests.

Currently provides ``daemonize_aiosqlite_workers`` — a one-shot monkeypatch
that marks ``aiosqlite``'s per-connection worker thread as ``daemon=True``.

Why
---
``aiosqlite.core.Connection`` runs every SQL call on a private background
``threading.Thread`` that is created with the stdlib default ``daemon=False``.
When a test opens a connection (directly via ``EpisodicMemory`` /
``RelationshipMemory`` or transitively via ``PersonaAgent.initialize_memory()``)
and the test forgets to call ``close()``, the worker thread keeps blocking
on its internal ``SimpleQueue.get()`` forever.  Python's interpreter
shutdown waits for all non-daemon threads to join before running ``atexit``
handlers, so the whole pytest process hangs after the last test reports
``passed`` — even though every assertion succeeded.

Daemonising the worker means a leaked connection no longer blocks process
exit: the daemon thread is killed when the interpreter tears down.
Properly-closed connections are unaffected (the worker drains its sentinel
and exits normally before the process ends).

This is a *test-only* safety net.  Production code paths still close their
connections in their own ``close()`` methods (see ``EpisodicMemory.close``,
``RelationshipMemory.close``).
"""

from __future__ import annotations


def daemonize_aiosqlite_workers() -> None:
    """Patch ``aiosqlite.core.Connection.__init__`` so its worker thread is daemon.

    Idempotent — safe to call from multiple conftests.  No-op if the patch is
    already installed or if ``aiosqlite`` is not importable.
    """
    try:
        import aiosqlite.core as _core
    except ImportError:
        return

    if getattr(_core.Connection.__init__, "_persatrix_daemon_patched", False):
        return

    _orig_init = _core.Connection.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _orig_init(self, *args, **kwargs)
        # ``self._thread`` is created (but not started) inside the original
        # ``__init__``; mark it daemon before the connection is awaited.
        thread = getattr(self, "_thread", None)
        if thread is not None:
            thread.daemon = True

    _patched_init._persatrix_daemon_patched = True  # type: ignore[attr-defined]
    _core.Connection.__init__ = _patched_init  # type: ignore[method-assign]
