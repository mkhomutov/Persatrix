<script>
  // Governance-settings surface for a GROUP channel (RFC 0050 Phase 2 PR 2;
  // RFC 0051 PR 5 added the nested reasoning block). Nested in ChannelTimeline
  // beside ChannelMembers and gated identically (a capability + a watched non-DM
  // channel). On channel select it reads GET /api/v1/channels/{id}/config and
  // renders the governance knobs (nine flat + the four nested reasoning.* sub-
  // knobs), each with its effective value, a provenance badge derived from
  // `source` (overridden-here vs inherited fleet default), and an inherit/override
  // control. Save collects ONLY the knobs the operator actually touched into a
  // sparse PATCH and sends the last-read revision in If-Match (optimistic
  // concurrency). A 409 reloads the latest config rather than blind-overwriting;
  // a successful apply adopts the returned (re-revisioned) body and calls
  // onChanged() so the rest of the panel refreshes.
  //
  // Unlike ChannelMembers (which gets its roster from the channel-list row),
  // config is a separate endpoint, so this component fetches its own state.
  //
  // channelId  — the group channel id (group:<name>).
  // members    — [{ id, respond, … }] from the list row; sources the chair picker.
  // agentsById — id → agent, for chair display names.
  // onChanged  — async () => …; called after a successful save to refresh siblings.
  import { getChannelConfig, patchChannelConfig, ApiError } from "../lib/api.js";
  import AutonomousSettings from "./AutonomousSettings.svelte";
  import { KNOBS } from "../lib/channelKnobs.js";
  import {
    AUTONOMOUS_KNOBS,
    agendaToText,
    agendaToList,
  } from "../lib/autonomousKnobs.js";

  let { channelId, members = [], agentsById = {}, onChanged } = $props();

  // The flat + nested-reasoning knob registry (order, labels, control types)
  // lives in lib/channelKnobs.js — carved out with the ISSUE-0114 cascade-depth
  // knob addition (v0.3.13) so this panel stays under the 500-line cap, the
  // autonomousKnobs.js precedent.

  // The flat + reasoning knobs render inline; the RFC 0052 `autonomous` block
  // renders in the AutonomousSettings child (this panel is at the file-size cap).
  // All knobs share ONE draft/patch/save path, so the logic iterates the union.
  const allKnobs = [...KNOBS, ...AUTONOMOUS_KNOBS];

  // Resolve a (possibly dotted) knob key to its {value, source} cell in a config
  // response. A flat key reads `resp[key]`; a dotted key (`reasoning.mode`) reads
  // the nested `resp.reasoning.mode`. Falls back to an inherited-null cell so a
  // knob the server omits still renders honestly rather than crashing.
  function fieldFor(resp, key) {
    const dot = key.indexOf(".");
    const cell =
      dot === -1
        ? resp?.[key]
        : resp?.[key.slice(0, dot)]?.[key.slice(dot + 1)];
    return cell ?? { value: null, source: "default" };
  }

  // Write a (possibly dotted) knob key into a sparse PATCH body, nesting a dotted
  // key under its namespace object (`reasoning.mode` → `{reasoning: {mode: …}}`)
  // so the body matches the server's nested knob shape. A `null` value (a revert
  // to inherit) nests identically, clearing just that sub-knob.
  function setBody(body, key, value) {
    const dot = key.indexOf(".");
    if (dot === -1) {
      body[key] = value;
      return;
    }
    const ns = key.slice(0, dot);
    (body[ns] ??= {})[key.slice(dot + 1)] = value;
  }

  let config = $state(null); // the last loaded/applied response (carries revision)
  let drafts = $state({}); // key -> { inherit, value } (reactive, bound to inputs)
  let original = {}; // non-reactive snapshot of the loaded state, for diffing
  let loading = $state(false);
  let saving = $state(false);
  let error = $state(""); // a hard failure (load/save error wording)
  let warning = $state(""); // a recovered condition (a 409 that reloaded cleanly)
  let notice = $state(""); // a success confirmation
  let loadToken = 0; // invalidates an in-flight load/save when the channel switches

  // Chair candidates: members that can hold the floor (observers — respond
  // "never" — are rejected by the server, so omit them). If the current override
  // points at someone no longer a member, keep it selectable so it is visible
  // and changeable rather than silently dropped.
  const chairCandidates = $derived.by(() => {
    const opts = members
      .filter((m) => m.respond !== "never")
      .map((m) => ({ id: m.id, name: agentsById[m.id]?.name ?? m.id }));
    const cur = drafts.escalation_chair_id?.value;
    if (cur && !opts.some((o) => o.id === cur)) {
      opts.push({ id: cur, name: `${agentsById[cur]?.name ?? cur} (not a member)` });
    }
    return opts;
  });

  // adopt a freshly loaded/applied response as the new baseline: it carries the
  // current revision and resets every draft to match (no edits pending).
  function adopt(resp) {
    config = resp;
    const d = {};
    const o = {};
    for (const k of allKnobs) {
      const field = fieldFor(resp, k.key);
      const inherit = field.source !== "channel";
      // A null/absent value renders empty, never coerced to 0 (so a no-op save
      // emits nothing). A `list` (agenda) is a JSON array on the wire — render it
      // as the newline text its <textarea> binds to; `o` keeps the raw array.
      const v = field.value;
      const draft = k.type === "list" ? agendaToText(v) : v == null ? "" : v;
      d[k.key] = { inherit, value: draft };
      o[k.key] = { inherit, value: v };
    }
    drafts = d;
    original = o;
  }

  function normalize(k, v) {
    if (k.type === "bool") return Boolean(v);
    if (k.type === "int") return v === "" || v == null ? null : Number(v);
    // A list (agenda): compare item-by-item, so a draft (text) and the original
    // (a wire array) normalize through one shape — agendaToList tolerates both.
    if (k.type === "list") return agendaToList(v).join("\n");
    return v == null ? "" : String(v); // chair + enum + text + convener
  }

  function changed(k) {
    const o = original[k.key];
    const c = drafts[k.key];
    if (!o || !c) return false;
    if (o.inherit !== c.inherit) return true; // override<->inherit flip
    if (c.inherit) return false; // both inherit -> nothing to send
    return normalize(k, o.value) !== normalize(k, c.value);
  }

  // The sparse patch: only the touched knobs. A reverted knob sends an explicit
  // null (unset->inherit); an overridden int with an empty box is skipped rather
  // than sent as 0. Derived so the Save button and the request share one source.
  const patch = $derived.by(() => {
    const body = {};
    for (const k of allKnobs) {
      if (!changed(k)) continue;
      if (drafts[k.key].inherit) {
        setBody(body, k.key, null);
        continue;
      }
      const v = drafts[k.key].value;
      if (k.type === "bool") setBody(body, k.key, Boolean(v));
      else if (k.type === "int") {
        if (v === "" || v == null) continue;
        setBody(body, k.key, Number(v));
      } else if (k.type === "list") {
        // The agenda override rides as a JSON array (empty box -> []).
        setBody(body, k.key, agendaToList(v));
      } else {
        // chair/convener selects + enum: a blank pick has nothing concrete to
        // send — skip it rather than emit escalation_chair_id:"" (a 400), as a
        // blank int is skipped above. Free TEXT (topic/goal) is the exception:
        // "" is a valid explicit override (CLI parity, server accepts it).
        const s = String(v ?? "");
        if (s === "" && k.type !== "text") continue;
        setBody(body, k.key, s);
      }
    }
    return body;
  });

  const dirty = $derived(Object.keys(patch).length > 0);

  async function load(id) {
    const token = ++loadToken;
    if (!id) {
      config = null;
      return;
    }
    loading = true;
    error = "";
    warning = "";
    notice = "";
    // Drop the previous channel's config up front so its form never renders over
    // the new channel's load — otherwise an operator could edit/save against the
    // wrong channel's state during the fetch window. The `{#if loading && !config}`
    // branch then shows the loading indicator instead of stale rows.
    config = null;
    try {
      const resp = await getChannelConfig(id);
      if (token !== loadToken) return; // a newer channel won the race
      adopt(resp);
    } catch (err) {
      if (token !== loadToken) return;
      config = null;
      error =
        err instanceof ApiError
          ? err.message
          : `Could not load settings: ${err.message}`;
    } finally {
      if (token === loadToken) loading = false;
    }
  }

  // Re-read after a 409 without clobbering the conflict notice, so the operator
  // sees the latest values AND why their save didn't land. Crucially, do NOT
  // discard their in-flight edits: snapshot the touched knobs first, refresh the
  // baseline (new revision + whatever changed elsewhere), then replay the edits
  // on top. That keeps the "review your edits and save again" notice honest and
  // the retried save dirty against the fresh revision. An edit that now matches
  // the updated server value falls out of `dirty` on its own; a genuine conflict
  // stays visible and saveable.
  // `id`/`token` pin this reload to the channel generation the conflicting save
  // belonged to: if the operator switched channels while the conflict was being
  // resolved, load() has bumped loadToken, so we drop the adopt and the notice
  // rather than stamping this channel's state/warning onto another channel.
  async function reloadAfterConflict(id, token) {
    const pending = {};
    for (const k of allKnobs) {
      if (changed(k)) pending[k.key] = { ...drafts[k.key] };
    }
    try {
      const resp = await getChannelConfig(id);
      if (token !== loadToken) return; // a newer channel won the race
      adopt(resp);
      for (const key of Object.keys(pending)) drafts[key] = pending[key];
    } catch {
      // The reload itself failed; the operator's edits stay put and the conflict
      // notice below is still the right signal — they must not assume success.
    }
    if (token !== loadToken) return; // moved on; don't warn against another channel
    // A recovered condition, not a hard error: the save didn't land but the panel
    // re-synced cleanly. Render it as a warning, not in the red error styling.
    warning =
      "This channel's settings changed elsewhere — reloaded with the latest. Review your edits and save again.";
  }

  async function save(event) {
    event?.preventDefault?.();
    if (!dirty || saving || !config) return;
    // Pin this save to the channel generation it started in. load() bumps
    // loadToken on every channel switch, so if the operator navigates away while
    // the request is in flight we leave the new channel's state untouched rather
    // than rendering this channel's response (or a stale failure) under it.
    const token = loadToken;
    const id = channelId;
    const body = patch;
    const revision = config.revision;
    saving = true;
    error = "";
    warning = "";
    notice = "";
    try {
      const resp = await patchChannelConfig(id, body, revision);
      if (token !== loadToken) return; // a newer channel won the race
      adopt(resp); // picks up the bumped revision for the next save
      notice = "Settings saved.";
      await onChanged?.();
    } catch (err) {
      if (token !== loadToken) return; // stale outcome for a channel we've left
      if (err instanceof ApiError && err.status === 409) {
        await reloadAfterConflict(id, token);
      } else {
        error =
          err instanceof ApiError
            ? err.message
            : `Could not save settings: ${err.message}`;
      }
    } finally {
      saving = false;
    }
  }

  // Reload whenever the watched channel changes.
  $effect(() => {
    const id = channelId;
    load(id);
  });
</script>

<details class="channel-settings">
  <summary>Channel settings</summary>

  {#if error}
    <p class="boot error" role="alert">{error}</p>
  {/if}
  {#if warning}
    <p class="warning" role="alert">{warning}</p>
  {/if}
  {#if notice}
    <p class="notice" role="status">{notice}</p>
  {/if}

  {#if loading && !config}
    <p class="loading" role="status">Loading settings…</p>
  {:else if config}
    <form class="settings-form" aria-label="Channel settings" onsubmit={save}>
      <ul class="knob-list">
        {#each KNOBS as knob (knob.key)}
          <li class="knob-row">
            <div class="knob-head">
              <span class="knob-label">{knob.label}</span>
              <span
                class="provenance"
                class:overridden={!drafts[knob.key].inherit}
              >
                {drafts[knob.key].inherit
                  ? "Inherited default"
                  : "Overridden on this channel"}
              </span>
            </div>

            <div class="knob-control">
              <!-- The label is shown once in .knob-head above; the control
                   carries it as an accessible name (aria-label), not a second
                   visible copy of the text. -->
              {#if knob.type === "bool"}
                <input
                  class="value"
                  type="checkbox"
                  aria-label={knob.label}
                  bind:checked={drafts[knob.key].value}
                  disabled={drafts[knob.key].inherit}
                />
              {:else if knob.type === "int"}
                <input
                  class="value"
                  type="number"
                  aria-label={knob.label}
                  min="0"
                  step="1"
                  bind:value={drafts[knob.key].value}
                  disabled={drafts[knob.key].inherit}
                />
              {:else if knob.type === "enum"}
                <!-- Generic enum select: options are a fixed value set on the knob
                     (reasoning.mode/model/depth). The same <select> primitive as
                     the chair picker below, generalized off a static list rather
                     than the member roster. -->
                <select
                  class="value"
                  aria-label={knob.label}
                  bind:value={drafts[knob.key].value}
                  disabled={drafts[knob.key].inherit}
                >
                  {#each knob.options as opt (opt)}
                    <option value={opt}>{opt}</option>
                  {/each}
                </select>
              {:else}
                <select
                  class="value"
                  aria-label={knob.label}
                  bind:value={drafts[knob.key].value}
                  disabled={drafts[knob.key].inherit}
                >
                  <option value="" disabled>Select a chair…</option>
                  {#each chairCandidates as cand (cand.id)}
                    <option value={cand.id}>{cand.name}</option>
                  {/each}
                </select>
              {/if}

              <label class="inherit">
                <input
                  type="checkbox"
                  bind:checked={drafts[knob.key].inherit}
                  aria-label={`Inherit fleet default for ${knob.label}`}
                />
                Inherit fleet default
              </label>
            </div>
          </li>
        {/each}
      </ul>

      <!-- RFC 0052: own child, shares this save; hosts the Convene action (PR 3, armed off `config`). -->
      <AutonomousSettings knobs={AUTONOMOUS_KNOBS} {drafts} {members} {agentsById} {channelId} {config} {dirty} />

      <button type="submit" class="save" disabled={!dirty || saving}>
        {saving ? "Saving…" : "Save settings"}
      </button>
    </form>
  {/if}
</details>

<style>
  .knob-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  .knob-row {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .knob-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.5rem;
  }
  .knob-label {
    font-weight: 600;
  }
  .provenance {
    font-size: 0.75rem;
    opacity: 0.7;
  }
  .provenance.overridden {
    opacity: 1;
    font-weight: 600;
  }
  .knob-control {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    flex-wrap: wrap;
  }
  .knob-control .inherit {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.8rem;
  }
  .warning {
    color: var(--warn, #b26a00);
  }
  .notice {
    color: var(--ok, green);
  }
  .save {
    margin-top: 0.75rem;
  }
</style>
