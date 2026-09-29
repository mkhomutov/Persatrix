<script>
  // The conversation sidebar — the two ways into a conversation (watch a group
  // channel / DM a persona) as keyboard-driven lists, one "Jump to…" box that
  // narrows both (Ctrl/⌘+K focuses it from anywhere; Enter opens the first
  // match), and the acting-identity echo (§E/§F). Built to stay usable with
  // hundreds of rows: sorted by name, filtered by every typed word, sections
  // foldable. On narrow screens the parent shows it as a drawer (`open`).
  //
  // groupChannels — the watched-channel candidates, already sorted (no DMs).
  // agents        — every registered agent; task agents list last, disabled.
  // selectedChannel / selectedAgent / isDM — what is open right now.
  // sending       — a DM turn is in flight: the persona list is locked.
  // canCreate     — gates the "New channel" affordance.
  // userId        — the effective identity echoed at the foot (§E/§F).
  // open          — drawer state on narrow screens.
  // onPickChannel(id) / onPickPersona(id) / onRefresh() / onRefreshAgents() /
  // onNewChannel() / onRequestOpen() / onClose() — the panel's handlers.
  import SelectList from "../ui/SelectList.svelte";
  import Icon from "../ui/Icon.svelte";
  import Avatar from "../ui/Avatar.svelte";
  import NoPersonasHint from "./NoPersonasHint.svelte";
  import { channelLabel } from "../lib/format.js";
  import { isChattable, agentLabel } from "../lib/agents.js";
  import { matchesQuery, byLabel } from "../lib/filter.js";
  import { readPref, writePref } from "../lib/prefs.js";

  let {
    groupChannels = [],
    agents = [],
    selectedChannel = "",
    selectedAgent = "",
    isDM = false,
    sending = false,
    canCreate = false,
    userId,
    open = false,
    onPickChannel,
    onPickPersona,
    onRefresh,
    onRefreshAgents,
    onNewChannel,
    onRequestOpen,
    onClose,
  } = $props();

  let query = $state("");
  let filterEl = $state(null);
  let channelList = $state(null);
  let personaList = $state(null);
  // Which sections are folded — remembered per browser; a missing or garbled
  // preference reads as both open.
  const savedFold = readPref("railFolded", null);
  let folded = $state({
    channels: savedFold?.channels === true,
    personas: savedFold?.personas === true,
  });

  const filtering = $derived(query.trim().length > 0);
  // The jump shortcut is ⌘K on a Mac — where Ctrl+K belongs to text fields
  // (delete to the end of the line) — and Ctrl+K elsewhere.
  const isMac = /mac|iphone|ipad/i.test(globalThis.navigator?.platform ?? "");
  const shortcutHint = isMac ? "⌘K" : "Ctrl K";

  // Personas by name, then task agents (shown with the reason they cannot be
  // picked, never hidden — RFC 0048 §A "show but explain").
  const sortedAgents = $derived.by(() => {
    const name = (a) => a.name || a.id;
    const chattable = agents.filter(isChattable).sort(byLabel(name));
    const tasks = agents.filter((a) => !isChattable(a)).sort(byLabel(name));
    return [...chattable, ...tasks];
  });

  const shownChannels = $derived(
    groupChannels.filter((c) =>
      matchesQuery([channelLabel(c), c.id, c.description], query),
    ),
  );
  const shownAgents = $derived(
    sortedAgents.filter((a) =>
      matchesQuery([a.name, a.id, a.role, ...(a.capabilities ?? [])], query),
    ),
  );

  // A filter forces both sections open, so folding waits until it is cleared
  // (the toggles are disabled meanwhile).
  function toggleFold(section) {
    if (filtering) return;
    folded = { ...folded, [section]: !folded[section] };
    writePref("railFolded", folded);
  }

  // Enter opens the first match — a channel if any matched, else the first
  // persona that can hold a conversation — and clears the box. An empty box
  // matches everything, so Enter there does nothing.
  function openFirstMatch() {
    if (!filtering) return;
    const channel = shownChannels[0];
    const persona = shownAgents.find(isChattable);
    if (channel) {
      onPickChannel?.(channel.id);
    } else if (persona && !sending) {
      onPickPersona?.(persona.id);
    } else {
      return;
    }
    query = "";
  }

  function onFilterKeydown(event) {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      openFirstMatch();
    } else if (event.key === "Escape") {
      if (query) {
        event.preventDefault();
        query = "";
      } else {
        filterEl?.blur();
      }
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!channelList?.focusFirst()) {
        personaList?.focusFirst();
      }
    }
  }

  // ⌘K / Ctrl+K from anywhere focuses the box (and opens the drawer on a narrow
  // screen, where the sidebar is off-canvas) — unless a modal dialog is open,
  // which keeps focus. Escape closes an open drawer, after the box has had its
  // own Escape. Some keydowns carry no `key` (browser autofill).
  function onWindowKeydown(event) {
    const shortcut = isMac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
    if (shortcut && !event.altKey && event.key?.toLowerCase() === "k") {
      if (document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      event.preventDefault();
      onRequestOpen?.();
      filterEl?.focus();
      filterEl?.select();
    } else if (event.key === "Escape" && open && !event.defaultPrevented) {
      onClose?.();
    }
  }

  // Opening the drawer moves focus into it, so keyboard users land in the list
  // they asked for (the box itself would pop a phone's keyboard).
  let railEl = $state(null);
  $effect(() => {
    if (open && railEl && !railEl.contains(document.activeElement)) {
      railEl.focus();
    }
  });
</script>

<svelte:window onkeydown={onWindowKeydown} />

<aside class="rail" class:open aria-label="Conversations" tabindex="-1" bind:this={railEl}>
  <div class="rail-top">
    <label class="rail-search">
      <Icon name="search" size={15} />
      <span class="sr-only">Filter conversations</span>
      <input
        type="search"
        bind:this={filterEl}
        bind:value={query}
        placeholder="Jump to…"
        autocomplete="off"
        spellcheck="false"
        onkeydown={onFilterKeydown}
      />
      <kbd aria-hidden="true">{shortcutHint}</kbd>
    </label>
    <button type="button" class="btn-icon btn-ghost" aria-label="Refresh" title="Refresh conversations" onclick={onRefresh}>
      <Icon name="refresh" size={16} />
    </button>
    <button type="button" class="btn-icon btn-ghost rail-close" aria-label="Hide conversations" onclick={onClose}>
      <Icon name="x" size={16} />
    </button>
  </div>

  <div class="rail-scroll">
    <section class="rail-section">
      <div class="rail-head">
        <button type="button" class="rail-toggle" aria-expanded={filtering || !folded.channels} disabled={filtering} onclick={() => toggleFold("channels")}>
          <Icon name="chevron-down" size={14} />
          <span class="rail-title">Channels</span>
          <span class="rail-count" aria-hidden="true">{filtering ? `${shownChannels.length}/${groupChannels.length}` : groupChannels.length}</span>
        </button>
        {#if canCreate}
          <button type="button" class="btn-icon sm btn-ghost new-channel" aria-label="New channel" title="New channel" onclick={onNewChannel}>
            <Icon name="plus" size={16} />
          </button>
        {/if}
      </div>
      {#if filtering || !folded.channels}
        <SelectList
          bind:this={channelList}
          label="Channels"
          items={shownChannels}
          selectedKey={isDM ? "" : selectedChannel}
          getLabel={channelLabel}
          onselect={(c) => onPickChannel?.(c.id)}
          emptyText={filtering ? "No channels match." : canCreate ? "No channels yet — create one with +." : "No group channels yet."}
        >
          {#snippet row(channel)}
            <span class="row-icon"><Icon name="hash" size={15} /></span>
            <span class="row-name">{channelLabel(channel)}</span>
            {#if channel.members?.length}
              <span class="row-meta" aria-hidden="true" title="{channel.members.length} members">{channel.members.length}</span>
            {/if}
          {/snippet}
        </SelectList>
      {/if}
    </section>

    <section class="rail-section">
      <div class="rail-head">
        <button type="button" class="rail-toggle" aria-expanded={filtering || !folded.personas} disabled={filtering} onclick={() => toggleFold("personas")}>
          <Icon name="chevron-down" size={14} />
          <span class="rail-title">Direct messages</span>
          <span class="rail-count" aria-hidden="true">{filtering ? `${shownAgents.length}/${agents.length}` : agents.length}</span>
        </button>
      </div>
      {#if agents.length === 0}
        <!-- No personas registered: the entry point explains itself (why +
             the cloud-demo cause) rather than vanishing. -->
        <NoPersonasHint onRefresh={onRefreshAgents} />
      {:else if filtering || !folded.personas}
        <SelectList
          bind:this={personaList}
          label="Personas"
          items={shownAgents}
          selectedKey={isDM ? selectedAgent : ""}
          getLabel={agentLabel}
          isDisabled={(a) => !isChattable(a) || sending}
          onselect={(a) => onPickPersona?.(a.id)}
          emptyText="No personas match."
        >
          {#snippet row(agent)}
            <Avatar id={agent.id} label={agent.name || agent.id} size={22} status={isChattable(agent) ? agent.status : ""} />
            <span class="row-name">{agent.name || agent.id}</span>
            {#if !isChattable(agent)}
              <span class="chip row-chip">task</span>
            {:else if agent.role}
              <span class="row-sub">{agent.role}</span>
            {/if}
          {/snippet}
        </SelectList>
      {/if}
    </section>
  </div>

  <p class="identity rail-foot">
    <Avatar id={userId} label={userId} size={24} self />
    <span>Acting as <code>{userId}</code></span>
  </p>
</aside>
