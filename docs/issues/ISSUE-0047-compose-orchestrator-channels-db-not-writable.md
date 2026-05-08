---
id: ISSUE-0047
summary: "docker-compose.yaml orchestrator omits --channels-db override; the binary's relative default 'data/channels.db' is read-only in the container, channels stay disabled, and POST /chat returns 500"
status: resolved
severity: high
area: deployment/docker
created: 2026-05-08
closed: 2026-05-08
refs:
  - docker-compose.yaml
  - cmd/orchestrator/channels.go
  - internal/server/chat_handler.go
  - docs/rfcs/0011-amendment-chat-as-dm.md
  - docs/issues/ISSUE-0046-pyproject-missing-temporal-package.md
---

## Summary

The orchestrator service in [docker-compose.yaml](../../docker-compose.yaml)
inherits the binary's default for `--channels-db`, which is the
relative path `data/channels.db` (see
[cmd/orchestrator/channels.go:22](../../cmd/orchestrator/channels.go#L22)).
Inside the container, WORKDIR is `/app` and the non-root `appuser`
cannot create `/app/data` — every mount under `/app` is either
read-only (`./config`, `./workflows`) or absent. The only writable
volume on the orchestrator service is `orchestrator-data:/var/lib/persatrix`.

`initChannels` therefore fails at startup with:

```
WARN  channels: cannot create db directory; channel endpoints will return 503
      path=data/channels.db error="mkdir data: permission denied"
```

The orchestrator stays up because the failure is soft, but the
channels subsystem is disabled. Under chat-as-DM (RFC 0011 PR
4a-ii-β-2), `POST /api/v1/agents/{id}/chat` is now routed through
`channels.ChannelRouter.PublishAndAwait`, so when `s.channelStore`
is nil the handler logs `"chat: channels subsystem not configured"`
and returns:

```json
{"error":"chat not available","code":"INTERNAL"}   HTTP 500
```

## Context

Captured during a fresh `docker compose build && up` rehearsal of
MT-CHAT-001 against `agent-ember-owl` (2026-05-08), in tandem with
ISSUE-0046 (the missing `persatrix_agents.temporal` package). With
ISSUE-0046 unfixed, every agent crash-loops and the orchestrator
never sees a chat request — masking this issue. Once ISSUE-0046
is fixed, the agents register healthy and the chat 500 surfaces
on the very first `curl POST /chat`.

This regressed when chat-as-DM landed in v0.3.0: pre-amendment chat
went through the gRPC `ChatExecutor` and did not require channels.
The compose file was not updated to point `--channels-db` at the
already-mounted writable volume.

## Impact

- **Compose-deployed orchestrator cannot serve chat at all.**
  MT-CHAT-001, MT-CHAT-002 (REPL), MT-CHAT-003 (session continuity),
  and MT-CHAT-004 (relationship memory) all depend on `POST /chat`
  succeeding. None can execute against the compose stack.
- **All seven channel REST endpoints return 503.** Any future
  manual test or smoke check that touches `/api/v1/channels/...`
  fails before authentication.
- **`make docker-up` looks healthy.** All containers report
  `(healthy)` because the channels failure is logged as `WARN` and
  the HTTP listener still binds. The break only surfaces on the
  first chat request.

## Fix

Append `--channels-db /var/lib/persatrix/channels.db` to the
orchestrator's `command` array in `docker-compose.yaml`. The
`orchestrator-data` named volume already mounts `/var/lib/persatrix`
as writable for `appuser` (mirrors the established pattern for
`OBSERVABILITY_AUDIT_PATH` and `PERSATRIX_LOGBUFFER_DIR`).

```yaml
command: ["--config", "/app/config/", ..., "--channels-db", "/var/lib/persatrix/channels.db"]
```

A regression guard lives at
[tests/unit/python/test_docker_compose_paths.py](../../tests/unit/python/test_docker_compose_paths.py),
asserting that any `--channels-db` value in the compose command
points under a writable mount and is backed by a declared volume.

## Notes

> 2026-05-08 — captured alongside ISSUE-0046 during MT-CHAT-001
> rehearsal. Both bugs together rendered the compose deployment
> non-functional for chat-with-persona since v0.3.0. Closing in
> the same PR; the two fixes are independent root causes but a
> single user-facing reproduction (`docker compose up && curl /chat`).

## Follow-ups (not in this PR)

- The binary's default `data/channels.db` works for `make run` (the
  developer's repo cwd is writable) but is a footgun for any
  containerized deployment. A separate PR could either (a) change
  the default to an absolute path under `XDG_DATA_HOME`/`/var/lib/`,
  or (b) make the default explicitly opt-in (require the flag) so
  the failure mode is loud at startup, not a silent 503.
- A docker smoke job in CI that runs `docker compose up -d`, polls
  `GET /api/v1/agents` for healthy registrations, and `POST /chat`
  against `ember-owl` would catch any future docker-only regression
  of either ISSUE-0046 or ISSUE-0047 class.
