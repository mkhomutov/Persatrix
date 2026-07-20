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
  // entry point (PersonaPicker) is the single DM affordance (RFC 0048
  // chat-panel-retirement amendment §B), so a redundant create-form "Direct"
  // mode was dropped. This form is group-channel creation only.
  //
  // agents/userId — the persona list and the acting principal.
  // onCreated     — called with the created channel ({ id }) so the panel lands in it.
  // onCancel      — collapse the form without creating.
  import { createChannel, ApiError } from "../lib/api.js";
  import { isChattable } from "../lib/agents.js";

  let { agents, userId, onCreated, onCancel } = $props();

  // Only persona agents are eligible — a task agent (agents.yaml type:"task")
  // runs workflow steps and never participates in a discussion.
  const personaAgents = $derived(agents.filter(isChattable));

  let creating = $state(false);
  let error = $state("");

  // memberChecked/respondById are keyed by agent id; an unset policy falls back
  // to when_mentioned (the server default), so no seeding.
  let name = $state("");
  let description = $state("");
  let memberChecked = $state({});
  let respondById = $state({});

  const selectedMembers = $derived(
    personaAgents
      .filter((a) => memberChecked[a.id])
      .map((a) => ({ id: a.id, respond: respondById[a.id] ?? "when_mentioned" })),
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
  <div class="modal" role="dialog" aria-modal="true" aria-label="New channel">
    <h2 class="modal-title">New channel</h2>
    <form class="create-channel" aria-label="Create channel" onsubmit={submit}>
  {#if error}
    <p class="boot error" role="alert">{error}</p>
  {/if}

  <label>
    Channel name
    <input name="channel_name" bind:value={name} autocomplete="off" />
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
    />
  </label>
  <fieldset class="members">
    <legend>Members</legend>
    {#if personaAgents.length === 0}
      <p class="empty">No persona agents are registered to add.</p>
    {:else}
      {#each personaAgents as agent (agent.id)}
        <div class="member">
          <label>
            <input type="checkbox" bind:checked={memberChecked[agent.id]} />
            {agent.name ?? agent.id}
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
            `when_mentioned` MUST stay the first option: respondById[id] is unset
            until the operator picks, and selectedMembers falls back to
            "when_mentioned", so the first-shown option has to match that
            fallback or the select would display one value while sending another.
          -->
          <select
            aria-label={`Respond policy for ${agent.name ?? agent.id}`}
            bind:value={respondById[agent.id]}
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
    {/if}
  </fieldset>

  <div class="create-actions">
    <button type="submit" class="create" disabled={!canSubmit}>
      {creating ? "Creating…" : "Create channel"}
    </button>
    <button type="button" class="cancel" onclick={onCancel}>Cancel</button>
  </div>
    </form>
  </div>
</div>
