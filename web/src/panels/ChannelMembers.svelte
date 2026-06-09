<script>
  // Member-management surface for a GROUP channel (RFC 0011 §C add/remove member
  // endpoints). Lists the channel's current members with their disposition and
  // the v0.3.8 salience signal, and lets an operator add a persona (with any
  // disposition) or remove an existing member.
  //
  // The members come from the channel-list row (listChannels already returns
  // members per channel as of the per-row members change), so this component
  // does not fetch — after a successful add/remove it calls onChanged() to let
  // the panel re-list, which re-derives the members it passes back in. That keeps
  // a single source of truth (the server's normalized read-back) rather than this
  // component maintaining its own optimistic copy.
  //
  // channelId  — the group channel id (group:<name>).
  // members    — [{ id, respond, joined_at, salience_gated, threshold }] from the list row.
  // agents     — full agent list (the add picker offers personas not already members).
  // agentsById — id → agent, for display names.
  // userId     — the acting principal (flagged "(you)"; a member but NOT removable
  //              here — see the per-row guard below).
  // onChanged  — async () => …; called after a successful mutation to re-list.
  import { addChannelMember, removeChannelMember, ApiError } from "../lib/api.js";
  import { isChattable } from "../lib/agents.js";

  let { channelId, members = [], agents = [], agentsById = {}, userId, onChanged } =
    $props();

  let addId = $state("");
  let addRespond = $state("when_mentioned");
  let busy = $state(false); // an add or remove is in flight
  let busyMember = $state(""); // the member id being removed (per-row disable)
  let error = $state("");

  // Personas not already in the channel are the add candidates. Task agents run
  // workflow steps and never converse, so they are excluded (same rule the
  // create form and persona picker use).
  const memberIds = $derived(new Set(members.map((m) => m.id)));
  const candidates = $derived(
    agents.filter((a) => isChattable(a) && !memberIds.has(a.id)),
  );

  const canAdd = $derived(Boolean(addId) && !busy);

  function displayName(id) {
    const name = agentsById[id]?.name ?? id;
    return id === userId ? `${name} (you)` : name;
  }

  // The persisted `respond` reads back as the legacy triple (the store
  // normalizes chair/participant → always, observer → never), so the salience
  // signal is what confirms an open-floor disposition actually took effect.
  function salienceNote(member) {
    if (!member.salience_gated) {
      return "";
    }
    return member.threshold != null
      ? `salience-gated · threshold ${member.threshold}`
      : "salience-gated";
  }

  async function add(event) {
    event.preventDefault();
    if (!canAdd) {
      return;
    }
    error = "";
    busy = true;
    try {
      await addChannelMember(channelId, { id: addId, respond: addRespond });
      addId = "";
      addRespond = "when_mentioned";
      await onChanged?.();
    } catch (err) {
      error =
        err instanceof ApiError
          ? err.message
          : `Could not add the member: ${err.message}`;
    } finally {
      busy = false;
    }
  }

  async function remove(id) {
    if (busy) {
      return;
    }
    error = "";
    busy = true;
    busyMember = id;
    try {
      await removeChannelMember(channelId, id);
      await onChanged?.();
    } catch (err) {
      error =
        err instanceof ApiError
          ? err.message
          : `Could not remove the member: ${err.message}`;
    } finally {
      busy = false;
      busyMember = "";
    }
  }
</script>

<details class="channel-members">
  <summary>Members ({members.length})</summary>

  {#if error}
    <p class="boot error" role="alert">{error}</p>
  {/if}

  {#if members.length === 0}
    <p class="empty">This channel has no members yet.</p>
  {:else}
    <ul class="member-list">
      {#each members as member (member.id)}
        <li class="member-row">
          <span class="member-name">{displayName(member.id)}</span>
          <span class="member-respond">{member.respond}</span>
          {#if salienceNote(member)}
            <span class="member-salience">{salienceNote(member)}</span>
          {/if}
          {#if member.id !== userId}
            <!-- The acting user is deliberately not removable here: they are the
                 /ui/context principal, not a registered agent, so the add picker
                 (sourced from listAgents) could never re-add them, and a
                 non-member sender is rejected on publish (ErrNotMember → 403).
                 Withholding the button avoids a one-click, web-unrecoverable
                 self-lockout. -->
            <button
              type="button"
              class="remove"
              disabled={busy}
              aria-label={`Remove ${member.id}`}
              onclick={() => remove(member.id)}
            >
              {busyMember === member.id ? "Removing…" : "Remove"}
            </button>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}

  <form class="add-member" aria-label="Add member" onsubmit={add}>
    {#if candidates.length === 0}
      <p class="empty">No personas available to add.</p>
    {:else}
      <label>
        Add persona
        <select bind:value={addId}>
          <option value="" disabled>Select a persona…</option>
          {#each candidates as agent (agent.id)}
            <option value={agent.id}>{agent.name ?? agent.id}</option>
          {/each}
        </select>
      </label>
      <label>
        Disposition
        <select bind:value={addRespond} aria-label="Disposition for the new member">
          <option value="when_mentioned">When mentioned</option>
          <option value="participant">Participant (salience bid)</option>
          <option value="chair">Chair (facilitator)</option>
          <option value="addressed">Addressed only</option>
          <option value="observer">Observer (never replies)</option>
          <option value="always">Always</option>
          <option value="never">Never (post-only)</option>
        </select>
      </label>
      <button type="submit" class="add" disabled={!canAdd}>
        {busy && !busyMember ? "Adding…" : "Add member"}
      </button>
    {/if}
  </form>
</details>
