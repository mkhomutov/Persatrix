<script>
  // RFC 0052 (v0.3.11) autonomous-channel config section — a child of
  // ChannelSettings, extracted because that panel is at the file-size cap. Pure
  // presentation plus the convener candidate list: the PARENT owns the
  // draft/patch/save/revision state and the list<->text coercion; this component
  // renders the autonomous rows bound to the parent's reactive `drafts` (by
  // reference), mirroring the parent's flat-knob rows (a provenance badge + an
  // inherit/override control) so the one Save covers every knob.
  //
  // knobs      — the AUTONOMOUS_KNOBS descriptors ({key, label, type}).
  // drafts     — the parent's reactive draft map (key -> {inherit, value}); the
  //              controls bind into it, so edits flow to the parent's patch with
  //              no callback. Adopt populates these before this renders.
  // members    — [{id, respond, …}] for the convener picker (observers excluded).
  // agentsById — id -> agent, for convener display names.
  // channelId  — the group channel id, for the Convene action's POST.
  // config     — the parent's loaded/applied config response; the Convene action
  //              derives the SAVED armed state from it (config.autonomous.enabled),
  //              NOT from `drafts` — convening reads the persisted block the server
  //              holds, so a just-toggled-but-unsaved draft must not offer it.
  // dirty      — whether the parent has unsaved edits; convening reads the
  //              PERSISTED block, so we disable Convene while dirty and tell the
  //              operator to save first rather than convene a stale config.
  import { conveneChannel, ApiError } from "../lib/api.js";
  let {
    knobs,
    drafts,
    members = [],
    agentsById = {},
    channelId = "",
    config = null,
    dirty = false,
  } = $props();

  // The SAVED armed state — the persisted `autonomous.enabled` cell, not the
  // editable draft. The Convene action shows only when the channel is armed per
  // the block the server actually reads.
  const armed = $derived(Boolean(config?.autonomous?.enabled?.value));

  // RFC 0052 §B Convene action state — independent of the parent's save flow
  // (this is an action, not a config edit), so it owns its own pending flag and
  // result messages.
  let convening = $state(false);
  let conveneError = $state(""); // a hard failure (the server's wording)
  let conveneNotice = $state(""); // a success confirmation

  async function convene() {
    if (convening || !armed || dirty) return;
    convening = true;
    conveneError = "";
    conveneNotice = "";
    try {
      const resp = await conveneChannel(channelId);
      const who = resp?.convener || "the convener";
      conveneNotice = `Convening — ${who} is opening the discussion.`;
    } catch (err) {
      conveneError =
        err instanceof ApiError
          ? err.message
          : `Could not convene: ${err.message}`;
    } finally {
      convening = false;
    }
  }

  // Convener candidates: members that can hold the floor. Observers (respond
  // "never") are server-rejected, so omit them — exactly as the parent's chair
  // picker does. If the current override points at someone no longer a member,
  // keep it selectable so it stays visible/changeable rather than silently dropped.
  const convenerCandidates = $derived.by(() => {
    const opts = members
      .filter((m) => m.respond !== "never")
      .map((m) => ({ id: m.id, name: agentsById[m.id]?.name ?? m.id }));
    const cur = drafts["autonomous.convener"]?.value;
    if (cur && !opts.some((o) => o.id === cur)) {
      opts.push({
        id: cur,
        name: `${agentsById[cur]?.name ?? cur} (not a member)`,
      });
    }
    return opts;
  });
</script>

<fieldset class="autonomous-settings">
  <legend>Autonomous channel (RFC 0052)</legend>
  <ul class="knob-list">
    {#each knobs as knob (knob.key)}
      {#if drafts[knob.key]}
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
            {:else if knob.type === "list"}
              <!-- The agenda: one sub-topic per line. The parent coerces this
                   text to/from the `[]string` wire shape (agendaToText/List). -->
              <textarea
                class="value agenda"
                aria-label={knob.label}
                rows="3"
                bind:value={drafts[knob.key].value}
                disabled={drafts[knob.key].inherit}
              ></textarea>
            {:else if knob.type === "convener"}
              <select
                class="value"
                aria-label={knob.label}
                bind:value={drafts[knob.key].value}
                disabled={drafts[knob.key].inherit}
              >
                <option value="" disabled>Select a convener…</option>
                {#each convenerCandidates as cand (cand.id)}
                  <option value={cand.id}>{cand.name}</option>
                {/each}
              </select>
            {:else}
              <!-- type === "text": the topic / goal free-text strings. -->
              <input
                class="value"
                type="text"
                aria-label={knob.label}
                bind:value={drafts[knob.key].value}
                disabled={drafts[knob.key].inherit}
              />
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
      {/if}
    {/each}
  </ul>

  <!-- RFC 0052 §B PR 3: the Convene action — the panel's first per-channel
       action button. Shown only when the channel is armed per the SAVED config;
       disabled while a convene is in flight or the operator has unsaved edits
       (convening reads the persisted block, so a stale draft must be saved
       first). type="button" so it never submits the parent's save form. -->
  {#if armed}
    <div class="convene-action">
      <button
        type="button"
        class="convene"
        onclick={convene}
        disabled={convening || dirty}
        title={dirty ? "Save your changes before convening" : ""}
      >
        {convening ? "Convening…" : "Convene now"}
      </button>
      {#if dirty}
        <span class="convene-hint">Save your changes before convening.</span>
      {/if}
      {#if conveneError}
        <p class="boot error" role="alert">{conveneError}</p>
      {/if}
      {#if conveneNotice}
        <p class="notice" role="status">{conveneNotice}</p>
      {/if}
    </div>
  {/if}
</fieldset>

<style>
  /* Mirror ChannelSettings' row layout (scoped styles don't cross components). */
  .autonomous-settings {
    border: 1px solid var(--border, #d0d0d0);
    border-radius: 6px;
    margin: 0.75rem 0 0;
    padding: 0.5rem 0.75rem 0.75rem;
  }
  .autonomous-settings legend {
    font-weight: 600;
    font-size: 0.85rem;
    padding: 0 0.4rem;
  }
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
  .agenda {
    flex: 1 1 14rem;
    resize: vertical;
    font: inherit;
  }
  .convene-action {
    margin-top: 0.75rem;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
  }
  .convene-hint {
    font-size: 0.8rem;
    opacity: 0.7;
  }
</style>
