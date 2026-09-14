#!/usr/bin/env python3
"""Operations vocabulary for the MT-PERSONA-CONFIDENTIALITY-001 driver.

The verbs the arc is phrased in: apply and revert the two run knobs, bring
the stack up on the Anthropic overlay, publish as a named human with an
@-mention, hold a DM turn through the chat REPL, and wait for the persona's
reply instead of sleeping past it. The account rotation, the auth flip, the
health poll and the throwaway credential are the GROUP-TENANT verbs
(``mt_group_tenant_ops.py``), imported — each of them was learned by a lost
arc and none is worth learning twice.

Two run knobs, both reverted however the arc ends:

* ``config/channels.yaml`` gains the ``restricted`` war room and the three
  declared humans (never ``channel join`` — a runtime join diverges the store
  from the config and the next orchestrator restart FATALs on reconcile).
* A compose overlay puts ``agent-ember-owl`` at ``--log-level DEBUG`` with
  ``PERSATRIX_MEMORY_PROVENANCE=1``: the per-entry ``candidates`` array, the
  B2 ``reason_note`` record and the B1 admission line all need it. The
  overlay lives outside the repo and is named through ``COMPOSE_FILE`` so
  every later ``docker compose`` verb addresses the same project.

Nothing here decides pass/fail, and nothing runs unless ``Ctx.execute`` is set.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scripts.manual_tests import mt_gate
from scripts.manual_tests.mt_group_tenant_ops import (  # noqa: F401  (re-exported verbs)
    CLI,
    REPO_ROOT,
    ArcAbortedError,
    Ctx,
    bootstrap_account,
    cli_cmd,
    login,
    mt_password,
    restore_config,
    set_auth_mode,
    wait_healthy,
)

CHANNELS_CONFIG = REPO_ROOT / "config" / "channels.yaml"
COMPOSE_BASE = REPO_ROOT / "docker-compose.yaml"
COMPOSE_PROVIDER = REPO_ROOT / "docker-compose.anthropic.yaml"

PERSONA = "ember-owl"
AGENT_SERVICE = f"agent-{PERSONA}"
WARROOM = "warroom"
PLANNING = "planning"
OPERATOR = "alex"   # Legs 1-4, the v0.3.12 baseline's operator identity
ALICE = "alice"     # Leg 5's teacher AND asker (scope lock 6)
BOB = "bob"         # Leg 5's audience; never speaks, is in nothing else
HUMANS_IN_PLANNING = (OPERATOR, ALICE, BOB)

REPLY_TIMEOUT = 240   # a quality-model turn with tools can take a while
POLL = 5
_KNOB_MARK = "# !!! TEMPORARY — MT-PERSONA-CONFIDENTIALITY-001 run knob, REVERT !!!"

WARROOM_BLOCK = f"""
{_KNOB_MARK}
  - name: {WARROOM}
    description: "MT-PERSONA-CONFIDENTIALITY-001 — restricted room"
    classification: restricted
    members:
      - id: {PERSONA}
        respond: addressed
      - id: {OPERATOR}
        respond: observer
"""


@dataclass
class ArcCtx(Ctx):
    """The GROUP-TENANT context plus this arc's own state."""

    #: `config/channels.yaml` as the arc found it (restored in `finally`).
    channels_backup: str | None = None
    #: The generated compose overlay, outside the repo.
    overlay: Path | None = None
    #: Leg 4's seed — the `restricted` episode summary Leg 1 stored.
    seed: str = ""


# ── run knob 1: the channel config ──────────────────────────────────────────

def apply_channel_run_knobs(text: str) -> str:
    """Declare the war room and the three humans, textually.

    Textual so the file's comments survive and the revert is a byte-for-byte
    restore. Refuses a second application: a duplicate member fails the
    loader at the next boot, after the store was already seeded.
    """
    if _KNOB_MARK in text or re.search(rf"^\s+- id: {OPERATOR}\s*$", text, re.M):
        raise ValueError("the channel run knobs are already applied")
    planning = re.search(rf"^  - name: {PLANNING}\s*$", text, re.M)
    if not planning:
        raise ValueError(f"no `- name: {PLANNING}` block in channels.yaml")
    members = re.compile(r"^    members:\s*$", re.M).search(text, planning.end())
    if not members:
        raise ValueError(f"`{PLANNING}` has no `members:` list")
    humans = "".join(f"      - id: {h}\n        respond: observer\n"
                     for h in HUMANS_IN_PLANNING)
    out = text[: members.end() + 1] + humans + text[members.end() + 1:]
    if not out.endswith("\n"):
        out += "\n"
    return out + WARROOM_BLOCK


def apply_channels(ctx: ArcCtx) -> None:
    if not ctx.execute:
        ctx.say("    [dry-run] declare group:warroom (restricted; ember-owl, alex) and "
                "alex/alice/bob as observers in planning — config/channels.yaml")
        return
    original = CHANNELS_CONFIG.read_text(encoding="utf-8")
    if ctx.channels_backup is None:
        ctx.channels_backup = original
    try:
        CHANNELS_CONFIG.write_text(apply_channel_run_knobs(original), encoding="utf-8")
    except ValueError as exc:
        raise ArcAbortedError(str(exc)) from exc
    ctx.say("    ~ config/channels.yaml -> war room + three humans declared")


def restore_channels(ctx: ArcCtx) -> None:
    """Put `config/channels.yaml` back exactly as the arc found it."""
    if ctx.channels_backup is None or not ctx.execute:
        return
    CHANNELS_CONFIG.write_text(ctx.channels_backup, encoding="utf-8")
    ctx.channels_backup = None
    ctx.say("    ~ config/channels.yaml restored to its pre-arc contents")
    ctx.say("      (the running store still holds the run-knob rooms — "
            "`make reset` before the next boot, or reconcile FATALs)")


# ── run knob 2: the compose overlay ─────────────────────────────────────────

def compose_overlay_text(base_text: str) -> str:
    """An overlay putting ember-owl at DEBUG with the provenance switch on.

    Docker *replaces* ``command`` rather than appending, so the whole base
    command rides along; ``environment`` likewise, so the provider key stays
    plumbed. No ports — the compose host-publish guard applies to overlays.
    """
    base = yaml.safe_load(base_text) or {}
    svc = (base.get("services") or {}).get(AGENT_SERVICE) or {}
    command = list(svc.get("command") or [])
    if "--log-level" in command:
        i = command.index("--log-level")
        command[i + 1] = "DEBUG"
    else:
        command += ["--log-level", "DEBUG"]
    env = [e for e in (svc.get("environment") or [])
           if not str(e).startswith("PERSATRIX_MEMORY_PROVENANCE=")]
    env.append("PERSATRIX_MEMORY_PROVENANCE=1")
    overlay = {"services": {AGENT_SERVICE: {"command": command, "environment": env}}}
    return ("# Generated by scripts/manual_tests/mt_confidentiality_ops.py — "
            "MT-PERSONA-CONFIDENTIALITY-001 run knob, not for commit.\n"
            + yaml.safe_dump(overlay, sort_keys=False))


def write_overlay(ctx: ArcCtx) -> None:
    """Write the overlay and point every later compose verb at it."""
    if not ctx.execute:
        ctx.say("    [dry-run] write the DEBUG + provenance overlay for agent-ember-owl "
                "and export COMPOSE_FILE=base:anthropic:overlay")
        return
    path = Path(tempfile.mkdtemp(prefix="mt-confidentiality-")) / "overlay.yaml"
    path.write_text(compose_overlay_text(COMPOSE_BASE.read_text(encoding="utf-8")),
                    encoding="utf-8")
    ctx.overlay = path
    os.environ["COMPOSE_FILE"] = os.pathsep.join(
        str(p) for p in (COMPOSE_BASE, COMPOSE_PROVIDER, path))
    ctx.say(f"    ~ overlay written: {path}")


# ── the stack ───────────────────────────────────────────────────────────────

def stack_up(ctx: ArcCtx) -> None:
    """A clean store, then the Anthropic stack with the overlay, then health."""
    ctx.run(["docker", "compose", "down", "-v", "--remove-orphans"],
            why="a clean store — prior facts would steer the run (precondition 2)",
            timeout=300)
    # Build first, separately: a fresh checkout builds the orchestrator's Go
    # and Node stages and three agent images from scratch, which took the
    # first live attempt past a 900 s `up --build` bound and aborted the arc
    # before a cent was spent. The build gets an hour; the `up` stays short.
    ctx.run(["docker", "compose", "build"],
            why="the images at the RC tip (a cold build takes many minutes)",
            timeout=3600, critical=True)
    ctx.run(["docker", "compose", "up", "-d"],
            why="the society on Anthropic with ember-owl at DEBUG",
            timeout=300, critical=True)
    wait_healthy(ctx)
    wait_registered(ctx)


def wait_registered(ctx: ArcCtx, timeout: int = 180) -> None:
    """The persona must be registered, or every publish is dropped silently."""
    if not ctx.execute:
        ctx.say(f"    [dry-run] poll {ctx.server}/api/v1/agents until {PERSONA} registers")
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = mt_gate.get_json(f"{ctx.server}/api/v1/agents", ctx.server)
        except (urllib.error.URLError, OSError, ValueError):
            payload = None
        agents = payload.get("agents", payload) if isinstance(payload, dict) else payload
        ids = {str(a.get("id") or a.get("agent_id") or "")
               for a in (agents or []) if isinstance(a, dict)}
        if PERSONA in ids:
            ctx.say(f"    ✓ {PERSONA} registered ({len(ids)} agents)")
            return
        time.sleep(POLL)
    raise ArcAbortedError(f"{PERSONA} did not register within {timeout}s")


def channel(ctx: ArcCtx, channel_id: str) -> dict[str, Any]:
    """GET /api/v1/channels/{id} — members and classification."""
    payload = mt_gate.get_json(f"{ctx.server}/api/v1/channels/{channel_id}", ctx.server)
    return payload if isinstance(payload, dict) else {}


# ── turns ───────────────────────────────────────────────────────────────────

def messages(ctx: ArcCtx, channel_id: str, limit: int = 30) -> list[dict[str, Any]]:
    """GET /api/v1/channels/{id}/messages — newest first."""
    try:
        payload = mt_gate.get_json(
            f"{ctx.server}/api/v1/channels/{channel_id}/messages?limit={limit}",
            ctx.server, timeout=15)
    except (urllib.error.URLError, OSError, ValueError):
        return []
    rows = payload.get("messages", []) if isinstance(payload, dict) else payload
    return [m for m in (rows or []) if isinstance(m, dict)]


def seen_ids(ctx: ArcCtx, channel_id: str) -> set[str]:
    return {str(m.get("id", "")) for m in messages(ctx, channel_id)} if ctx.execute else set()


def wait_for_reply(ctx: ArcCtx, channel_id: str, before: set[str],
                   sender: str = PERSONA, timeout: int = REPLY_TIMEOUT) -> dict[str, Any] | None:
    """The persona's first message in *channel_id* that was not there before.

    Polling the history beats a fixed sleep in both directions: a turn that
    lands in twenty seconds is read at once, and one that takes three minutes
    is not read half-written. ``None`` — no reply — is recorded as such; the
    driver never turns silence into a pass.
    """
    if not ctx.execute:
        ctx.say(f"    [dry-run] poll {channel_id} for a reply from {sender} (max {timeout}s)")
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for m in reversed(messages(ctx, channel_id)):
            if str(m.get("id", "")) not in before and str(m.get("sender_id", "")) == sender:
                return m
        time.sleep(POLL)
    return None


def run_capture(ctx: ArcCtx, cmd: list[str], *, why: str,
                timeout: int = 120) -> subprocess.CompletedProcess[str] | None:
    """Like ``Ctx.run`` but hands back the whole process, stderr included.

    ``Ctx.run`` returns stdout only, and the CLI reports a refused publish
    on stderr — so a caller that needs to tell a 429 from a 403 cannot use it.
    ``None`` in a dry run.
    """
    printable = " ".join(cmd)
    if not ctx.execute:
        ctx.say(f"    [dry-run] {printable}")
        ctx.say(f"              ({why})")
        return None
    ctx.say(f"    $ {printable}")
    try:
        return subprocess.run(  # noqa: S603
            cmd, cwd=REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ArcAbortedError(f"{printable} -> {type(exc).__name__}: {exc}") from exc


def send_mention(ctx: ArcCtx, sender: str, room: str, body: str,
                 mention: str = PERSONA, critical: bool = True) -> str:
    """Publish as *sender* with an @-mention, retrying a post-restart 429.

    The RFC 0009 rate-limit bucket is not flushed by an orchestrator restart,
    so the first turn after one can draw a 429 for about a minute; a turn the
    arc never sent is not a persona that stayed quiet.
    """
    cmd = cli_cmd(ctx, "channel", "send", room, body, "--as", sender, "--mention", mention)
    detail = ""
    for attempt in range(6):
        proc = run_capture(ctx, cmd, why=f"publish as {sender} in {room}")
        if proc is None or proc.returncode == 0:
            return proc.stdout if proc else ""
        detail = (proc.stderr.strip() or proc.stdout.strip())[:400]
        ctx.say(f"    ! exit {proc.returncode}: {detail}")
        if "429" not in detail:
            break
        ctx.pause(20, f"rate limited — retry {attempt + 1}/5")
    if critical:
        raise ArcAbortedError(f"publish as {sender} in {room} failed: {detail}")
    return ""


def turn(ctx: ArcCtx, sender: str, room: str, body: str,
         mention: str = PERSONA) -> tuple[str, dict[str, Any] | None]:
    """One addressed group turn: publish, then wait for the persona's reply."""
    channel_id = f"group:{room}"
    before = seen_ids(ctx, channel_id)
    send_mention(ctx, sender, room, body, mention)
    reply = wait_for_reply(ctx, channel_id, before)
    return channel_id, reply


def chat_as(ctx: ArcCtx, user: str, message: str) -> dict[str, Any] | None:
    """One DM turn as *user* through the chat REPL, unattended.

    The MT writes ``persatrix chat send … --as alice``; no such verb exists.
    ``persatrix chat <agent> --user <id>`` is a REPL that reads stdin line by
    line and exits on EOF, so one piped line is one turn. Under
    ``auth.mode: enabled`` the verified claim replaces the body's ``user_id``
    (RFC 0039 §F), which is why Leg 5's account is bootstrapped with the
    participant ``alice`` — the same id the DM is keyed by under ``disabled``.
    """
    dm = f"dm:{':'.join(sorted((user, PERSONA)))}"
    before = seen_ids(ctx, dm)
    if not ctx.execute:
        ctx.say(f"    [dry-run] {CLI} chat {PERSONA} --user {user}  <<< {message!r}")
        return None
    env = {**os.environ, "NO_COLOR": "1"}
    ctx.say(f"    $ {CLI} --server {ctx.server} chat {PERSONA} --user {user}  <<< (one line)")
    try:
        proc = subprocess.run(  # noqa: S603
            cli_cmd(ctx, "chat", PERSONA, "--user", user), cwd=REPO_ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=330, check=False, input=message + "\n", env=env,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ArcAbortedError(f"chat as {user} -> {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip())[:400]
        raise ArcAbortedError(f"chat as {user} -> exit {proc.returncode}: {detail}")
    if "error:" in proc.stderr:
        ctx.say(f"    ! {proc.stderr.strip()[:300]}")
    return wait_for_reply(ctx, dm, before, timeout=60)


def render_message(m: dict[str, Any] | None, label: str) -> str:
    if m is None:
        return f"_{label}: **no reply from {PERSONA} within the wait** — recorded, not inferred._"
    return (f"{label} — `{m.get('sender_id', '?')}` at `{m.get('timestamp', '?')}`:\n\n"
            f"> {json.dumps(m.get('content', ''))[1:-1]}")


def utc_now() -> str:
    """An RFC 3339 UTC stamp `docker compose logs --since` accepts."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
