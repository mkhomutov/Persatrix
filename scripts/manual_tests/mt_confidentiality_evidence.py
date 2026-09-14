#!/usr/bin/env python3
"""Evidence readers for MT-PERSONA-CONFIDENTIALITY-001 (the v0.3.16 gate).

Three evidence obligations, each because a green leg can pass for the wrong
reason ([the release-prep plan](../../docs/v0.3.16-release-prep-plan.md)):

* **The per-entry ``audience egress`` record.** Under ``auth.mode: enabled``
  the tenant partition already withholds Alice's DM content from any turn she
  did not cause, so a quiet reply proves nothing. The artifact is her entry
  at *withhold-disjoint* with ``withheld`` 1 — read off the persona's own log,
  where the per-entry ``candidates`` array rides only at DEBUG.
* **A fetch-failed count, not silence.** *withhold-unknown-fetch-failed*
  cannot occur offline and ``live`` admits it, so a run that never resolved a
  roster looks like every entry admitted-as-unknown. The count is reported
  from both runs, zero included.
* **The fact must have consolidated with DM provenance.** The check reads
  ``source_channel_id``; a NULL there measures *no-provenance*, not audience.
  The ``facts`` row is quoted before 5c is sent.

The store queries and the cost capture are the GROUP-TENANT collectors,
reused rather than re-derived: ``_agent_query`` already knows the agent image
ships no ``sqlite3`` CLI and that a failed read must never render as an
empty table, and ``collect_cost`` already knows the wallet's ``settle`` line
carries no USD. Read-only. Nothing here publishes, restarts or reconfigures.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.manual_tests.mt_group_tenant_evidence import (  # noqa: F401
    QueryResult,
    _agent_query,
    _cell,
    collect_cost,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PERSONA = "ember-owl"
AGENT_SERVICE = f"agent-{PERSONA}"
METRICS_URL = "http://127.0.0.1:8889/metrics"

#: The verdict vocabulary (`agents/persona_runtime/audience.py`), spelled out
#: so a renamed verdict shows up as a missing key, not a silent zero.
VERDICTS = (
    "admit",
    "withhold-disjoint",
    "withhold-unknown-fetch-failed",
    "withhold-unknown-no-provenance",
)
FETCH_FAILED = "withhold-unknown-fetch-failed"

#: `logger.info("... audience egress ...", extra={"audience_shadow": trace})`
#: in `agents/persona_runtime/audience_shadow.py`; the extra is surfaced into
#: the JSON line under that key.
_TRACE_KEY = "audience_shadow"
_EVENT_NEEDLE = "audience egress"

_WORD_RE = re.compile(r"[a-z0-9]+")


# ── the audience egress record ──────────────────────────────────────────────

@dataclass
class AudienceRecord:
    """One turn's ``audience egress`` line, as the report quotes it."""

    acting: str
    mode: str
    judged: int
    verdicts: dict[str, int]
    withheld: int
    fetches: int
    candidates: list[dict[str, Any]] = field(default_factory=list)
    event: str = ""
    timestamp: str = ""

    @property
    def fetch_failed(self) -> int:
        return int(self.verdicts.get(FETCH_FAILED, 0))


def json_lines(text: str) -> list[dict[str, Any]]:
    """Every JSON object on its own line, with compose's ``svc | `` prefix cut.

    A non-JSON line (a startup banner, a traceback) is skipped, never fatal:
    the log is read once per leg and one bad line must not discard the rest.
    """
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            obj = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def audience_records(text: str) -> list[AudienceRecord]:
    """The ``audience egress`` records in *text*, oldest first."""
    records: list[AudienceRecord] = []
    for obj in json_lines(text):
        # The container log renders a stdlib record's text under ``message``
        # (``event`` is the structlog-native spelling); read both.
        text = str(obj.get("message") or obj.get("event") or "")
        trace = obj.get(_TRACE_KEY)
        if not isinstance(trace, dict) or _EVENT_NEEDLE not in text:
            continue
        raw_verdicts = trace.get("verdicts") or {}
        verdicts = {v: int(raw_verdicts.get(v, 0)) for v in VERDICTS}
        records.append(AudienceRecord(
            acting=str(trace.get("acting", "")),
            mode=str(trace.get("mode", "")),
            judged=int(trace.get("judged", 0)),
            verdicts=verdicts,
            withheld=int(trace.get("withheld", 0)),
            fetches=int(trace.get("fetches", 0)),
            candidates=[c for c in (trace.get("candidates") or []) if isinstance(c, dict)],
            event=text,
            timestamp=str(obj.get("timestamp", "")),
        ))
    return records


def fetch_failed_total(records: list[AudienceRecord]) -> int:
    """Obligation 2 — summed over every record, so zero is a measured zero."""
    return sum(r.fetch_failed for r in records)


def dm_candidates(record: AudienceRecord) -> list[dict[str, Any]]:
    """Obligation 3 — the entries whose provenance is a DM.

    A candidate sourced from the acting room is admitted by construction and
    says nothing about audience; the leg turns on the ``dm:`` one.
    """
    return [c for c in record.candidates
            if str(c.get("source_channel_id") or "").startswith("dm:")]


def render_audience_records(records: list[AudienceRecord]) -> str:
    """The records as the report quotes them — verdict counts, then entries."""
    if not records:
        return (
            "_No `audience egress` record in the persona log for this turn._ "
            "Either the turn never reached the gate (read the admitted set "
            "first — scope lock 6's vacuity rule), or the log was read too "
            "early. **Not a finding either way.**"
        )
    lines = [
        "| # | acting | mode | judged | admit | disjoint | fetch-failed | "
        "no-provenance | withheld | fetches |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(records, 1):
        v = r.verdicts
        lines.append(
            f"| {i} | `{_cell(r.acting)}` | `{r.mode}` | {r.judged} | "
            f"{v['admit']} | {v['withhold-disjoint']} | {v[FETCH_FAILED]} | "
            f"{v['withhold-unknown-no-provenance']} | {r.withheld} | {r.fetches} |"
        )
    lines.append("")
    lines.append(
        f"**fetch-failed count: {fetch_failed_total(records)}** over "
        f"{len(records)} record(s) (stated even when zero — obligation 2)."
    )
    for i, r in enumerate(records, 1):
        lines.append("")
        lines.append(f"Record {i} — `{_cell(r.event)}`")
        if not r.candidates:
            lines.append("")
            lines.append("_No per-entry `candidates` array — the persona was not "
                         "at DEBUG when this turn ran._")
            continue
        lines.append("")
        lines.append("| tier | entry_id | protection_level | source_channel_id | verdict |")
        lines.append("|---|---|---|---|---|")
        for c in r.candidates:
            lines.append(
                f"| {_cell(c.get('tier', ''))} | `{_cell(c.get('entry_id', ''))}` | "
                f"{_cell(c.get('protection_level', ''))} | "
                f"`{_cell(c.get('source_channel_id') or 'NULL')}` | "
                f"**{_cell(c.get('verdict', ''))}** |"
            )
    return "\n".join(lines)


# ── the leak scan and the tripwire span ─────────────────────────────────────

def leak_scan(text: str, words: tuple[str, ...]) -> list[str]:
    """Which of *words* the reply carries — the Leg 2 / Leg 5 fail criterion.

    Case-insensitive; the driver records the list and the operator judges
    (a persona may say "board" innocently, which is why the scan reports
    rather than grades).
    """
    low = text.lower()
    return [w for w in words if w.lower() in low]


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def verbatim_run(stored: str, reply: str) -> int:
    """Longest run of consecutive normalized words *reply* shares with *stored*.

    §G fires at 8+ (`agents/confidentiality_tripwire.py` hashes the stored
    bytes' normalized words); the driver uses this to say, before reading the
    audit log, whether the model echoed enough for the tripwire to have
    anything to fire on — Edge Case 1's paraphrase reads as a short run.
    """
    a, b = _words(stored), _words(reply)
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def metric_total(scrape: str, name: str) -> float | None:
    """Sum one counter over every label set in a Prometheus text scrape.

    OTEL exports a counter with a ``_total`` suffix unless the name already
    ends in ``total`` (``channel.confidentiality.tripwire_hits`` →
    ``channel_confidentiality_tripwire_hits_total``); both spellings are
    accepted. ``None`` means the series is absent — which for a counter that
    never fired is the ordinary case, and is not the same as ``0.0``.
    """
    base = name.replace(".", "_")
    if base.endswith("_total"):
        base = base[: -len("_total")]
    pattern = re.compile(rf"^{re.escape(base)}(?:_total)?(?:\{{[^}}]*\}})?\s+(-?[0-9.eE+-]+)\s*$")
    total, seen = 0.0, False
    for line in scrape.splitlines():
        m = pattern.match(line.strip())
        if m:
            total += float(m.group(1))
            seen = True
    return total if seen else None


# ── live readers ────────────────────────────────────────────────────────────

def agent_log(since: str, service: str = AGENT_SERVICE, timeout: int = 90) -> str:
    """The persona container's log since *since* (RFC 3339, UTC)."""
    try:
        proc = subprocess.run(  # noqa: S603
            ["docker", "compose", "logs", "--no-color", "--since", since, service],  # noqa: S607
            cwd=REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"[log read failed: {type(exc).__name__}: {exc}]"
    return proc.stdout if proc.returncode == 0 else f"[log read failed: exit {proc.returncode}]"


def grep_lines(text: str, needle: str) -> list[str]:
    return [ln for ln in text.splitlines() if needle in ln]


def scrape_metrics(url: str = METRICS_URL) -> str:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return f"[metrics scrape failed: {exc}]"


FACTS_LIKE = (
    "SELECT subject, predicate, object, protection_level, source_channel_id, "
    "principal_id FROM facts WHERE lower(subject) LIKE '%{needle}%' "
    "OR lower(object) LIKE '%{needle}%' ORDER BY asserted_at"
)
RESTRICTED_EPISODES = (
    "SELECT id, protection_level, source_channel_id, summary FROM episodes "
    "WHERE protection_level = 'restricted' ORDER BY created_at"
)
PROJECTIONS = "SELECT entry_tier, level, text FROM memory_projections"


def facts_like(needle: str, persona: str = PERSONA) -> QueryResult:
    """The `facts` rows naming *needle* — Leg 1's `zephyr`, Leg 5b's `helix`."""
    return _agent_query(persona, FACTS_LIKE.format(needle=needle.lower()))


def restricted_episodes(persona: str = PERSONA) -> QueryResult:
    return _agent_query(persona, RESTRICTED_EPISODES)


def projections(persona: str = PERSONA) -> QueryResult:
    return _agent_query(persona, PROJECTIONS)


def render_rows(headers: tuple[str, ...], result: QueryResult) -> str:
    """A query as a markdown table — or, loudly, as a query that did not run."""
    if result.failed:
        return (f"> ⚠️ **QUERY FAILED — this is not a finding.** "
                f"`{_cell(result.error)}`\n>\n> Nothing was measured here.")
    if not result.rows:
        return "_No rows._"
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in result.rows:
        lines.append("| " + " | ".join(_cell(v if v is not None else "NULL") for v in row) + " |")
    return "\n".join(lines)
