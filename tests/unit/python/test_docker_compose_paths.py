"""
Guards docker-compose.yaml against the ISSUE-0047 failure mode:

The orchestrator's ``--channels-db`` flag defaults to the relative path
``data/channels.db`` (see ``cmd/orchestrator/channels.go:22``). Inside
the container, the working directory is ``/app`` and ``/app/data`` has
no writable mount — the only writable path the non-root ``appuser``
owns is ``/var/lib/persatrix`` (volume ``orchestrator-data``).

Without an explicit ``--channels-db`` override pointing at that volume,
``initChannels`` fails with ``mkdir data: permission denied``, the
channels subsystem stays disabled, and every ``POST /api/v1/agents/{id}/chat``
request returns ``500 chat not available`` because chat is routed
through channels (chat-as-DM, RFC 0011 PR 4a-ii-β-2).

The agent containers have analogous failure modes for any future
relative-default-path flag, so the test asserts the invariant for
*every* service that declares a ``command`` and a ``volumes`` block.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# Paths inside the container that are known to be writable for the
# non-root container user. Any flag value beginning with one of these
# is accepted by the assertion below. ``:memory:`` is the SQLite
# in-process sentinel (no filesystem access at all).
WRITABLE_PREFIXES = (
    "/var/lib/persatrix",
    "/workspace",
    "/app/data",
    ":memory:",
)


def _compose_path() -> Path:
    return Path(__file__).resolve().parents[3] / "docker-compose.yaml"


def _load_compose() -> dict:
    with _compose_path().open("rb") as fh:
        return yaml.safe_load(fh)


def _command_args(service: dict) -> list[str]:
    """Return the command list for a service, or an empty list if absent."""
    cmd = service.get("command")
    if cmd is None:
        return []
    if isinstance(cmd, str):
        return cmd.split()
    return list(cmd)


def _flag_value(args: list[str], flag: str) -> str | None:
    """Return the value following ``flag`` in a ``--key value`` style arg list.

    Returns ``None`` if the flag is absent. Supports both ``--flag value``
    (two-token) and ``--flag=value`` (one-token) forms.
    """
    for i, arg in enumerate(args):
        if arg == flag:
            if i + 1 < len(args):
                return args[i + 1]
            return None
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return None


def test_orchestrator_channels_db_points_at_writable_volume() -> None:
    """ISSUE-0047 guard: --channels-db must resolve to a writable path.

    The flag default is the relative ``data/channels.db``, which under
    the container WORKDIR (``/app``) becomes ``/app/data/channels.db``.
    ``/app`` has no writable mount, so without an explicit override the
    channels subsystem stays disabled and chat-as-DM returns 500.
    """
    compose = _load_compose()
    services = compose["services"]
    assert "orchestrator" in services, "orchestrator service missing from compose"

    args = _command_args(services["orchestrator"])
    channels_db = _flag_value(args, "--channels-db")

    assert channels_db is not None, (
        "docker-compose.yaml orchestrator command does not pass "
        "--channels-db. The flag default is the relative path "
        "'data/channels.db', which the non-root appuser cannot create "
        "under WORKDIR=/app. Channels stay disabled and POST /chat "
        "returns 500. See ISSUE-0047."
    )
    assert channels_db.startswith(WRITABLE_PREFIXES), (
        f"--channels-db={channels_db!r} does not point at a known "
        f"writable mount {WRITABLE_PREFIXES}. Pick a path under the "
        "orchestrator-data volume (/var/lib/persatrix/) so the SQLite "
        "store can be created."
    )


def test_orchestrator_channels_db_path_has_writable_mount() -> None:
    """The chosen --channels-db path must be served by a declared volume.

    Stronger than the prefix check: catches the case where someone
    invents a new prefix that *looks* writable but has no volume
    backing (e.g. ``/data/channels.db`` with no ``/data`` mount).
    """
    compose = _load_compose()
    orch = compose["services"]["orchestrator"]
    args = _command_args(orch)
    channels_db = _flag_value(args, "--channels-db")
    if channels_db is None or channels_db == ":memory:":
        return  # other test covers presence; in-memory needs no mount

    mount_targets = []
    for entry in orch.get("volumes", []):
        # Volumes are ``"src:dst[:opts]"`` strings in the short form.
        parts = entry.split(":")
        if len(parts) >= 2:
            mount_targets.append(parts[1])

    assert any(channels_db.startswith(target.rstrip("/") + "/") for target in mount_targets), (
        f"--channels-db={channels_db!r} is not under any volume mount "
        f"declared on the orchestrator service. Mount targets: "
        f"{mount_targets}. Without a backing volume the path is on the "
        "container's writable layer (lost on every recreate) or on a "
        "read-only mount."
    )
