#!/usr/bin/env python3
"""Evidence collectors for MT-MEMORY-GROUP-TENANT-001.

Three artifacts, one per residual, because each residual fails *green* in a
different way and no single reading covers them:

* **Leg 2 — the per-dispatch table.** R-2 is invisible to storage: R-1
  re-attributes the relayed turns at close, so a `principal_id` group-by reads
  clean over a run in which the tenant was dropped on every hop. The wire is
  the only instrument. A bare count is not the finding either — the *grouping
  by originating message* is, so the table keeps that column.
* **Leg 4 — the (principal_id, speaker_id, summary) triples.** With every
  persona sharing the `local` principal, `speaker_id` is the only column that
  distinguishes ISSUE-0131's three records from one row written three times.
  The row **count** is the assertion; the triples are the evidence.
* **Leg 9 — the partition snapshots.** Idempotence is an absence bar, and an
  absence bar is satisfied by any empty recall. So the snapshot is per
  ``(principal_id, speaker_id)`` in *both* partitions, and the `replay-`
  interaction-id prefix is what separates a replay derivation from the live
  close writes happening alongside it.

Read-only. Nothing here publishes, restarts, or reconfigures.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PERSONAS = ("ember-owl", "iron-fox", "nova-sparrow")

# The persona memory store resolves `data/memory.db` against the container
# WORKDIR (/app), and the agent image ships no sqlite3 CLI — so every query
# rides the runtime's own python.
CONTAINER_DB = "data/memory.db"


@dataclass
class DispatchSpan:
    """One `channel.dispatch` span, as Leg 2 reads it."""

    start_us: int
    recipient: str
    message_id: str
    principal: str  # "" == tenant lost

    @property
    def tenant_lost(self) -> bool:
        return not self.principal


@dataclass
class Leg2Evidence:
    spans: list[DispatchSpan] = field(default_factory=list)

    @property
    def lost(self) -> int:
        return sum(1 for s in self.spans if s.tenant_lost)

    def render(self) -> str:
        """The per-dispatch table, grouped by originating message."""
        if not self.spans:
            return (
                "_No `channel.dispatch` spans found._ Check the collector's "
                "`sampling_percentage` (1% by default drops them) and that "
                "`floor_control` is off — under floor control agent publishes "
                "never reach `Dispatch` at all."
            )
        by_message: dict[str, list[DispatchSpan]] = {}
        for span in sorted(self.spans, key=lambda s: s.start_us):
            by_message.setdefault(span.message_id, []).append(span)

        lines = [
            "| originating message | recipient | principal.id | tenant |",
            "|---|---|---|---|",
        ]
        for message_id, spans in by_message.items():
            for i, span in enumerate(spans):
                shown = message_id[:18] if i == 0 else "↳"
                principal = span.principal or "_(absent)_"
                verdict = "**LOST**" if span.tenant_lost else "carried"
                lines.append(
                    f"| `{shown}` | {span.recipient} | `{principal}` | {verdict} |"
                )
        total = len(self.spans)
        lines.append("")
        lines.append(
            f"**{total} dispatch(es), {self.lost} tenant-less.** "
            f"Post-fix expectation: **zero** tenant-less dispatches in the "
            f"causal chain. (v0.3.14 reference run: 6 with, 9 without.)"
        )
        return "\n".join(lines)


def collect_leg2(jaeger: str, lookback: str = "20m", limit: int = 300) -> Leg2Evidence:
    """Read `principal.id` off every `channel.dispatch` span."""
    url = (
        f"{jaeger}/api/traces?service=persatrix-server"
        f"&operation=channel.dispatch&limit={limit}&lookback={lookback}"
    )
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return Leg2Evidence()

    spans: list[DispatchSpan] = []
    for trace in payload.get("data", []):
        for span in trace.get("spans", []):
            if span.get("operationName") != "channel.dispatch":
                continue
            tags = {t.get("key"): t.get("value") for t in span.get("tags", [])}
            spans.append(
                DispatchSpan(
                    start_us=span.get("startTime", 0),
                    recipient=str(tags.get("recipient.agent_id", "?")),
                    message_id=str(tags.get("channel.message_id", "?")),
                    principal=str(tags.get("principal.id", "") or ""),
                )
            )
    return Leg2Evidence(spans)


def _agent_query(persona: str, sql: str, timeout: int = 40) -> list[tuple[Any, ...]]:
    """Run one read-only SQL statement inside a persona container.

    The image has no sqlite3 CLI, so this goes through the runtime's python.
    """
    script = (
        "import sqlite3, json; "
        f"c = sqlite3.connect({CONTAINER_DB!r}); "
        f"print(json.dumps(c.execute({sql!r}).fetchall()))"
    )
    proc = subprocess.run(  # noqa: S603
        ["docker", "compose", "exec", "-T", f"agent-{persona}", "python", "-c", script],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        return []
    try:
        return [tuple(row) for row in json.loads(proc.stdout.strip().splitlines()[-1])]
    except (ValueError, IndexError):
        return []


# The MT's Leg 4 query filters `WHERE turn_count > 1`, and that filter is a
# PRE-FIX lens: it was written to find the single merged multi-speaker
# aggregate the defect produced. Post-fix the record is keyed
# `(principal, speaker, scope)`, so an agent that spoke once owns a
# ONE-turn record — and the filter hides exactly the rows ISSUE-0131 exists
# to create. Observed live: every persona kept a 3-turn record for the human
# and 1-turn records per agent speaker; under the MT's query each store
# reported "1 row" and the speaker split was invisible.
#
# Read every close-derived row and let the caller judge. `interaction_id` is
# carried so replay-derived rows stay identifiable (the shape-(b) prefix).
LEG4_EPISODES = (
    "SELECT principal_id, speaker_id, turn_count, scope, substr(summary,1,200) "
    "FROM episodes ORDER BY speaker_id, turn_count DESC"
)
LEG4_FACTS = (
    "SELECT principal_id, speaker_id, subject, predicate, object "
    "FROM facts ORDER BY asserted_at DESC LIMIT 20"
)


def collect_leg4(personas: tuple[str, ...] = PERSONAS) -> str:
    """The triples. The row count is the assertion."""
    blocks: list[str] = []
    for persona in personas:
        rows = _agent_query(persona, LEG4_EPISODES)
        blocks.append(f"#### `{persona}` — close-derived episodes (all records)\n")
        if not rows:
            blocks.append("_No rows._ A close-derived record should exist here.\n")
            continue
        blocks.append("| principal_id | speaker_id | turns | scope | summary (200ch) |")
        blocks.append("|---|---|---|---|---|")
        for principal, speaker, turns, scope, summary in rows:
            clean = str(summary).replace("|", "\\|").replace("\n", " ")
            blocks.append(
                f"| `{principal}` | `{speaker}` | {turns} | `{scope}` | {clean} |"
            )
        blocks.append("")
        speakers = sorted({str(r[1]) for r in rows})
        principals = sorted({str(r[0]) for r in rows})
        blocks.append(
            f"**{len(rows)} record(s)** — {len(speakers)} distinct speaker(s) "
            f"({', '.join(f'`{s}`' for s in speakers)}) across "
            f"{len(principals)} principal(s) "
            f"({', '.join(f'`{p}`' for p in principals)}). The assertion is "
            f"that no record mixes two speakers or two principals, and that "
            f"the speaker count matches the number of distinct speakers this "
            f"persona actually heard. One record spanning several speakers "
            f"means the speaker dimension of the key did not land."
        )
        blocks.append("")
        facts = _agent_query(persona, LEG4_FACTS)
        if facts:
            blocks.append(f"#### `{persona}` — extracted facts\n")
            blocks.append("| principal_id | speaker_id | subject | predicate | object |")
            blocks.append("|---|---|---|---|---|")
            for principal, speaker, subj, pred, obj in facts:
                blocks.append(
                    f"| `{principal}` | `{speaker}` | {subj} | {pred} | {obj} |"
                )
            blocks.append("")
    return "\n".join(blocks)


def collect_cost() -> str:
    """Live spend for the arc — read BEFORE `docker compose down -v`.

    The v0.3.14 arc reported cost as *not measured* and left the next run an
    instruction that does not work. Two things are wrong with it, both
    verified against the code here rather than inherited:

    * It greps ``"op":"settle"``. That line is ``wallet: lease finalized``
      (``internal/wallet/wallet.go``) and carries ``actual_input_tokens`` /
      ``actual_output_tokens`` — **no USD figure at all**.
    * The USD figure is on a *different* line with no ``op`` field:
      ``provisional charge reconciled`` (``internal/cost/cost.go``), carrying
      ``estimatedUSD`` and ``actualUSD``.

    Both are ``Debug``, which the compose stack does emit — it runs the
    orchestrator with ``--env development`` and ``loggerLevel`` returns
    ``DebugLevel`` there. A deployment running ``--env production`` logs
    neither, and cost capture is impossible from logs alone.
    """
    proc = subprocess.run(  # noqa: S603
        ["docker", "compose", "logs", "--no-color", "orchestrator"],  # noqa: S607
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=90, check=False,
    )
    if proc.returncode != 0:
        return "_Could not read orchestrator logs._"
    lines = [ln for ln in proc.stdout.splitlines() if "actualUSD" in ln]
    if not lines:
        return (
            "_No `actualUSD` lines._ Check the orchestrator is running with "
            "`--env development` (production suppresses DEBUG), and that the "
            "logs have not been rotated or the stack torn down."
        )
    total = 0.0
    for line in lines:
        marker = '"actualUSD":'
        idx = line.find(marker)
        if idx == -1:
            continue
        tail = line[idx + len(marker):].lstrip()
        number = ""
        for ch in tail:
            if ch.isdigit() or ch in ".-e":
                number += ch
            else:
                break
        try:
            total += float(number)
        except ValueError:
            continue
    return (
        f"**{len(lines)} reconciled lease(s), ${total:.4f} total.**\n\n"
        f"Read from `provisional charge reconciled` / `actualUSD` "
        f"(`internal/cost/cost.go`) — *not* from `\"op\":\"settle\"`, which "
        f"carries token counts only. Captured before teardown."
    )


LEG9_EPISODES = "SELECT principal_id, speaker_id, COUNT(*) FROM episodes GROUP BY 1, 2"
LEG9_FACTS = "SELECT principal_id, speaker_id, COUNT(*) FROM facts GROUP BY 1, 2"
LEG9_REPLAY = (
    "SELECT interaction_id, principal_id, speaker_id FROM episodes "
    "WHERE interaction_id LIKE 'replay-%'"
)


def collect_leg9(label: str, personas: tuple[str, ...] = PERSONAS) -> str:
    """One snapshot (A, B or C), per (principal_id, speaker_id), both partitions."""
    blocks = [f"#### Snapshot {label}\n"]
    for persona in personas:
        blocks.append(f"**`{persona}`**\n")
        for title, sql in (("episodes", LEG9_EPISODES), ("facts", LEG9_FACTS)):
            rows = _agent_query(persona, sql)
            blocks.append(f"{title}:")
            blocks.append("")
            blocks.append("| principal_id | speaker_id | count |")
            blocks.append("|---|---|---|")
            for principal, speaker, count in rows or []:
                blocks.append(f"| `{principal}` | `{speaker}` | {count} |")
            if not rows:
                blocks.append("| _(empty)_ | | |")
            blocks.append("")
        replay = _agent_query(persona, LEG9_REPLAY)
        blocks.append(f"`replay-` derived rows: **{len(replay)}**")
        for interaction_id, principal, speaker in replay:
            blocks.append(f"- `{interaction_id}` — `{principal}` / `{speaker}`")
        blocks.append("")
    return "\n".join(blocks)
