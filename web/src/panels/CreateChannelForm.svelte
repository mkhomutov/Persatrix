<script>
  // Self-contained "New channel" form (RFC 0048 channel-creation amendment §B),
  // extracted from ChannelTimeline.svelte so the panel keeps only the open/close
  // glue and the post-create reload-and-select. It owns the draft state and the
  // createChannel POST + error envelope; on success it hands the created channel
  // back via onCreated so the panel can land the operator in it. Member ids come
  // from the agent list the panel already loads, never free-typed (§C); the
  // server derives the canonical group:<name> id, so the name is sent bare (the
  // preview shows what will actually be created).
  //
  // agents    — the registered-agent list; only personas (not task agents) are
  //             selectable members, since only personas hold a conversation.
  // userId    — the acting principal; auto-added as a member (respond:"never")
  //             so the operator can post into the channel — the store rejects a
  //             publish from a non-member (ErrNotMember).
  // onCreated — called with the created channel on a 201 (panel reloads + selects).
  // onCancel  — collapse the form without creating.
  import { createChannel, ApiError } from "../lib/api.js";
  import { isChattable } from "../lib/agents.js";

  let { agents, userId, onCreated, onCancel } = $props();

  // Only persona agents are eligible members — a task agent (agents.yaml
  // type:"task") runs workflow steps and never participates in a discussion.
  const personaAgents = $derived(agents.filter(isChattable));

  let name = $state("");
  let description = $state("");
  // memberChecked/respondById are keyed by agent id; presence-checked drives the
  // members array, and an unset policy falls back to when_mentioned (the server
  // default in handleCreateChannel, amendment §B/OQ3), so seeding is unnecessary.
  let memberChecked = $state({});
  let respondById = $state({});
  let creating = $state(false);
  let error = $state("");

  const selectedMembers = $derived(
    personaAgents
      .filter((a) => memberChecked[a.id])
      .map((a) => ({ id: a.id, respond: respondById[a.id] ?? "when_mentioned" })),
  );

  // The members the create call sends: the selected personas, plus the acting
  // user (respond:"never" — present so they can publish, never dispatched a turn)
  // unless a selected persona already carries that id.
  const memberPayload = $derived(
    userId && !selectedMembers.some((m) => m.id === userId)
      ? [...selectedMembers, { id: userId, respond: "never" }]
      : selectedMembers,
  );

  // Create needs a name AND at least one persona member — the endpoint rejects an
  // empty members array, and a channel of just the user has no one to talk to.
  const canSubmit = $derived(
    name.trim().length > 0 && selectedMembers.length > 0 && !creating,
  );

  async function submit(event) {
    event.preventDefault();
    if (!canSubmit || creating) {
      return;
    }
    const trimmed = description.trim();
    error = "";
    creating = true;
    try {
      const channel = await createChannel({
        name: name.trim(),
        description: trimmed || undefined,
        members: memberPayload,
      });
      onCreated?.(channel);
    } catch (err) {
      // Surface the server envelope verbatim (esp. 409 duplicate group:<name>);
      // the form stays mounted so the operator can pick a different name.
      error =
        err instanceof ApiError
          ? err.message
          : `The channel could not be created: ${err.message}`;
    } finally {
      creating = false;
    }
  }
</script>

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
