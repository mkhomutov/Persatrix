<script>
  // RFC 0052 (v0.3.11) autonomous-channel config section — a child of
  // ChannelSettings, extracted because that panel is at the file-size cap. Pure
  // presentation plus the convener candidate list: the PARENT owns the
  // draft/patch/save/revision state and the list<->text coercion; this component
  // renders the autonomous rows bound to the parent's reactive `drafts` (by
  // reference) through the same KnobRow as the parent's flat knobs (a
  // provenance badge + an inherit/override control), so the one Save covers
  // every knob.
  //
  // knobs      — the AUTONOMOUS_KNOBS descriptors ({key, label, type, hint}).
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
  import KnobRow from "./KnobRow.svelte";
  import Icon from "../ui/Icon.svelte";
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

  // RFC 0052 §E convening readout — the LIVE count of openers this channel has
  // dispatched (this process lifetime) and, for a standing channel with a
  // max_convenings bound, how much of that aggregate allowance remains. Sourced
  // from the persisted `autonomous_runtime` block (runtime counters, no
  // provenance), so it tracks the SAVED channel, not the draft. A null
  // `convenings_remaining` is the server's unbounded signal (no positive
  // max_convenings). Composed into ONE string so it renders as a single text
  // node, shown only when armed.
  const conveningReadout = $derived.by(() => {
    const count = config?.autonomous_runtime?.convening_count ?? 0;
    const remaining = config?.autonomous_runtime?.convenings_remaining;
    return remaining != null
      ? `${count} used, ${remaining} remaining`
      : `${count} used (no aggregate bound)`;
  });

  // RFC 0052 §B Convene action state — independent of the parent's save flow
  // (this is an action, not a config edit), so it owns its own pending flag and
  // result messages.
  let convening = $state(false);
  let conveneError = $state(""); // a hard failure (the server's wording)
  let conveneNotice = $state(""); // a success confirmation
  // Latches true after a successful convene so the button cannot fire a second
  // opener. The §E count bound (`max_convenings`) is now enforced server-side,
  // but it does not close the idle race: in the window before the convener's
  // first reply commits an interaction, a second POST is NOT caught by the
  // already-convening 409 — it dispatches (and burns a second convening slot) a
  // second opener that folds into the same discussion (no force-fresh convene
  // was built to stop it). So we latch after one convene; reload (or switch
  // channels) to convene again.
  let convened = $state(false);

  // Reset the action state when the operator switches to a different channel — a
  // fresh channel has its own armed/convened status. Tracks `channelId` only, so
  // it never clobbers a notice the convene() handler just set on this channel.
  $effect(() => {
    channelId;
    convened = false;
    conveneError = "";
    conveneNotice = "";
  });

  async function convene() {
    if (convening || !armed || dirty || convened) return;
    convening = true;
    conveneError = "";
    conveneNotice = "";
    try {
      const resp = await conveneChannel(channelId);
      const who = resp?.convener || "the convener";
      conveneNotice = `Convening — ${who} is opening the discussion.`;
      convened = true; // one opener per panel session; see `convened` above
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

<fieldset class="autonomous-settings settings-section">
  <legend class="settings-section-title"><Icon name="zap" size={13} />Autonomous channel</legend>
  <ul class="knob-list">
    {#each knobs as knob (knob.key)}
      {#if drafts[knob.key]}
        <KnobRow {knob} draft={drafts[knob.key]} candidates={convenerCandidates} />
      {/if}
    {/each}
  </ul>

  <!-- RFC 0052 §E PR 7b: the convening-count / aggregate-bound readout — the
       live count of openers dispatched (this process lifetime) and, for a
       standing channel, how much of the max_convenings allowance remains. Shown
       only when armed (a non-autonomous channel has no convening story). -->
  {#if armed}
    <p class="convening-readout">
      <span class="readout-label">Convenings:</span>
      {conveningReadout}
    </p>
  {/if}

  <!-- RFC 0052 §B PR 3: the Convene action — the panel's first per-channel
       action button. Shown only when the channel is armed per the SAVED config;
       disabled while a convene is in flight or the operator has unsaved edits
       (convening reads the persisted block, so a stale draft must be saved
       first). type="button" so it never submits the parent's save form. -->
  {#if armed}
    <div class="convene-action">
      <button
        type="button"
        class="convene btn-primary btn-sm"
        onclick={convene}
        disabled={convening || dirty || convened}
        title={dirty
          ? "Save your changes before convening"
          : convened
            ? "Convened — reload to convene again"
            : ""}
      >
        <Icon name="play" size={13} />{convening ? "Convening…" : convened ? "Convened" : "Convene now"}
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
