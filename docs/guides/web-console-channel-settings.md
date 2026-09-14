# Web Console — Channel Settings

The console's **Channel settings** card lets you read and change a group
channel's governance settings from the browser, and convene an autonomous
channel. For what the console is, how to start it, and when it is safe to
expose, see the [web console guide](web-console.md).

---

## Channel settings — edit governance from the browser

A selected **group channel** can have its governance knobs read and edited from
the console — the browser counterpart to the CLI
[`channel config`](channels.md#editing-governance-config-at-runtime--channel-config-rfc-0050-phase-1)
verb group (RFC 0050 Phase 2). Both surfaces ride the **same**
`GET`/`PATCH /api/v1/channels/{id}/config` endpoint and the **same** per-channel
revision, so a value set in one is what the other reads back — one source of
truth, the store. It is a **Channel settings** card in the management rail,
beside the **Members** card, shown only for a watched **group** channel
(not DMs).

**It ships on.** The schema default is `false`, but the delivered
[`config/ui.yaml`](../../config/ui.yaml) sets `config_edit_enabled: true` (RFC
0050) — set it back to `false` under the `channel_timeline` panel to disable it:

```yaml
panels:
  channel_timeline:
    enabled: true
    config_edit_enabled: true   # shipped on (schema default false) — gates BOTH the web panel and CLI uniformly
```

The **same toggle** gates the CLI `channel config` verbs — the whole `/config`
endpoint, read *and* write: on exposes both surfaces; off returns `403` to both.
The panel renders under the usual `enabled && available` rule; `available` is
**runtime-derived** (channel store + router wired, mirroring the endpoint's
`503`) and never authored — an `available:` key in the YAML is a
`make validate` error. Verify with:

```bash
curl -s http://localhost:8080/api/v1/ui/config | jq '.panels.channel_timeline.config_edit'
# want: { "enabled": true, "available": true }
```

**Using it.** Each knob shows its effective value and a provenance badge —
**Overridden on this channel** or **Inherited default**. To change one, untick
**Inherit fleet default** and set the value; to revert, re-tick it. **Save
settings** sends only the knobs you touched (a sparse patch), carrying the loaded
revision as an `If-Match` guard:

- A reverted knob sends an explicit "unset → inherit"; an override left blank is
  skipped, not sent as `0` (a no-op save sends nothing).
- The **escalation chair** picker offers only floor-capable members (an observer
  cannot chair); a chair needs `floor_control` on, else the save `400`s (a
  cross-field conflict the picker cannot prevent).
- `interaction_budget_tokens` is **router-wired and live-enforced** (RFC 0050
  amendment), so an inherited value resolves to a concrete number, not empty.
- Since v0.3.11 the panel renders an **Autonomous channel** section (RFC 0052) —
  the `autonomous` knobs (enable, Topic/Goal, Agenda, Convener, Max rounds) on the
  same PATCH, plus (PR 3) a **Convene** action
  ([autonomous channels guide](autonomous-channels.md)).
- On a concurrent edit, the save returns `409`; the panel **reloads the latest
  config and replays your pending edits on top** rather than blind-overwriting,
  and asks you to review and save again.

> **First-edit behavior (✅ ISSUE-0103 resolved 2026-06-15).** Editing one knob on
> a YAML-seeded channel **preserves** its other knobs (including the YAML chair):
> the first edit seeds its merge base from the channel's resolved governance
> ([ISSUE-0103](../issues/ISSUE-0103-first-config-edit-detaches-yaml-seeded-knobs.md)).
> Expect the channel to become **store-canonical** (previously-inherited knobs now
> read source `channel`), and a lone `floor_control: false` on a chaired channel to
> be **rejected** — clear the chair in the same save. Still a governance write
> that is anonymous under the default `auth.mode: disabled` (`operator`-gated
> under `enabled`) — see
> [Security](web-console.md#security--exposure-beyond-localhost) before exposing the
> console beyond localhost.

For the live cross-surface acceptance walkthrough, see
[MT-CHANNEL-CONFIG-002](../manual-tests/MT-CHANNEL-CONFIG-002.md).

---

## Related documentation

- [Web console guide](web-console.md) — running the console, and its
  [security posture](web-console.md#security--exposure-beyond-localhost).
- [Channels guide § `channel config`](channels.md#editing-governance-config-at-runtime--channel-config-rfc-0050-phase-1)
  — the CLI side of the same endpoint.
- [MT-CHANNEL-CONFIG-002](../manual-tests/MT-CHANNEL-CONFIG-002.md) and
  [MT-CHANNEL-CONFIG-004](../manual-tests/MT-CHANNEL-CONFIG-004.md) — manual
  tests for editing channel config from the console.
