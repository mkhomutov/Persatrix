<script>
  // Channel-timeline panel (RFC 0048 Phase 1 PR 5) — watch personas interact:
  // pick a channel, see its history newest-first, and keep it live by polling
  // (no channel-push API exists today, OQ4). An optional human publish posts
  // into the channel and the mention fan-out (RFC 0011) surfaces on the next
  // poll. No backend change; pure render-over-existing-API. The shell threads in
  // the /ui/context-derived userId (RFC §F single identity source), so the panel
  // never prompts for or hard-codes a sender.
  import {
    listAgents,
    listChannels,
    getChannelHistory,
    publishMessage,
    ApiError,
  } from "../lib/api.js";
  import { channelLabel } from "../lib/format.js";
  import { nav } from "../lib/nav.svelte.js";
  import OnboardingEmpty from "./OnboardingEmpty.svelte";
  import PublishComposer from "./PublishComposer.svelte";
  import CreateChannelForm from "./CreateChannelForm.svelte";
  import ChannelMessage from "./ChannelMessage.svelte";

  // canCreate gates the "New channel" affordance: the shell passes the create
  // capability already reduced to create.enabled && create.available (RFC 0048
  // channel-creation amendment §A). Defaults false — graceful degradation (§C).
  let { userId, canCreate = false } = $props();

  // POLL_INTERVAL_MS is the steady-state cadence; on a poll error the delay
  // backs off exponentially up to MAX_BACKOFF_MS so an idle or erroring tab does
  // not hammer the unauthenticated localhost surface (RFC §Security / §D.2). The
  // head poll fetches at most HEAD_LIMIT newest messages and de-dupes against
  // what's already shown — it never re-fetches the full history per tick.
  const POLL_INTERVAL_MS = 3000;
  const MAX_BACKOFF_MS = 30000;
  const HISTORY_LIMIT = 50;
  const HEAD_LIMIT = 50;

  let channels = $state([]);
  let channelsError = $state("");
  // channelsLoaded gates the "no channels" empty state so it only shows after a
  // confirmed-empty list, never as a flash of a blank picker mid-load.
  let channelsLoaded = $state(false);
  let selectedChannel = $state("");

  // messages is the panel's view of the channel, newest-first (the wire order).
  let messages = $state([]);
  let historyError = $state("");
  let historyLoaded = $state(false);
  // pollError is a non-fatal banner: the loaded history stays on screen while
  // the poll retries with backoff, so a transient hiccup doesn't blank the view.
  let pollError = $state("");

  let publishContent = $state("");
  let publishing = $state(false);
  let publishError = $state("");

  // agentsById maps sender_id → persona record so the timeline can show
  // "Ada — Researcher" instead of a raw id (RFC 0048 amendment §A / §D). It is
  // best-effort decoration: the load is fire-and-forget and a failure leaves the
  // map empty, so senderLabel falls back to the raw id rather than blocking the
  // timeline (which is the panel's actual job) on the agent list.
  let agentsById = $state({});
  // agents is the ordered list (same load as agentsById) the create form's
  // member multi-select renders — member ids come from the server, never
  // free-typed (amendment §C).
  let agents = $state([]);

  // showCreateForm toggles the collapsed "New channel" affordance (§B); the form
  // (CreateChannelForm) owns the draft state and the POST.
  let showCreateForm = $state(false);

  // seenIds de-dupes the head poll against messages already shown (and a
  // just-published echo). Not reactive; reset on channel switch. Left uncapped on
  // purpose for Slice 1's low-traffic localhost surface (a retention cap rides
  // with the keyset back-fill / push channel later, OQ4).
  let seenIds = new Set();
  // pollTimer holds the pending setTimeout; backoffMs is the current poll delay,
  // reset to the base interval on every success.
  let pollTimer = null;
  let backoffMs = POLL_INTERVAL_MS;
  // polling guards against a second concurrent fetch: the visibility handler
  // invokes poll() directly, and a 'visible' event can land while a timer-fired
  // tick is still awaiting its request. Not reactive — it's in-flight bookkeeping
  // the render never reads.
  let polling = false;
  // loadToken disambiguates superseded loads: switching channel (or unmount)
  // bumps it so an in-flight history fetch or poll can't write to a channel the
  // operator already navigated away from, and Retry is race-safe.
  let loadToken = 0;
  // channelsToken does the same for the channel-list load — kept separate from
  // loadToken so a channels reload (mount/Retry) never invalidates the active
  // history poll, and a resolve-after-unmount can't write to a dead component.
  let channelsToken = 0;

  const canPublish = $derived(
    Boolean(selectedChannel) && publishContent.trim().length > 0 && !publishing,
  );

  // onChannelCreated lands the operator in the channel the form just made: it
  // reuses loadChannels()'s one-shot nav.targetChannel select-this hand-off (§F).
  function onChannelCreated(channel) {
    nav.targetChannel = channel?.id ?? "";
    showCreateForm = false;
    loadChannels();
  }

  function clearPoll() {
    if (pollTimer !== null) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  // scheduleNext arms the next poll, unless the tab is backgrounded (a hidden tab
  // parks polling; the visibility handler resumes it). Clears any pending timer
  // first so callers can't stack two.
  function scheduleNext(delay) {
    clearPoll();
    if (typeof document !== "undefined" && document.hidden) {
      return;
    }
    pollTimer = setTimeout(poll, delay);
  }

  // poll fetches the channel head (HEAD_LIMIT newest), appends only messages not
  // already seen, and reschedules: at the base interval on success, or a backed-
  // off delay on error. The token guard drops a result whose channel was
  // switched out mid-flight.
  async function poll() {
    // Clear (not just null) any armed tick: poll() runs from both its own timer
    // and the visibility handler, so a repeat 'visible' event would otherwise
    // orphan an already-armed tick into one extra poll.
    clearPoll();
    const channel = selectedChannel;
    const token = loadToken;
    if (!channel) {
      return;
    }
    // A tick is already awaiting its fetch; don't fire a second concurrent
    // request — the in-flight tick reschedules the loop when it settles.
    if (polling) {
      return;
    }
    polling = true;
    try {
      const { messages: head } = await getChannelHistory(channel, {
        limit: HEAD_LIMIT,
      });
      if (token !== loadToken) {
        return;
      }
      // head is newest-first; prepend the unseen (newest) ones to keep the shown
      // timeline newest-first without re-sorting. Bound: if more than HEAD_LIMIT
      // land between two ticks, the overflow is never re-fetched — an acceptable
      // Slice 1 gap, closed by keyset back-fill / a push channel later (OQ4).
      const fresh = head.filter((m) => !seenIds.has(m.id));
      if (fresh.length > 0) {
        fresh.forEach((m) => seenIds.add(m.id));
        messages = [...fresh, ...messages];
      }
      pollError = "";
      backoffMs = POLL_INTERVAL_MS;
      scheduleNext(POLL_INTERVAL_MS);
    } catch (err) {
      if (token !== loadToken) {
        return;
      }
      // err is an ApiError out of the client (its message carries the server's
      // wording when present); a non-ApiError still degrades to its message.
      pollError = `Live updates paused: ${err.message}`;
      backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
      scheduleNext(backoffMs);
    } finally {
      polling = false;
    }
  }

  // loadChannels fetches the channel list and is re-runnable (mount + Retry); the
  // channelsToken guard drops a superseded resolution (slow load after Retry/unmount).
  function loadChannels() {
    const token = ++channelsToken;
    channelsError = "";
    channelsLoaded = false;
    return listChannels()
      .then((result) => {
        if (token !== channelsToken) {
          return;
        }
        channels = result.channels ?? [];
        // Honour a one-shot cross-panel/create hand-off (§F): select the
        // requested channel if present, consuming the intent on this successful
        // load (a failed load leaves it for a Retry). Otherwise default to first.
        const requested = nav.targetChannel;
        nav.targetChannel = "";
        if (requested && channels.some((c) => c.id === requested)) {
          selectedChannel = requested;
        } else if (channels.length > 0 && !selectedChannel) {
          selectedChannel = channels[0].id;
        }
      })
      .catch((err) => {
        if (token !== channelsToken) {
          return;
        }
        channelsError = `Could not load channels: ${err.message}`;
      })
      .finally(() => {
        if (token !== channelsToken) {
          return;
        }
        channelsLoaded = true;
      });
  }

  // loadHistory replaces the timeline with the channel's history and (re)starts
  // polling from a clean de-dupe set; the loadToken bump invalidates prior work.
  function loadHistory(channel) {
    const token = ++loadToken;
    clearPoll();
    historyError = "";
    pollError = "";
    historyLoaded = false;
    messages = [];
    seenIds = new Set();
    backoffMs = POLL_INTERVAL_MS;
    // Clear the in-flight flag too: the loadToken bump stranded any prior poll,
    // so without this reset its stale flag could make the new channel's first
    // poll bail on the guard and leave the loop unarmed.
    polling = false;
    return getChannelHistory(channel, { limit: HISTORY_LIMIT })
      .then(({ messages: history }) => {
        if (token !== loadToken) {
          return;
        }
        messages = history;
        history.forEach((m) => seenIds.add(m.id));
        scheduleNext(POLL_INTERVAL_MS);
      })
      .catch((err) => {
        if (token !== loadToken) {
          return;
        }
        historyError = `Could not load history: ${err.message}`;
      })
      .finally(() => {
        if (token !== loadToken) {
          return;
        }
        historyLoaded = true;
      });
  }

  // retryHistory re-runs the selected channel's load after an initial-load
  // failure: the poll loop only arms on success and re-selecting the same channel
  // fires no onchange, so without this a failed first fetch is a dead end.
  function retryHistory() {
    if (selectedChannel) {
      loadHistory(selectedChannel);
    }
  }

  // Load the agent list once for sender-name decoration (and the create form's
  // member list). Best-effort: a failure is swallowed and never gates the panel.
  $effect(() => {
    let cancelled = false;
    listAgents()
      .then((list) => {
        if (cancelled) return;
        agentsById = Object.fromEntries(list.map((agent) => [agent.id, agent]));
        agents = list;
      })
      .catch(() => {
        // Decoration only — leave the map empty and fall back to raw ids.
      });
    return () => {
      cancelled = true;
    };
  });

  $effect(() => {
    loadChannels();
    return () => {
      // Invalidate the in-flight channels load and stop polling on unmount.
      // (The selected-channel effect's own cleanup invalidates history/poll.)
      channelsToken++;
      clearPoll();
    };
  });

  // React to the selected channel: reload history and restart the poll loop; the
  // cleanup invalidates the prior channel's in-flight work.
  $effect(() => {
    const channel = selectedChannel;
    if (!channel) {
      return;
    }
    loadHistory(channel);
    return () => {
      loadToken++;
      clearPoll();
    };
  });

  // Pause polling while the tab is backgrounded and catch up when it returns —
  // the load-protection contract for the unauthenticated surface. Reveal polls
  // immediately, then resumes the cadence.
  $effect(() => {
    function onVisibility() {
      if (document.hidden) {
        clearPoll();
      } else if (selectedChannel && historyLoaded && !historyError) {
        poll();
      }
    }
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  });

  async function publish() {
    if (publishing) {
      return;
    }
    const content = publishContent.trim();
    if (!selectedChannel || content.length === 0) {
      return;
    }
    // Capture the load token before the await: the <select> stays enabled during
    // a publish, so a channel switch mid-flight must not echo this message into
    // (nor seed its id against) the now-current channel. Mirrors poll()/loadHistory().
    const token = loadToken;
    publishError = "";
    publishing = true;
    try {
      const stored = await publishMessage(selectedChannel, {
        senderId: userId,
        content,
      });
      if (token !== loadToken) {
        // Superseded by a channel switch: drop the echo (the message persisted
        // and surfaces on its own channel's load/poll).
        return;
      }
      // Echo the stored message immediately; the de-dupe set keeps the next poll
      // from re-adding it. The agent mention fan-out (RFC 0011) surfaces later.
      if (stored && stored.id && !seenIds.has(stored.id)) {
        seenIds.add(stored.id);
        messages = [stored, ...messages];
      }
      publishContent = "";
    } catch (err) {
      if (token !== loadToken) {
        // A failure for a channel the operator already left isn't actionable —
        // surfacing it over the new selection would read as if it were broken.
        return;
      }
      publishError =
        err instanceof ApiError
          ? err.message
          : `The message could not be posted: ${err.message}`;
    } finally {
      publishing = false;
    }
  }

  function onPublishSubmit(event) {
    event.preventDefault();
    publish();
  }

  // Drop a stale publish error on channel switch — it refers to the prior
  // channel's attempt and would read as if the new selection were broken.
  function onChannelChange() {
    publishError = "";
  }

  // Internal order is newest-first (poll prepends, publish echoes to the front);
  // the panel RENDERS oldest-top, newest-bottom (RFC 0048 amendment §D). Reverse
  // a shallow copy for display; the internal model and its de-dupe are untouched.
  const displayMessages = $derived(messages.slice().reverse());

  // Pinned-scroll autoscroll: a new message scrolls to the bottom ONLY when the
  // operator is already there, so reading history isn't yanked away (§D caveat).
  let timelineEl = $state(null);
  let pinnedToBottom = true;
  const PIN_EPSILON_PX = 40;

  function onTimelineScroll() {
    if (!timelineEl) return;
    const distance =
      timelineEl.scrollHeight - timelineEl.scrollTop - timelineEl.clientHeight;
    pinnedToBottom = distance < PIN_EPSILON_PX;
  }

  // After the list changes, stick to the bottom if we were pinned; a reader who
  // scrolled up (pinnedToBottom false as of the last scroll) is left in place.
  $effect(() => {
    // Touch the reactive length so this effect tracks message changes.
    void displayMessages.length;
    if (timelineEl && pinnedToBottom) {
      timelineEl.scrollTop = timelineEl.scrollHeight;
    }
  });

  // Mirror the chat composer (§D): Enter posts, Shift+Enter inserts a newline.
  function onPublishKeydown(event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      publish();
    }
  }
</script>

<section class="panel channels" aria-label="Channels">
  <h2>Channels</h2>
  <p class="identity">Acting as <code>{userId}</code></p>

  {#if channelsError}
    <p class="boot error" role="alert">{channelsError}</p>
    <button type="button" class="retry" onclick={loadChannels}>Retry</button>
  {:else if !channelsLoaded}
    <p class="loading" role="status">Loading channels…</p>
  {:else if channels.length === 0}
    <!-- Onboarding, not a dead end (§F): no channels yet on a fresh stack. A
         human↔persona chat creates a DM channel, and group channels come from
         config; say so and offer a re-check. -->
    <OnboardingEmpty title="No channels exist yet." onRetry={loadChannels}>
      Chat with a persona to start a DM, or define group channels in
      <code>config/channels.yaml</code>, then re-check.
    </OnboardingEmpty>
  {:else}
    <div class="channel-picker">
      <label>
        Channel
        <select bind:value={selectedChannel} onchange={onChannelChange}>
          {#each channels as channel (channel.id)}
            <option value={channel.id}>{channelLabel(channel)}</option>
          {/each}
        </select>
      </label>
      <!-- Refresh the channel list without a full reload (§D). -->
      <button type="button" class="refresh" onclick={loadChannels}>Refresh</button>
      {#if canCreate}
        <!-- Structural-write affordance (channel-creation amendment §B): opens
             the collapsed create form. -->
        <button
          type="button"
          class="new-channel"
          onclick={() => (showCreateForm = true)}>New channel</button
        >
      {/if}
    </div>

    {#if canCreate && showCreateForm}
      <CreateChannelForm
        {agents}
        {userId}
        onCreated={onChannelCreated}
        onCancel={() => (showCreateForm = false)}
      />
    {/if}

    {#if pollError}
      <!-- Non-fatal: the loaded history stays visible while the poll backs off
           and retries, so a transient error doesn't blank the timeline. -->
      <p class="poll-error" role="status">{pollError}</p>
    {/if}

    {#if historyError}
      <p class="boot error" role="alert">{historyError}</p>
      <button type="button" class="retry" onclick={retryHistory}>Retry</button>
    {:else if !historyLoaded}
      <p class="loading" role="status">Loading messages…</p>
    {:else if messages.length === 0}
      <p class="empty">No messages yet.</p>
    {:else}
      <ol
        class="timeline"
        aria-label="Channel messages"
        bind:this={timelineEl}
        onscroll={onTimelineScroll}
      >
        {#each displayMessages as message (message.id)}
          <ChannelMessage {message} {userId} {agentsById} />
        {/each}
      </ol>
    {/if}

    {#if publishError}
      <p class="boot error" role="alert">{publishError}</p>
    {/if}

    <PublishComposer
      bind:content={publishContent}
      {publishing}
      {canPublish}
      onSubmit={onPublishSubmit}
      onKeydown={onPublishKeydown}
    />
  {/if}
</section>
