<script>
  // Self-contained "New channel" form (RFC 0048 channel-creation amendment §B),
  // extracted from ChannelTimeline.svelte so the panel keeps only the open/close
  // glue and the post-create reload-and-select. Two modes:
  //   - Group  — POST /api/v1/channels creates group:<name> with persona members
  //              (the server derives the id, so the name is sent bare); the acting
  //              user is seeded as a member so they can post (ErrNotMember).
  //   - Direct — a DM is born by chatting (GetOrCreateDM on first message), so
  //              direct mode picks one persona + an opening message and sends it
  //              via the chat façade; the response's channel_id is the dm: channel
  //              the panel then opens. Member ids always come from the agent list
  //              (§C), filtered to personas — only personas hold a conversation.
  // On success either path hands the resulting channel back via onCreated so the
  // panel lands the operator in it.
  //
  // agents/userId — the persona list and the acting principal.
  // onCreated     — called with the created/opened channel ({ id }).
  // onCancel      — collapse the form without creating.
  import { createChannel, sendChat, ApiError } from "../lib/api.js";
  import { isChattable } from "../lib/agents.js";

  let { agents, userId, onCreated, onCancel } = $props();

  // Only persona agents are eligible — a task agent (agents.yaml type:"task")
  // runs workflow steps and never participates in a discussion.
  const personaAgents = $derived(agents.filter(isChattable));

  // mode: "group" (default) | "direct".
  let mode = $state("group");
  let creating = $state(false);
  let error = $state("");

  // Group-mode draft. memberChecked/respondById are keyed by agent id; an unset
  // policy falls back to when_mentioned (the server default), so no seeding.
  let name = $state("");
  let description = $state("");
  let memberChecked = $state({});
  let respondById = $state({});

  // Direct-mode draft: one persona + an opening message (the DM is born by it).
  let directAgentId = $state("");
  let openingMessage = $state("");

  const selectedMembers = $derived(
    personaAgents
      .filter((a) => memberChecked[a.id])
      .map((a) => ({ id: a.id, respond: respondById[a.id] ?? "when_mentioned" })),
  );

  // The members the group create sends: selected personas plus the acting user
  // (respond:"never" — present so they can publish, never dispatched a turn).
  const memberPayload = $derived(
    userId && !selectedMembers.some((m) => m.id === userId)
      ? [...selectedMembers, { id: userId, respond: "never" }]
      : selectedMembers,
  );

  const canSubmitGroup = $derived(
    name.trim().length > 0 && selectedMembers.length > 0 && !creating,
  );
  const canStartDirect = $derived(
    Boolean(directAgentId) && openingMessage.trim().length > 0 && !creating,
  );
  const canSubmit = $derived(
    mode === "direct" ? canStartDirect : canSubmitGroup,
  );

  function fail(err, verb) {
    error =
      err instanceof ApiError
        ? err.message
        : `The ${verb} could not be created: ${err.message}`;
  }

  async function submitGroup() {
    const trimmed = description.trim();
    const channel = await createChannel({
      name: name.trim(),
      description: trimmed || undefined,
      members: memberPayload,
    });
    onCreated?.(channel);
  }

  // Open a DM by sending the opening message through the chat façade
  // (GetOrCreateDM creates the channel + adds both members); the response's
  // channel_id is the dm: channel the panel opens.
  async function submitDirect() {
    const res = await sendChat(directAgentId, {
      message: openingMessage.trim(),
      userId,
    });
    onCreated?.({ id: res?.channel_id ?? "" });
  }

  async function submit(event) {
    event.preventDefault();
    if (!canSubmit || creating) {
      return;
    }
    error = "";
    creating = true;
    try {
      if (mode === "direct") {
        await submitDirect();
      } else {
        await submitGroup();
      }
    } catch (err) {
      // Surface the server envelope verbatim (esp. 409 duplicate group:<name>);
      // the form stays mounted so the operator can adjust and retry.
      fail(err, mode === "direct" ? "conversation" : "channel");
    } finally {
      creating = false;
    }
  }
</script>

<form class="create-channel" aria-label="Create channel" onsubmit={submit}>
  {#if error}
    <p class="boot error" role="alert">{error}</p>
  {/if}

  <fieldset class="channel-type">
    <legend>Channel type</legend>
    <label>
      <input type="radio" name="channel_type" value="group" bind:group={mode} />
      Group channel
    </label>
    <label>
      <input type="radio" name="channel_type" value="direct" bind:group={mode} />
      Direct message
    </label>
  </fieldset>

  {#if mode === "group"}
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
  {:else}
    <label>
      Persona
      <select aria-label="Persona" bind:value={directAgentId}>
        <option value="" disabled>Choose a persona…</option>
        {#each personaAgents as agent (agent.id)}
          <option value={agent.id}>{agent.name ?? agent.id}</option>
        {/each}
      </select>
    </label>
    <label>
      Opening message
      <textarea
        name="opening_message"
        bind:value={openingMessage}
        rows="2"
        placeholder="Say hello to start the conversation…"
      ></textarea>
    </label>
  {/if}

  <div class="create-actions">
    <button type="submit" class="create" disabled={!canSubmit}>
      {#if mode === "direct"}
        {creating ? "Starting…" : "Start conversation"}
      {:else}
        {creating ? "Creating…" : "Create channel"}
      {/if}
    </button>
    <button type="button" class="cancel" onclick={onCancel}>Cancel</button>
  </div>
</form>
