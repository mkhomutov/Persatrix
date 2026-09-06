#!/usr/bin/env python3
"""Operations vocabulary for the MT-MEMORY-GROUP-TENANT-001 driver.

Split from ``mt_group_tenant_001.py`` when the driver crossed the 500-line
cap. The seam is deliberate: this module is the arc's **verbs** — rotate the
accounts store, authenticate, publish as a named speaker, flip the auth mode,
wait for health — while the driver holds the **arc**, one function per leg.
Several of these verbs exist only because the MT's own instructions did not
survive contact with a compose deployment; each carries the correction and
the symptom it prevents.

Nothing here decides pass/fail, and nothing runs unless ``Ctx.execute`` is set.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

if str(Path(__file__).resolve().parent.parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.manual_tests import mt_group_tenant_preflight as pf  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SECURITY_CONFIG = REPO_ROOT / "config" / "security.yaml"

CLI = "./bin/persatrix"
ROOM = "planning"

CONTAINER_ACCOUNTS_DB = "/var/lib/persatrix/accounts.db"
ACCOUNTS_SIDECARS = (
    CONTAINER_ACCOUNTS_DB,
    f"{CONTAINER_ACCOUNTS_DB}-wal",
    f"{CONTAINER_ACCOUNTS_DB}-shm",
)


class ArcAbortedError(RuntimeError):
    """A setup step failed; continuing would produce meaningless legs.

    The first live run of this driver bootstrapped nothing (wrong binary
    path), then logged in as nobody, then published as nobody — and still
    "captured" a Leg 2 table and a cost figure. Empty artifacts from a broken
    setup are worse than no artifacts: they are indistinguishable, in the
    report, from a leg that genuinely found nothing.
    """


@dataclass
class Ctx:
    execute: bool
    server: str
    jaeger: str
    out: Path
    artifacts: list[str]
    password: str

    #: Original text of `config/security.yaml`, taken the first time
    #: :func:`set_auth_mode` rewrites it. The arc flips auth twice — Leg 8 off,
    #: Leg 9 back on — so every path that leaves before Leg 9 finishes used to
    #: strand a TRACKED file in a modified state: an abort, a Ctrl-C, or even
    #: `--legs 0-8` completing cleanly. One release-prep commit in this repo
    #: exists purely to undo that by hand. :func:`restore_config` puts it back.
    security_backup: str | None = None

    #: Channels this arc armed (`autonomous.enabled=true`) and must disarm on
    #: the way out. An armed channel is convenable, and spends, on any boot.
    armed_rooms: list[str] = field(default_factory=list)

    def say(self, msg: str) -> None:
        print(msg, flush=True)

    def run(self, cmd: list[str], *, why: str, timeout: int = 120,
            stdin: str | None = None, secret: bool = False,
            critical: bool = False) -> str:
        """Run a command, or describe it in a dry run.

        ``stdin`` feeds the provisioning pipe that both credential verbs
        support (`promptPassword` in `cmd/orchestrator/bootstrap.go` and
        `read_password` in `cli/src/commands/auth.rs` both fall back to
        reading a line when stdin is not a terminal). ``secret`` keeps that
        input out of the printed transcript — the password never reaches
        argv, which is the §J discipline both verbs are written to.
        """
        printable = " ".join(cmd)
        piped = "  <<< (password on stdin)" if secret else ""
        if not self.execute:
            self.say(f"    [dry-run] {printable}{piped}")
            self.say(f"              ({why})")
            return ""
        self.say(f"    $ {printable}{piped}")
        try:
            proc = subprocess.run(  # noqa: S603
                cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout, check=False, input=stdin,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            # A timeout or a missing binary is not a command that FAILED — it
            # is a command that never ran, so `critical` does not enter into
            # it. Both used to escape as a bare traceback past the driver's
            # `except ArcAbortedError`, which skipped the partial-artifact
            # write and discarded every leg the arc had already paid for.
            # `./bin/persatrix` is a gitignored build artifact no gate checks,
            # and a slow `docker compose restart` clears the 120s default.
            self.say(f"    ! {type(exc).__name__}: {exc}")
            raise ArcAbortedError(
                f"{printable} -> {type(exc).__name__}: {exc}"
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip())[:400]
            self.say(f"    ! exit {proc.returncode}: {detail}")
            if critical:
                raise ArcAbortedError(f"{printable} -> exit {proc.returncode}: {detail}")
        return proc.stdout

    def pause(self, seconds: int, why: str) -> None:
        if not self.execute:
            self.say(f"    [dry-run] wait {seconds}s ({why})")
            return
        self.say(f"    … waiting {seconds}s ({why})")
        time.sleep(seconds)

    def record(self, heading: str, body: str) -> None:
        self.artifacts.append(f"### {heading}\n\n{body}\n")
        self.say(f"    + captured: {heading}")


def set_auth_mode(ctx: Ctx, mode: str) -> None:
    """Flip `auth.mode` in config/security.yaml and restart the orchestrator.

    Leg 8 turns auth OFF and — critically — does not turn it back on; Leg 9
    must restore it before it reads the attributed partition, or the newest
    traffic is stamped `local` and the partition it tells you to read first is
    the one nothing replays.

    Rewrites only the *setting* line inside the `auth:` block. A naive
    substitution would also hit the comment above it, which contains the
    literal string `mode: enabled` while the setting reads `disabled` — the
    same trap that made the first preflight pass on prose (F-1).

    The original text is stashed on the ``Ctx`` the first time through, so
    :func:`restore_config` can put the tracked file back however the arc ends.
    """
    path = SECURITY_CONFIG
    if not ctx.execute:
        ctx.say(f"    [dry-run] set auth.mode: {mode} in config/security.yaml + restart")
        ctx.say(f"    [dry-run] probe a policyAuthenticated route: expect "
                f"{'401' if mode == 'enabled' else 'not 401'}")
        return
    original = path.read_text(encoding="utf-8")
    if ctx.security_backup is None:
        ctx.security_backup = original
    lines = original.splitlines(keepends=True)
    out, in_auth, done = [], False, False
    for line in lines:
        if line.startswith("auth:"):
            in_auth = True
        elif line and not line[0].isspace() and not line.startswith("#"):
            in_auth = False
        stripped = line.strip()
        if (in_auth and not done and not stripped.startswith("#")
                and stripped.startswith("mode:")):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}mode: {mode}\n")
            done = True
            continue
        out.append(line)
    if not done:
        raise ArcAbortedError("could not find the auth.mode setting line")
    path.write_text("".join(out), encoding="utf-8")
    ctx.say(f"    ~ config/security.yaml -> auth.mode: {mode}")
    ctx.run(["docker", "compose", "restart", "orchestrator"],
            why="the running process keeps the mode it booted with",
            critical=True)
    wait_healthy(ctx)
    verify_auth_live(ctx, mode)


def verify_auth_live(ctx: Ctx, mode: str) -> None:
    """Prove the RESTARTED process actually booted with *mode*.

    `wait_healthy` proves only that something answers, and it cannot tell a
    restart that took from one that never happened — the old process answers
    `/healthz` just as fast, and faster. That is the file-versus-process
    divergence `gate_auth_live` exists to draw, so reuse the same 401 probe
    here rather than trusting an exit code.

    It matters in both directions. If Leg 8's flip to `disabled` does not
    take, the new rows carry `alice-person` instead of `local` and the leg's
    central assertion reads as the fix failing. If Leg 9's flip back to
    `enabled` does not take, the `alice-person` partition never grows and the
    "A → B grows" bar goes flat — which the leg text itself warns is a
    wrong-looking result.
    """
    gate = pf.gate_auth_live(ctx.server, expected=mode)
    ctx.say(gate.render())
    if not gate.ok:
        raise ArcAbortedError(
            f"config/security.yaml now reads auth.mode: {mode}, but the "
            f"running orchestrator disagrees ({gate.detail}). Every later leg "
            f"would read the wrong tenant."
        )


def restore_config(ctx: Ctx) -> None:
    """Put `config/security.yaml` back exactly as the arc found it.

    Called from the driver's ``finally``, so it runs on the clean path, on an
    abort, and on a Ctrl-C alike. Leaving a tracked config modified is not a
    cosmetic problem here: the next `git add -A` sweeps a run knob into a
    commit, which has already happened once in this repo.
    """
    if ctx.security_backup is None:
        return
    if not ctx.execute:
        return
    SECURITY_CONFIG.write_text(ctx.security_backup, encoding="utf-8")
    ctx.security_backup = None
    ctx.say("    ~ config/security.yaml restored to its pre-arc contents")


def wait_healthy(ctx: Ctx, timeout: int = 180) -> None:
    """Poll /healthz instead of sleeping a fixed interval.

    A fixed `sleep 25` after `compose restart orchestrator` is a coin flip:
    the first live run of this driver lost a leg to `connection failed` on
    the login that followed it. The MT's own guidance is to *wait for the
    orchestrator to answer /healthz before publishing*, which is what this
    does. The RFC 0009 rate-limit bucket is NOT flushed by a restart, so the
    first turn afterwards can still draw a 429 for ~60s — healthy is
    necessary, not sufficient.
    """
    if not ctx.execute:
        ctx.say(f"    [dry-run] poll {ctx.server}/healthz until 200 "
                f"(max {timeout}s)")
        return
    ctx.say(f"    … polling {ctx.server}/healthz")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{ctx.server}/healthz", timeout=5) as r:  # noqa: S310
                if r.status == 200:
                    ctx.say("    ✓ orchestrator healthy")
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    raise ArcAbortedError(f"orchestrator did not become healthy within {timeout}s")


def mt_password() -> str:
    """The throwaway credential for this arc's test principals.

    A local fixture, not a secret worth protecting — but it still never goes
    in argv (§J) and is never written to the repo. Override with
    ``PERSATRIX_MT_PASSWORD``; otherwise one is generated per run and printed
    once, so an operator can log in by hand afterwards. The 12-character
    floor is enforced by bootstrap itself.
    """
    supplied = os.environ.get("PERSATRIX_MT_PASSWORD")
    if supplied:
        return supplied
    return "mt-" + secrets.token_urlsafe(15)


def bootstrap_account(ctx: Ctx, username: str, participant: str) -> None:
    """Rotate the accounts store IN THE CONTAINER, then log in — unattended.

    Both credential verbs fall back to reading one line per prompt when stdin
    is not a terminal (the documented provisioning pipe), so the whole arc
    runs without a human at the keyboard. Bootstrap asks twice (password +
    confirm); login asks once.
    """
    ctx.run(
        ["docker", "compose", "exec", "-T", "orchestrator",
         "rm", "-f", *ACCOUNTS_SIDECARS],
        why="remove the db AND its -wal/-shm, or bootstrap fails with "
            "disk I/O error (522) — v0.3.14 F-4",
    )
    ctx.run(
        ["docker", "compose", "exec", "-T", "orchestrator",
         "persatrix-server", "account", "bootstrap",
         "--accounts-db", CONTAINER_ACCOUNTS_DB,
         "--username", username, "--participant", participant],
        why=f"create {participant} where the orchestrator actually reads it",
        stdin=f"{ctx.password}\n{ctx.password}\n", secret=True, critical=True,
    )
    # NOT optional and NOT the caller's job: the orchestrator holds the
    # deleted accounts.db inode until it reopens, so a login before this
    # restart fails `invalid credentials` against an account that plainly
    # exists on disk. Folded in here because Leg 9 assembled the sequence
    # without it and lost a run to exactly that.
    ctx.run(["docker", "compose", "restart", "orchestrator"],
            why="reopen the rotated accounts.db")
    wait_healthy(ctx)


def login(ctx: Ctx, username: str) -> None:
    """Authenticate the CLI as *username* over the same provisioning pipe."""
    ctx.run([CLI, "login", "--username", username],
            why=f"the CLI must hold {username}'s session for the sends below",
            stdin=f"{ctx.password}\n", secret=True, critical=True)


def arm_room(ctx: Ctx, room: str) -> None:
    """Arm a channel's `autonomous` block, and remember to put it back.

    `roundtable` ships DISARMED on purpose: `config/channels.yaml` says an
    armed autonomous channel "would spend real tokens with no human present",
    convenable on any boot, with no enforced cost cap. Leg 5 has to arm it to
    test the travel path — so the arming is a run knob like the collector's
    sampling percentage, and every other run knob in this arc is reverted.
    """
    ctx.run([CLI, "channel", "config", "set", f"group:{room}",
             "autonomous.enabled=true"],
            why="convene 409s on a disarmed channel", critical=True)
    if room not in ctx.armed_rooms:
        ctx.armed_rooms.append(room)


def disarm_rooms(ctx: Ctx) -> None:
    """Disarm every channel this arc armed. Called from the driver's ``finally``.

    Sets `false` explicitly rather than `channel config unset`: this arc's own
    finding F-3 records that `unset` falls back to the FLEET default, not to
    the channel's YAML, so `unset` is not a reliable way back to `disabled`.
    Store overrides are canonical over YAML, so reverting `config/` — which a
    previous run did by hand — never touched this at all.
    """
    for room in list(ctx.armed_rooms):
        ctx.run([CLI, "channel", "config", "set", f"group:{room}",
                 "autonomous.enabled=false"],
                why=f"leave {room} disarmed — an armed channel spends on any boot")
        ctx.armed_rooms.remove(room)


def send_as(ctx: Ctx, participant: str, room: str, body: str,
            critical: bool = False) -> str:
    """Publish as *participant*. The MT omits `--as` and the default is wrong.

    `channel send`'s sender identity defaults to the **OS username**,
    normalized — not to the authenticated principal. The MT writes Leg 1 as a
    bare `persatrix channel send planning "..."`, which publishes as whoever
    is running the script and 403s on a room they are not in. Worse, if the
    operator's OS username *did* happen to be a member, the arc would run
    green with the turns attributed to the wrong speaker — and ISSUE-0131's
    speaker axis is read off the event's `sender_id`, so Leg 4's triples
    would name a speaker who never spoke.

    `sender_id` and the principal are separate axes: this sets the former,
    while the latter comes from the authenticated session (`login` above).
    Alice's turns need both to be `alice-person`.
    """
    return ctx.run([CLI, "channel", "send", room, body, "--as", participant],
                   why=f"publish as {participant} — NOT the OS username",
                   critical=critical)
