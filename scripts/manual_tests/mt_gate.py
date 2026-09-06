#!/usr/bin/env python3
"""Gate vocabulary and authenticated HTTP for the MT-MEMORY-GROUP-TENANT-001 arc.

Split from ``mt_group_tenant_preflight.py`` when it crossed the 500-line cap.
The seam mirrors the driver's: that module holds the **checks** — one function
per vacuity trap — while this one holds what they are all phrased in, the
:class:`Gate` result type and the credential-aware ``GET`` the authenticated
ones need.

Two things here were learned the expensive way, and both are about a gate that
reports the wrong thing rather than a stack that is broken:

* A gate has **three** states. Reporting "cannot be answered yet" as a failure
  made the whole preflight unsatisfiable on the MT's own clean start — see
  :func:`deferred_gate`.
* The credential file is keyed **by orchestrator URL**. Reading "the first
  entry with a token" sent one server's token to another and only ever worked
  by accident — see :func:`bearer_token`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Gate:
    """One preflight check: what it protects, and how to fix it.

    Three states, not two. ``skipped`` is the one that matters: a gate that
    could not be *answered* is not a gate that failed, and conflating the two
    made the whole preflight unsatisfiable on the MT's own documented clean
    start — see :func:`deferred_gate`.
    """

    name: str
    leg: str
    ok: bool
    detail: str
    remedy: str = ""
    skipped: bool = False

    @property
    def blocking(self) -> bool:
        """A gate that must be cleared before the arc may be spent."""
        return not self.ok and not self.skipped

    def render(self) -> str:
        mark = "SKIP" if self.skipped else ("PASS" if self.ok else "FAIL")
        line = f"  [{mark}] {self.name} (protects {self.leg}) — {self.detail}"
        if not self.ok and self.remedy:
            line += f"\n         remedy: {self.remedy}"
        return line


def credentials_path() -> Path:
    """Where the Rust CLI stores its tokens.

    `PERSATRIX_CREDENTIALS_FILE` wins when it is set and non-blank, exactly as
    `resolve_path` in `cli/src/credentials.rs` does. A preflight that ignored
    the override probed anonymously against a correctly-configured stack and
    reported three gates broken.
    """
    override = os.environ.get("PERSATRIX_CREDENTIALS_FILE", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".persatrix" / "credentials"


#: `http://localhost:8080` and `http://127.0.0.1:8080` are different map keys
#: but the same orchestrator, and the two defaults in play disagree: the Rust
#: CLI's `--server` defaults to the first, this module's to the second. So a
#: token stored by `login` is looked up under both spellings of one origin —
#: and never under a *different* origin, which is what returning "the first
#: entry with a token" used to do.
_HOST_ALIASES = ("localhost", "127.0.0.1", "[::1]")


def _server_keys(server: str) -> list[str]:
    """Every credential-file key that names this same orchestrator."""
    base = server.rstrip("/")
    keys = [base]
    for host in _HOST_ALIASES:
        if f"//{host}:" in base or base.endswith(f"//{host}"):
            keys.extend(
                base.replace(f"//{host}", f"//{other}")
                for other in _HOST_ALIASES if other != host
            )
            break
    return keys


def bearer_token(server: str) -> str:
    """The operator's stored CLI token **for this server**, if they logged in.

    Once `auth.mode: enabled` is live, most of the endpoints these gates read
    (`/api/v1/agents`, a channel's `/config`) answer **401** to an anonymous
    caller — so a preflight that reads them anonymously reports every gate as
    broken the moment auth starts working. The file is a JSON object *keyed by
    orchestrator URL* (`cli/src/credentials.rs`); taking the first entry with a
    token regardless of key sent one orchestrator's token to another, and only
    ever worked by accident. Missing or unreadable is not an error here — the
    probes go out unauthenticated and the gates say so.
    """
    try:
        blob = json.loads(credentials_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(blob, dict):
        return ""
    for key in _server_keys(server):
        entry = blob.get(key)
        if isinstance(entry, dict) and entry.get("token"):
            return str(entry["token"])
    return ""


def get_json(url: str, server: str, timeout: float = 5.0) -> Any:
    """GET *url* as the operator, if a token for *server* is on disk.

    Raises ``urllib.error.HTTPError`` on a 4xx/5xx — callers must distinguish
    a 401 (auth on, no token yet: deferrable) from everything else (broken).
    """
    req = urllib.request.Request(url)  # noqa: S310
    token = bearer_token(server)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def deferred_gate(name: str, leg: str) -> Gate:
    """A gate that cannot be answered YET — not one that failed.

    Three gates read `policyAuthenticated` routes (`internal/server/
    auth_policy.go` registers `GET /api/v1/agents` and a channel's `/config`
    that way). On the MT's own documented clean start — `make reset`, no
    `accounts.db`, Leg 0 bootstraps the very first account — there is no
    operator token in existence, so those three can only 401 while
    ``gate_auth_live`` simultaneously *requires* a 401 to prove enforcement.
    Reported as failures they made the preflight unsatisfiable: the driver
    refused to spend the arc, and the only escape was `--skip-preflight`,
    which drops all nine gates including the four vacuity traps.

    They are not skipped for good. The driver re-runs them through
    :func:`run_deferred_gates` the moment Leg 0's login succeeds — the first
    point at which they are answerable, and still before a cent is spent.
    """
    return Gate(
        name,
        leg,
        ok=False,
        detail="401 — no operator token yet (Leg 0 bootstraps the first account)",
        remedy="none needed: re-checked automatically once Leg 0 logs in. "
               "Running the preflight ALONE against an authenticated stack "
               "requires `persatrix login` first",
        skipped=True,
    )


