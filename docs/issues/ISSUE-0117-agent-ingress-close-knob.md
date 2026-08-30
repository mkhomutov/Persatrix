---
id: ISSUE-0117
summary: "No way to close the agent-attributable REST ingress under `auth.mode: enabled`: RFC 0039 PR 5 (#793) deliberately carves the persona-fleet seams out as `public` (agent register/deregister, channels list/get, messages GET/POST, convene — agents hold no accounts, RFC 0039 §Non-Goals), so a non-loopback `enabled` bind still exposes anonymous channel reads, participant-attributed message publishes, and agent deregistration; the residual is WARN'd at startup and its real fix is RFC 0009 Phase 4 agent tokens, but a deployment running the console with NO persona fleet gets all of the residual and none of the benefit. A cheap interim knob — e.g. `auth.agent_ingress: open|closed` (default `open`), flipping the seven carve-out routes to `operator` when closed — would let fleet-less deployments close the surface without waiting for the token track. Filed from the #793 review for explicit RFC 0009 slotting."
status: open
severity: low
area: server
created: 2026-07-30
refs:
  - docs/rfcs/0009-security-sandboxing.md
  - docs/rfcs/0039-user-accounts-authentication.md
  - docs/rfcs/0039-pr-plan.md
  - internal/server/auth_policy.go
  - cmd/orchestrator/auth.go
---

## Summary

Under `auth.mode: enabled` the agent-attributable REST ingress stays `public`
by design, and there is no configuration to close it — even on deployments
that run no persona fleet and therefore need none of it.

## Context

RFC 0039 PR 5 (#793) shipped the §E enforcement matrix with a deliberate
carve-out (`internal/server/auth_policy.go`): agent self-registration and
self-deregistration plus the RFC 0011 channel HTTP seams the fleet drives in
production (channels list/get, messages GET/POST, convene) resolve to
`policyPublic`, because agents hold no accounts (RFC 0039 §Non-Goals places
that surface on the RFC 0009 agent-token track — Phase 4, "Agent Identity
Tokens"). Gating those routes would break every deployed persona the moment
`enabled` flips. The residual is WARN'd at startup on a non-loopback
`enabled` bind (`cmd/orchestrator/auth.go` warnAuthPosture) and defended by
the RFC 0009 per-agent limiter + quarantine.

## Impact

On a non-loopback `enabled` bind, anonymous network peers can still: read all
channel messages and history (which interacts with RFC 0037's channel-
classification goals — protected content in channels is anonymously readable
over REST), publish messages attributed to any participant, register agents,
and deregister any agent. For a deployment with a persona fleet this is the
accepted RFC 0039 trade-off; for a human-only deployment (console + CLI, no
fleet) it is pure attack surface with no offsetting benefit.

## Proposed fix / investigation path

A config knob, e.g. `auth.agent_ingress: open|closed` (default `open`, so the
flip to `enabled` stays fleet-safe), that when `closed` re-registers the seven
carve-out routes at `policyOperator`. Against the #793 structure this is
small: parameterize `newPolicyMux` on the knob (the policy mux becomes
server-scoped rather than the current package-level var), extend
`config/security.yaml` + `schemas/security.schema.json`, and downgrade/skip
the startup residual WARN when closed. Belongs on the RFC 0009 track (interim
hardening until Phase 4 agent tokens land); could also slot as a small
follow-up amendment to RFC 0039 if 0009 Phase 4 stays parked.

## Notes

> 2026-07-30 — captured from the PR #793 review (the "load-bearing judgment
> call" focus item): the carve-out itself was judged correct as scoped; this
> knob is the recorded escape hatch for fleet-less deployments.
