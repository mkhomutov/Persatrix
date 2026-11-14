---
id: ISSUE-0255
summary: "Every gRPC connection between the orchestrator and its agents is opened without TLS, in both directions, and no guide tells an operator to keep that link on one host or a private network. The defaults do keep it private; a deployment that puts agents on another host sends prompts, replies and wallet calls in clear text."
status: open
severity: low
area: security
created: 2026-11-14
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1119
  - internal/executor/dispatch.go
  - internal/executor/chat.go
  - internal/executor/interactions.go
  - internal/channels/grpc_dispatcher.go
  - agents/server.py
  - agents/observability/log_shipper.py
  - cmd/orchestrator/main.go
  - docs/guides/auth.md
  - docs/persatrix-extension-spec.md
  - docs/issues/ISSUE-0152-one-quarantine-refuses-every-anonymous-caller.md
  - docs/issues/ISSUE-0227-canonical-specs-describe-unbuilt-features-as-built.md
---

# ISSUE-0255: gRPC between the orchestrator and its agents has no TLS

## Summary

The orchestrator and its agents talk over gRPC in both directions: the
orchestrator sends tasks, chat turns and channel events to each agent, and
each agent calls the orchestrator for its wallet and ships its logs. None
of those connections uses TLS, and no setting turns it on.

The design spec promised mutual TLS (E6.5). The fix for
[ISSUE-0227](ISSUE-0227-canonical-specs-describe-unbuilt-features-as-built.md)
says in the spec that it is not built. Nothing an operator reads before
deploying says so: no guide mentions this link at all.

## Context

Found in the review of #1119. That pull request's description had
"accepted" the gap on the ground that the deployment guides already keep
this traffic on one host or network. No guide says that; the sentence was
wrong and is withdrawn there.

Where the connections are opened, all without TLS:

- The orchestrator dials each agent with `insecure.NewCredentials()`:
  `internal/executor/dispatch.go`, `chat.go` and `interactions.go`, and
  `internal/channels/grpc_dispatcher.go`.
- An agent listens with `add_insecure_port` and dials the orchestrator
  with `grpc.aio.insecure_channel` (`agents/server.py`). Its log shipper
  does the same (`agents/observability/log_shipper.py`).
- The orchestrator's own gRPC server is built with no credentials.

What keeps this private today is the defaults, not a rule:

- The orchestrator's gRPC server and an agent's both bind `127.0.0.1`
  unless told otherwise (`--grpc-bind`, `--host`).
- The shipped compose stack binds them to `0.0.0.0` inside its own
  network and publishes the orchestrator's gRPC port on the host's
  loopback address only. The agents publish no port.

The authentication guide's "HTTPS is required beyond localhost" section
covers the REST side only.

## Impact

Proposed low, because both defaults keep the link on one host or one
private network. For a deployment that puts an agent on another host it
is more serious: prompts, replies, channel messages and wallet calls
cross the network in clear text, and anyone on the path can read or
change them. The link has no authentication either: on gRPC an agent's
ID is whatever the caller sends
([ISSUE-0152](ISSUE-0152-one-quarantine-refuses-every-anonymous-caller.md)).
The severity is proposed here, not ruled.

## Proposed fix / investigation path

1. **Say it where an operator looks.** A short section in the
   authentication guide, beside the HTTPS one: the gRPC link is
   unencrypted and unauthenticated, so keep the orchestrator and its
   agents on one host or a private network, or put the link inside a
   tunnel. This needs no code.
2. **Warn at start-up** when the gRPC server binds a non-loopback
   address, as the orchestrator already does for the REST side under
   `auth.mode: disabled`.
3. **TLS itself** (server certificates first, mutual TLS after) belongs
   with the mesh design of E6.5 and RFC 0009, and is a larger change.

## Notes

> 2026-11-14 — filed from the review of #1119. The extension spec's notes
> at E5.3 and E6.5 link here.
