"""
Guards that the deleted v0.2 ``ChannelService`` surface stays unreachable.

Catches a ``make proto`` regen accident that quietly resurrects the orphan
stubs (the corresponding ``.proto`` file no longer exists, so the only way
the modules come back is a hand-edit or a stale generator cache). Adjacent
to but not subsumed by ISSUE-0023's broader ``make proto && git diff
--exit-code`` CI gate. PR #246 deep review Should-Fix #2.

The stub-behavior tests previously in this file (assert ``success=False``)
were superseded by the real-handler tests in
``tests/unit/python/test_receive_channel_message.py`` (RFC 0011 PR 4a).
"""

from __future__ import annotations

import pytest


class TestV02ChannelSurfaceRemoved:
    def test_agent_message_pb2_unimportable(self):
        with pytest.raises(ModuleNotFoundError):
            import agents.generated.agent_message_pb2  # noqa: F401

    def test_agent_message_pb2_grpc_unimportable(self):
        with pytest.raises(ModuleNotFoundError):
            import agents.generated.agent_message_pb2_grpc  # noqa: F401
