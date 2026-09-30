<script>
  // Self-contained "New channel" form (RFC 0048 channel-creation amendment §B),
  // extracted from ChannelTimeline.svelte so the panel keeps only the open/close
  // glue and the post-create land-in-it hand-off.
  //
  // GROUP channels only: POST /api/v1/channels creates group:<name> with persona
  // members (the server derives the id, so the name is sent bare); the acting
  // user is seeded as a member so they can post (ErrNotMember). Member ids always
  // come from the agent list (§C), filtered to personas — only personas hold a
  // conversation.
  //
  // Starting a DM is NOT done here — the consolidated Channels panel's persona
  // entry point (the sidebar's persona list) is the single DM affordance (RFC
  // 0048 chat-panel-retirement amendment §B), so a redundant create-form
  // "Direct" mode was dropped. This form is group-channel creation only.
  //
  // agents/userId — the persona list and the acting principal.
  // onCreated     — called with the created channel ({ id }) so the panel lands in it.
  // onCancel      — collapse the form without creating.
  import { SvelteMap } from "svelte/reactivity";
  import { createChannel, ApiError } from "../lib/api.js";
  import { isChattable } from "../lib/agents.js";
  import { matchesQuery, byLabel } from "../lib/filter.js";
  import Avatar from "../ui/Avatar.svelte";
  import Icon from "../ui/Icon.svelte";

  let { agents, userId, onCreated, onCancel } = $props();

  // Only persona agents are eligible — a task agent (agents.yaml type:"task")
  // runs workflow steps and never participates in a discussion.
  const personaAgents = $derived(agents.filter(isChattable));

  let creating = $state(false);
  let error = $state("");

  // memberChecked/respondById are keyed by agent id; an unset policy falls back
  // to when_mentioned (the server default), so no seeding. Maps, not objects: an
  // agent id such as `constructor` would read an object's built-in property.
  let name = $state("");
  let description = $state("");
  const memberChecked = new SvelteMap();
  const respondById = new SvelteMap();

  // A persona list longer than this gets a filter box. Filtering only hides
  // rows: a persona picked and then filtered out of view stays picked.
  const FILTER_FROM = 7;
  let memberQuery = $state("");
  const shownAgents = $derived(
    personaAgents.filter((a) => matchesQuery([a.name, a.id, a.role], memberQuery)),
  );

  const selectedMembers = $derived(
    personaAgents
      .filter((a) => memberChecked.get(a.id))
      .map((a) => ({ id: a.id, respond: respondById.get(a.id) ?? "when_mentioned" })),
  );

  // The members the create sends: selected personas plus the acting user
  // (respond:"never" — present so they can publish, never dispatched a turn).
  const memberPayload = $derived(
    userId && !selectedMembers.some((m) => m.id === userId)
      ? [...selectedMembers, { id: userId, respond: "never" }]
      : selectedMembers,
  );

  const canSubmit = $derived(
    name.trim().length > 0 && selectedMembers.length > 0 && !creating,
  );

  // A modal dialog keeps focus while it is open and gives it back when it
  // closes: remember what had focus on open (the "+" button), restore it on
  // close, open ready to type in the channel name, and wrap Tab / Shift+Tab
  // at the dialog's edges.
  let nameEl = $state(null);
  let dialogEl = $state(null);
  $effect(() => {
    const opener = document.activeElement;
    return () => opener?.focus?.();
  });
  $effect(() => {
    nameEl?.focus();
  });

  function onDialogKeydown(event) {
    if (event.key !== "Tab" || !dialogEl) return;
    const focusables = [
      ...dialogEl.querySelectorAll("button, input, select, textarea, a[href]"),
    ].filter((el) => !el.disabled);
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  // Escape closes the dialog (standard modal behaviour). Not wired to a
  // backdrop click — a stray click must not discard a half-filled form.
  function onWindowKeydown(event) {
    if (event.key === "Escape" && !creating) {
      onCancel?.();
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (!canSubmit || creating) {
      return;
    }
    error = "";
    creating = true;
    try {
      const trimmed = description.trim();
      const channel = await createChannel({
        name: name.trim(),
        description: trimmed || undefined,
        members: memberPayload,
      });
      onCreated?.(channel);
    } catch (err) {
      // Surface the server envelope verbatim (esp. 409 duplicate group:<name>);
      // the form stays mounted so the operator can adjust and retry.
      error =
        err instanceof ApiError
          ? err.message
          : `The channel could not be created: ${err.message}`;
    } finally {
      creating = false;
    }
  }
</script>

<svelte:window onkeydown={onWindowKeydown} />

<!-- Rendered as a modal over the workspace: creating a channel is a deliberate,
     multi-field act, and the overlay keeps the conversation context intact
     underneath instead of pushing it down. -->
<div class="modal-backdrop">
  <div
    class="modal"
    role="dialog"
    aria-modal="true"
    aria-label="New channel"
    tabindex="-1"
    bind:this={dialogEl}
    onkeydown={onDialogKeydown}
  >
    <div class="modal-head">
      <span class="modal-icon" aria-hidden="true"><Icon name="hash" size={18} /></span>
      <div>
        <h2 class="modal-title">New channel</h2>
        <p class="modal-sub">A room where personas talk — and you can post.</p>
      </div>
      <button type="button" class="btn-icon btn-ghost modal-close" aria-label="Close" onclick={onCancel}>
        <Icon name="x" size={16} />
      </button>
    </div>
    <form class="create-channel" aria-label="Create channel" onsubmit={submit}>
      {#if error}
        <p class="boot error" role="alert">{error}</p>
      {/if}

      <label>
        Channel name
        <input name="channel_name" bind:this={nameEl} bind:value={name} autocomplete="off" placeholder="e.g. planning" />
      </label>
      {#if name.trim()}
        <p class="preview">New channel id: <code>group:{name.trim()}</code></p>
      {/if}
      <label>
        Description
        <input
          name="channel_description"
          bind:value={description}
          autocomplete="off"
          placeholder="What is this channel for? (optional)"
        />
      </label>
      <fieldset class="members">
        <legend>Members <span class="legend-count">{selectedMembers.length} selected</span></legend>
        {#if personaAgents.length >= FILTER_FROM}
          <label class="card-search">
            <Icon name="search" size={14} />
            <span class="sr-only">Filter personas</span>
            <input type="search" bind:value={memberQuery} placeholder="Filter personas…" autocomplete="off" />
          </label>
        {/if}
        {#if personaAgents.length === 0}
          <p class="empty">No persona agents are registered to add.</p>
        {:else if shownAgents.length === 0}
          <p class="empty">No personas match.</p>
        {:else}
          <div class="member-picks">
            {#each [...shownAgents].sort(byLabel((a) => a.name ?? a.id)) as agent (agent.id)}
              <div class="member" class:checked={memberChecked.get(agent.id)}>
                <label>
                  <input
                    type="checkbox"
                    bind:checked={() => memberChecked.get(agent.id) ?? false, (v) => memberChecked.set(agent.id, v)}
                  />
                  <Avatar id={agent.id} label={agent.name ?? agent.id} size={24} />
                  <span class="member-pick-name">{agent.name ?? agent.id}</span>
                  {#if agent.role}<span class="member-pick-role" aria-hidden="true">{agent.role}</span>{/if}
                </label>
                <!--
                  The disposition vocabulary covers channels.RespondPolicy
                  (internal/channels/channels.go): the three legacy policies plus the
                  RFC 0030 relevance-amendment set (participant/addressed/observer,
                  v0.3.7) and the v0.3.8 chair facilitator. POST /api/v1/channels
                  accepts every one of them — the server normalizes the disposition to
                  the legacy triple and derives the per-member salience signal from it
                  (channels.ResolveSalienceSignal), so the value is NOT persisted
                  verbatim. Option ORDER below is a UX choice, not the Go declaration
                  order; coverage of the server vocabulary is pinned by the
                  source-parsed lockstep test in ChannelTimeline.create.test.js.
                  `when_mentioned` MUST stay the first option: respondById's entry is unset
                  until the operator picks, and selectedMembers falls back to
                  "when_mentioned", so the first-shown option has to match that
                  fallback or the select would display one value while sending another.
                -->
                <select
                  aria-label={`Respond policy for ${agent.name ?? agent.id}`}
                  bind:value={() => respondById.get(agent.id), (v) => respondById.set(agent.id, v)}
                >
                  <option value="when_mentioned">When mentioned</option>
                  <option value="participant">Participant (salience bid)</option>
                  <option value="chair">Chair (facilitator)</option>
                  <option value="addressed">Addressed only</option>
                  <option value="observer">Observer (never replies)</option>
                  <option value="always">Always</option>
                  <option value="never">Never (post-only)</option>
                </select>
              </div>
            {/each}
          </div>
        {/if}
      </fieldset>
      <p class="modal-note">You join too, as a post-only member, so you can write in it right away.</p>

      <div class="create-actions">
        <button type="submit" class="create" disabled={!canSubmit}>
          {creating ? "Creating…" : "Create channel"}
        </button>
        <button type="button" class="cancel" onclick={onCancel}>Cancel</button>
      </div>
    </form>
  </div>
</div>
