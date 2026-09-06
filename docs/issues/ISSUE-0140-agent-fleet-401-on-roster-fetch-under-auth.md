---
id: ISSUE-0140
summary: "Under auth.mode: enabled the whole persona fleet gets HTTP 401 on the channel roster fetch, because GET /api/v1/agents is policyAuthenticated and agents hold no accounts — the personas degrade to an empty roster, which silently weakens --mention-all resolution and the RFC 0030 Tier B member-count gating"
status: open
severity: medium
area: channels
created: 2026-09-06
refs:
  - docs/manual-tests/v0.3.15-execution-report.md
  - docs/rfcs/0039-user-accounts-authentication.md
  - docs/rfcs/0030-amendment-relevance-gated-response.md
  - docs/rfcs/0009-security-sandboxing.md
---

## Summary

Turn authentication on and the persona fleet can no longer read the channel
roster. Every agent logs one `401` at startup and carries on with an empty
roster, so the two features that ask "who else is in this room?" quietly answer
"nobody".

## Context

Found live during the v0.3.15 release-prep arc
([execution report](../manual-tests/v0.3.15-execution-report.md), finding F-2).
All three agents logged, once at startup:

```
channels: roster fetch http://orchestrator:8080/api/v1/agents returned HTTP 401
```

`GET /api/v1/agents` is `policyAuthenticated`
([`internal/server/auth_policy.go`](../../internal/server/auth_policy.go)) and the
personas are unauthenticated callers **by design** — the fleet holds no accounts,
which is why every agent-authored turn resolves the shared `local` principal. So
this is not a misconfiguration: it is the designed auth posture meeting a route
the agents need.

The arc itself was unaffected, which is the point — nothing failed loudly.

## Impact

The roster is not decorative. It feeds:

- **`--mention-all` resolution** — expanding "everyone" to a member list.
- **RFC 0030 Tier B relevance gating**, via `salience_max_channel_members`, which
  compares the room's member count against a threshold to decide how strictly to
  gate speaking.

With an empty roster both read a room as smaller than it is. The failure mode is
a *behavioural* drift under `auth.mode: enabled` — personas gating differently
than the same deployment gates with auth off — with no error surfaced after the
single startup line. No existing gate covers the interaction between RFC 0039
auth and the channel layer's own service-to-service calls.

## Proposed fix / investigation path

Three shapes, roughly in increasing order of cost:

1. **Reclassify the route.** The roster is already effectively public — channel
   history and the publish response are public by design on the same surface — so
   `policyPublic` for `GET /api/v1/agents` would match the surrounding posture.
   Cheapest, and widens an unauthenticated read surface.
2. **Carve out the fleet at the network layer**, the mitigation the startup WARN
   about the ungated publish surface already names. Deployment-side, not code.
3. **[RFC 0009](../rfcs/0009-security-sandboxing.md) agent authentication** — the
   designed answer, which lets these routes stop being public rather than making
   one more of them so.

Worth deciding alongside [ISSUE-0132](ISSUE-0132-memory-egress-gate-blind-to-room-audience.md),
since audience-scoped egress makes the roster load-bearing for a third consumer.

## Notes

Filed at v0.3.15 release-prep PR 2 from the PR 1 arc's F-2. Not release-blocking
for v0.3.15 — it predates this cycle's changes and touches none of the
attribution axes — and is carried as a Known Gap on the
[v0.3.15 release checklist](../v0.3.15-release-checklist.md#6-known-gaps-to-document-in-release-notes).
