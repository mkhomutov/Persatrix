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
  import { ApiError } from "../lib/api.js";
  import {
    addChannelMember,
    removeChannelMember,
    updateChannelMember,
  } from "../lib/api.members.js";
  import { isChattable } from "../lib/agents.js";

  let { channelId, members = [], agents = [], agentsById = {}, userId, onChanged } =
    $props();

  let addId = $state("");
  let addRespond = $state("when_mentioned");
  let busy = $state(false); // an add, remove, or edit is in flight
  let busyMember = $state(""); // the member id being removed (per-row disable)
  let error = $state("");

  // Inline member-config editor (RFC 0050 member-config edit). One row edits at a
  // time. `editThreshold` is a numeric binding: null/empty = unset the salience
  // bar. `editRespond` defaults to the member's persisted (legacy-triple) respond
  // — the edit is a full REPLACE, and the server REQUIRES the disposition because
  // `salience_gated` is derived from it and unrecoverable from persisted state.
  let editingMember = $state(""); // member id being edited ("" = none)
  let editRespond = $state("when_mentioned");
  let editThreshold = $state(null);

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

  function startEdit(member) {
    if (busy) {
      return;
    }
    error = "";
    editingMember = member.id;
    // The persisted `respond` reads back as the normalized legacy triple: the
    // store collapses an open-floor participant/chair to "always", so the
    // declared disposition is unrecoverable from persisted state. Seeding the
    // editor with a literal "always" would be a silent demotion trap — the
    // server re-derives `salience_gated` from the disposition we send back, and
    // "always" with no explicit threshold resolves to salience_gated=false. So
    // for a member the store still reports as salience-gated we re-declare an
    // open-floor disposition (participant; chair is indistinguishable here and
    // resolves to the same RespondAlways canonical + bid). This keeps a no-op
    // save and an unset-the-bar edit bias-to-silence instead of un-gating.
    editRespond =
      member.salience_gated && member.respond === "always"
        ? "participant"
        : member.respond;
    editThreshold = member.threshold ?? null;
  }

  function cancelEdit() {
    editingMember = "";
  }

  async function saveEdit() {
    if (busy || !editingMember) {
      return;
    }
    // Empty/NaN clears the bar; the server is the authority on the [0, 1] range.
    const threshold =
      editThreshold === null || editThreshold === "" || Number.isNaN(editThreshold)
        ? null
        : Number(editThreshold);
    error = "";
    busy = true;
    const id = editingMember;
    try {
      await updateChannelMember(channelId, id, {
        respond: editRespond,
        threshold,
      });
      editingMember = "";
      await onChanged?.();
    } catch (err) {
      error =
        err instanceof ApiError
          ? err.message
          : `Could not update the member: ${err.message}`;
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
          {#if editingMember === member.id}
            <!-- Inline member-config editor: replace disposition + threshold. -->
            <select
              bind:value={editRespond}
              aria-label={`Disposition for ${displayName(member.id)}`}
            >
              <option value="when_mentioned">When mentioned</option>
              <option value="participant">Participant (salience bid)</option>
              <option value="chair">Chair (facilitator)</option>
              <option value="addressed">Addressed only</option>
              <option value="observer">Observer (never replies)</option>
              <option value="always">Always</option>
              <option value="never">Never (post-only)</option>
            </select>
            <input
              type="number"
              min="0"
              max="1"
              step="0.05"
              placeholder="unset"
              bind:value={editThreshold}
              aria-label={`Salience threshold for ${displayName(member.id)}`}
            />
            <button type="button" class="save" disabled={busy} onclick={saveEdit}>
              {busy ? "Saving…" : "Save"}
            </button>
            <button type="button" class="cancel" disabled={busy} onclick={cancelEdit}>
              Cancel
            </button>
          {:else}
            <span class="member-respond">{member.respond}</span>
            {#if salienceNote(member)}
              <span class="member-salience">{salienceNote(member)}</span>
            {/if}
            {#if member.id !== userId}
              <!-- Edit + Remove are withheld for the acting user: they are the
                   /ui/context principal, not a registered/governed agent, so the
                   add picker could never re-add them, a non-member sender is
                   rejected on publish (ErrNotMember → 403), and a salience
                   threshold on a human principal is meaningless. Withholding
                   avoids a one-click, web-unrecoverable self-lockout. -->
              <button
                type="button"
                class="edit"
                disabled={busy}
                aria-label={`Edit ${displayName(member.id)}`}
                onclick={() => startEdit(member)}
              >
                Edit
              </button>
              <button
                type="button"
                class="remove"
                disabled={busy}
                aria-label={`Remove ${displayName(member.id)}`}
                onclick={() => remove(member.id)}
              >
                {busyMember === member.id ? "Removing…" : "Remove"}
              </button>
            {/if}
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
