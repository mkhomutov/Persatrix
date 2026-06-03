<script>
  // Collapsed "New channel" form (extracted from ChannelTimeline.svelte to keep
  // the panel under the review-size cap; RFC 0048 channel-creation amendment
  // §B). Presentational only — the panel owns the createChannel POST, the
  // reload-and-select, and the error envelope; this child renders the inputs and
  // binds the draft state. Member ids come from the agent list the panel already
  // loads, never free-typed (amendment §C); the server derives the canonical
  // group:<name> id, so the name is sent bare (the preview shows what will be
  // created).
  //
  // agents        — the registered-agent list the member multi-select renders.
  // name/description — bound to the panel's draft fields.
  // memberChecked — id → bool (presence-checked drives the members array).
  // respondById   — id → respond policy (when_mentioned | always | never).
  // creating      — true while the create POST is in flight.
  // canSubmit     — whether Create is enabled (name + ≥1 member, not in flight).
  // error         — the server error envelope to surface (esp. 409 duplicate).
  // onSubmit / onCancel — form submit + collapse handlers.
  let {
    agents,
    name = $bindable(),
    description = $bindable(),
    memberChecked = $bindable(),
    respondById = $bindable(),
    creating,
    canSubmit,
    error,
    onSubmit,
    onCancel,
  } = $props();
</script>

<form class="create-channel" aria-label="Create channel" onsubmit={onSubmit}>
  {#if error}
    <p class="boot error" role="alert">{error}</p>
  {/if}
  <label>
    Channel name
    <input name="channel_name" bind:value={name} autocomplete="off" />
  </label>
  {#if name.trim()}
    <!-- Read-only preview of what will actually be created: the server prepends
         group:, so the client must not (amendment §B). -->
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
    {#if agents.length === 0}
      <p class="empty">No agents are registered to add.</p>
    {:else}
      {#each agents as agent (agent.id)}
        <div class="member">
          <label>
            <input type="checkbox" bind:checked={memberChecked[agent.id]} />
            {agent.name ?? agent.id}
          </label>
          <select
            aria-label={`Respond policy for ${agent.name ?? agent.id}`}
            bind:value={respondById[agent.id]}
          >
            <option value="when_mentioned">When mentioned</option>
            <option value="always">Always</option>
            <option value="never">Never</option>
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
