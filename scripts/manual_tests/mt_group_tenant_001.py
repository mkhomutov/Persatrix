#!/usr/bin/env python3
"""Machine-paced driver for MT-MEMORY-GROUP-TENANT-001 (the v0.3.15 gate).

**Why this exists.** The MT says to drive the arc from one script, and the
reason is specific: the end-vote quorum is counted over a 600s / W=3 window,
so an idle pause mid-arc expires the window and silently changes *which close
trigger fires* — the exact variable Legs 4 and 6 turn on. A human reading legs
between steps is slow enough to change the result. Two live arcs have already
been lost this way or to a mute fleet, both on a paid provider.

**Dry run by default.** ``--execute`` is required to touch anything. The dry
run prints the full plan with its pacing and proves the script's own wiring
without spending a cent, which is how it should be reviewed before the real
arc. ``--legs`` runs a subset (``--legs 0-4``, ``--legs 9``).

**What it does not do.** It does not decide pass/fail. It drives the arc,
collects the three evidence artifacts, and writes them to
``--out``; the operator reads them against the MT's expected columns and
writes the report. A script that graded itself would be the same mistake as a
leg that passes vacuously.

Legs 8 and 9 are deliberately kept apart: Leg 8 restarts the **orchestrator**
(ISSUE-0125's live proof — ``GET /api/v1/agents`` non-empty afterwards), Leg 9
restarts the **agents** (catch-up replay runs from ``AgentServer.start()``, so
restarting the orchestrator replays nothing and Leg 9 would pass with the
shape-(b) guard deleted).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.manual_tests import mt_group_tenant_evidence as ev  # noqa: E402
from scripts.manual_tests import mt_group_tenant_preflight as pf  # noqa: E402
from scripts.manual_tests.mt_group_tenant_ops import (  # noqa: E402
    CLI,
    ROOM,
    ArcAbortedError,
    Ctx,
    bootstrap_account,
    login,
    mt_password,
    send_as,
    set_auth_mode,
)

SECOND_ROOM = "roundtable"
PERSONAS = ev.PERSONAS

# Setup corrections live in mt_group_tenant_ops.py. The MT's v1.1 setup was
# written for a HOST orchestrator and does not work
# against the compose stack. The seven corrections this driver implements —
# in-container bootstrap, the -wal/-shm sidecars, declared-not-joined
# membership, mandatory `--as`, the Leg 1 check, the Leg 4 query lens, and the
# Leg 3 fallback — are catalogued with their symptoms in
# docs/manual-tests/v0.3.15-execution-report.md (§MT corrections) and fixed in
# the MT at v1.2. Only the values they resolve to live here.
CONTAINER_ACCOUNTS_DB = "/var/lib/persatrix/accounts.db"
ACCOUNTS_SIDECARS = (
    CONTAINER_ACCOUNTS_DB,
    f"{CONTAINER_ACCOUNTS_DB}-wal",
    f"{CONTAINER_ACCOUNTS_DB}-shm",
)


# Pacing. The end-vote window is 600s/W=3; these gaps keep the arc inside it
# while still letting a round settle. Raising SETTLE is safe; lowering it
# risks reading a cascade that has not finished fanning out.
SETTLE = 45  # seconds to let a round settle before reading or publishing again
RESTART_WAIT = 25  # after a compose restart, before polling /healthz

ALICE_DISCLOSURE = (
    "Before we plan Q3 — I'll be out the week of the 14th, my daughter Mira "
    "has surgery. @ember-owl can you take the review slot?"
)
WRAP_PROMPT = "Anything else before we wrap?"
BOB_PROBE = "Who's covering the review slot this month?"
ROUNDTABLE_AGENDA = "Draft the Q3 review rota."


def leg0(ctx: Ctx) -> None:
    """Alice, authenticated. Bootstraps accounts.db and rotates the wire id."""
    ctx.say("\nLeg 0 — Alice, authenticated")
    ctx.say("    NOTE: `account bootstrap` refuses once an account exists, so a")
    ctx.say("    second principal (Leg 7) means deleting accounts.db and")
    ctx.say("    restarting — which rotates the wire interaction id and")
    ctx.say("    structurally closes open records. That is why the two-human")
    ctx.say("    aggregate is pinned by the R-1 unit gate, not by this arc.")
    bootstrap_account(ctx, "alice", "alice-person")
    login(ctx, "alice")


def leg1(ctx: Ctx) -> None:
    """Alice discloses; a cascade must run at least two hops."""
    ctx.say("\nLeg 1 — Alice discloses into the room, and a cascade runs")
    send_as(ctx, "alice-person", ROOM, ALICE_DISCLOSURE, critical=True)
    ctx.pause(SETTLE, "let ember-owl reply and at least one further hop land")
    history = ctx.run([CLI, "channel", "history", ROOM],
                      why="Leg 1 needs >= 2 hops: a persona replying to a persona")
    if history:
        ctx.record("Leg 1 — channel history", f"```\n{history.strip()}\n```")
    ctx.say("    CHECK: at least two hops, and each persona's memory.db has an")
    ctx.say("    `alice-person` episodes row. If every row reads `local`,")
    ctx.say("    emission is not reaching the personas — STOP and diagnose,")
    ctx.say("    or every later leg passes vacuously.")


def leg2(ctx: Ctx) -> None:
    """R-2 — read the wire, not storage."""
    ctx.say("\nLeg 2 — R-2: the relayed write (WIRE ONLY — storage cannot see this)")
    if ctx.execute:
        ctx.record("Leg 2 — per-dispatch principal table",
                   ev.collect_leg2(ctx.jaeger).render())
    else:
        ctx.say(f"    [dry-run] GET {ctx.jaeger}/api/traces?...operation=channel.dispatch")
        ctx.say("              (per-dispatch principal.id table, grouped by "
                "originating message)")


def leg3(ctx: Ctx) -> None:
    """Close from a persona publish — the trigger matters."""
    ctx.say("\nLeg 3 — close the interaction from a *persona* publish")
    send_as(ctx, "alice-person", ROOM, WRAP_PROMPT)
    ctx.pause(SETTLE, "let the quorum form")
    ctx.say("    CHECK: orchestrator logs show `interaction_closed{…end_votes}`")
    ctx.say("    or `synthesis_reply` — NOT `structural` / `cost`. A")
    ctx.say("    mis-identified trigger inverts the Leg 4 expected column.")


def leg4(ctx: Ctx) -> None:
    """R-1 + ISSUE-0131 — the triples, and the row count."""
    ctx.say("\nLeg 4 — R-1: the derived write (the TRIPLES are the finding)")
    if ctx.execute:
        ctx.record("Leg 4 — (principal_id, speaker_id, summary) triples",
                   ev.collect_leg4())
    else:
        ctx.say("    [dry-run] query each persona's memory.db for episodes")
        ctx.say("              WHERE turn_count > 1, plus the newest 20 facts")
        ctx.say("              expect THREE local rows (one per agent speaker)")


def leg5(ctx: Ctx) -> None:
    """The travel — an absence bar post-fix."""
    ctx.say("\nLeg 5 — the travel: does shared-tenant content reach another room?")
    # MT correction 8: `roundtable` ships DISARMED (a default-deploy safety
    # posture — an armed channel is convenable, and spends, on any boot), and
    # `convene` takes NO topic argument; the agenda comes from the channel's
    # own `autonomous` block. The MT's `convene roundtable "<topic>"` fails
    # twice over. Arm at runtime, the MT-AUTONOMOUS-001 operator flow.
    ctx.run([CLI, "channel", "config", "set", f"group:{SECOND_ROOM}",
             "autonomous.enabled=true"],
            why="convene 409s on a disarmed channel", critical=True)
    ctx.run([CLI, "channel", "convene", f"group:{SECOND_ROOM}"],
            why="agent-origin turns in another room — the leak path Leg 5 tests",
            critical=True)
    ctx.pause(SETTLE * 3, "let the autonomous discussion run (max_rounds: 8)")
    transcript = ctx.run([CLI, "channel", "history", SECOND_ROOM],
                         why="the transcript is pasted verbatim into the report")
    if transcript:
        ctx.record("Leg 5 — roundtable transcript", f"```\n{transcript.strip()}\n```")


def leg6(ctx: Ctx) -> None:
    """The other close trigger — cross the bound instead."""
    ctx.say("\nLeg 6 — the other close trigger, the other direction")
    ctx.say("    Re-run Legs 1-3 but close by CROSSING THE BOUND (raise traffic")
    ctx.say("    until max_rounds fires, or lower max_rounds) so the close")
    ctx.say("    descends from Alice's own publish. Post-fix this must be")
    ctx.say("    IDENTICAL to Leg 4: the record binds its own frozen principal,")
    ctx.say("    so the close trigger no longer selects a tenant.")
    send_as(ctx, "alice-person", ROOM, "Let's keep going on the rota details.")
    ctx.pause(SETTLE, "let the bound-crossing close fire")
    if ctx.execute:
        ctx.record("Leg 6 — triples after a bound-crossing close", ev.collect_leg4())


def leg7(ctx: Ctx) -> None:
    """Bob — the absence bar for a second human."""
    ctx.say("\nLeg 7 — Bob: the absence bar")
    bootstrap_account(ctx, "bob", "bob-person")
    login(ctx, "bob")
    send_as(ctx, "bob-person", ROOM, BOB_PROBE)
    ctx.pause(SETTLE, "let Bob's round settle")
    ctx.say("    CHECK: alice-person, bob-person and local coexist in episodes —")
    ctx.say("    isolation is a recall filter, not a delete.")


def leg8(ctx: Ctx) -> None:
    """auth.mode: disabled — and ISSUE-0125's live proof."""
    ctx.say("\nLeg 8 — auth.mode: disabled (and the ISSUE-0125 orchestrator restart)")
    set_auth_mode(ctx, "disabled")   # this IS the ISSUE-0125 orchestrator restart
    ctx.pause(RESTART_WAIT, "the fleet must re-register itself (ISSUE-0125)")
    agents = ctx.run(["curl", "-s", f"{ctx.server}/api/v1/agents"],
                     why="ISSUE-0125: non-empty WITHOUT touching an agent process")
    if agents:
        ctx.record("Leg 8 — registry after an orchestrator restart (ISSUE-0125)",
                   f"```json\n{agents.strip()[:2000]}\n```")
    send_as(ctx, "alice-person", ROOM, ALICE_DISCLOSURE)
    ctx.pause(SETTLE, "let the unauthenticated round settle")
    ctx.say("    CHECK: every new row principal_id='local'; NO principal.id span")
    ctx.say("    attribute (absent, not empty). Row count is NOT unchanged from")
    ctx.say("    v0.3.14 — the speaker split is auth-independent, so N personas")
    ctx.say("    now close N records where v0.3.14 closed one.")


def leg9(ctx: Ctx) -> None:
    """The replayed write — three snapshots, and B must equal C."""
    ctx.say("\nLeg 9 — the replayed write: a restart derives nothing twice")
    # Leg 8 leaves auth off and the newest traffic stamped `local`; the
    # partition this leg reads FIRST is `alice-person`. Restore and re-post
    # as alice, or it goes flat for a reason unrelated to the guard.
    set_auth_mode(ctx, "enabled")
    # MT correction 9: Leg 7 rotated the accounts store to BOB, and
    # `account bootstrap` is the only shipped account-creation verb — so alice
    # does not exist by the time Leg 9 runs, and the MT's "re-authenticate as
    # alice" is impossible as written. Rotate back. The restart this performs
    # also structurally closes whatever Leg 8 left open, which is how the
    # `local` partition acquires the records Leg 8 asserts.
    bootstrap_account(ctx, "alice", "alice-person")
    login(ctx, "alice")
    send_as(ctx, "alice-person", ROOM,
            "One more thing before I go — I'll send the agenda tomorrow.")
    ctx.pause(SETTLE, "let alice-person traffic land as the newest in the window")
    if ctx.execute:
        ctx.record("Leg 9 — snapshot A (before any restart)", ev.collect_leg9("A"))
    agent_services = [f"agent-{p}" for p in PERSONAS]
    ctx.run(["docker", "compose", "restart", *agent_services],
            why="the AGENTS — catch-up replay runs from AgentServer.start()")
    ctx.pause(RESTART_WAIT + 20, "catch-up replay to run and settle")
    if ctx.execute:
        ctx.record("Leg 9 — snapshot B (after restart 1)", ev.collect_leg9("B"))
    ctx.run(["docker", "compose", "restart", *agent_services],
            why="second restart, NO traffic in between")
    ctx.pause(RESTART_WAIT + 20, "catch-up replay again")
    if ctx.execute:
        ctx.record("Leg 9 — snapshot C (after restart 2, no traffic between)",
                   ev.collect_leg9("C"))
    ctx.say("    BAR: A → B GROWS (replay derives the window once, in")
    ctx.say("    alice-person, with `replay-` interaction ids); B = C exactly.")
    ctx.say("    Do NOT expect A = B — that bar fails on correct code.")
    ctx.say("\n    Then: send ONE message, restart a third time. New rows MUST")
    ctx.say("    appear and at least one MUST carry a `replay-` id — otherwise")
    ctx.say("    'nothing was written' cannot be told apart from replay having")
    ctx.say("    stopped deriving at all.")


LEGS = {0: leg0, 1: leg1, 2: leg2, 3: leg3, 4: leg4,
        5: leg5, 6: leg6, 7: leg7, 8: leg8, 9: leg9}


def _write_artifacts(ctx: Ctx, execute: bool, partial: bool = False) -> None:
    """Persist whatever was collected. Called on the clean path AND on abort."""
    if not execute or not ctx.artifacts:
        return
    note = ("\n> **PARTIAL — the arc aborted before completing.** These are the "
            "legs that did finish; the rest were not run.\n") if partial else ""
    header = ("# MT-MEMORY-GROUP-TENANT-001 — collected evidence\n"
              f"{note}\n"
              "Paste these into `docs/manual-tests/v0.3.15-execution-report.md`.\n\n")
    ctx.out.write_text(header + "\n".join(ctx.artifacts))
    print(f"Evidence written to {ctx.out}")


def parse_legs(spec: str) -> list[int]:
    chosen: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            chosen.update(range(int(lo), int(hi) + 1))
        elif part:
            chosen.add(int(part))
    return sorted(n for n in chosen if n in LEGS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--execute", action="store_true",
                        help="actually drive the arc (default is a dry run)")
    parser.add_argument("--legs", default="0-9", help="e.g. 0-4, 9, 1,3,5")
    parser.add_argument("--server", default=pf.DEFAULT_SERVER)
    parser.add_argument("--jaeger", default=pf.DEFAULT_JAEGER)
    parser.add_argument("--out", default="mt-group-tenant-evidence.md")
    parser.add_argument("--skip-preflight", action="store_true")
    args = parser.parse_args(argv)

    legs = parse_legs(args.legs)
    mode = "EXECUTE (live, spends money)" if args.execute else "DRY RUN (no side effects)"
    print(f"MT-MEMORY-GROUP-TENANT-001 — {mode}")
    print(f"Legs: {', '.join(str(n) for n in legs)}")
    print("=" * 62)

    if not args.skip_preflight:
        gates = pf.run_gates(args.server, args.jaeger)
        for gate in gates:
            print(gate.render())
        failed = [g for g in gates if not g.ok]
        if failed and args.execute:
            print("\nPreflight FAILED — refusing to spend the arc. "
                  "Every failure above is a leg that would pass vacuously.")
            return 1
        if failed:
            print(f"\n({len(failed)} gate(s) failing — dry run continues anyway.)")

    password = mt_password()
    ctx = Ctx(execute=args.execute, server=args.server, jaeger=args.jaeger,
              out=Path(args.out), artifacts=[], password=password)
    if args.execute and not os.environ.get("PERSATRIX_MT_PASSWORD"):
        print(f"\nGenerated throwaway credential for this arc: {password}")
        print("(local test fixture; set PERSATRIX_MT_PASSWORD to pin it)")
    try:
        for number in legs:
            LEGS[number](ctx)
    except ArcAbortedError as exc:
        print(f"\nARC ABORTED — {exc}")
        print("Setup failed; later legs would have produced empty artifacts "
              "that read like real findings. Fix and re-run.")
        # Whatever DID complete is still evidence and was paid for. Discarding
        # it on the way out (the first version of this handler returned here)
        # means re-running earlier legs to recover artifacts that already
        # existed.
        _write_artifacts(ctx, args.execute, partial=True)
        return 1

    if args.execute:
        ctx.say("\nCost capture (before any teardown)")
        ctx.record("Live spend", ev.collect_cost())
    else:
        print("\n[dry-run] cost capture reads `actualUSD` off the orchestrator")
        print("          logs BEFORE teardown — see collect_cost() for why the")
        print("          v0.3.14 checklist's `\"op\":\"settle\"` grep finds no USD.")

    print("\n" + "=" * 62)
    if ctx.artifacts and args.execute:
        _write_artifacts(ctx, args.execute)
    elif not args.execute:
        print("Dry run complete — no commands were run and nothing was spent.")
        print("Re-run with --execute once preflight is green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
