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
  import ChannelMessage from "./ChannelMessage.svelte";

  let { userId } = $props();

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

  // seenIds de-dupes the head poll against messages already shown (and against a
  // just-published echo). Not reactive — it's bookkeeping the render reads
  // through `messages`, reset whenever the active channel changes. Both it and
  // `messages` grow unbounded across a long-lived session on a busy channel
  // (only a channel switch clears them); left uncapped on purpose for Slice 1's
  // low-traffic localhost surface — a retention cap rides with the keyset
  // back-fill / push channel later (OQ4), rather than silently dropping the tail.
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

  function clearPoll() {
    if (pollTimer !== null) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  // scheduleNext arms the next poll, unless the tab is backgrounded — a hidden
  // tab parks polling entirely (Page Visibility API) and the visibility handler
  // resumes it. Always clears any pending timer first so callers can't stack two.
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
    // Clear (not just null) any armed tick: poll() is invoked both by its own
    // setTimeout and directly by the visibility handler on resume. A repeat
    // 'visible' event while a tick is already armed would otherwise orphan that
    // tick — leaving the browser to fire it later as one extra poll. clearPoll()
    // is a no-op on the already-elapsed handle when poll() runs from the timer.
    clearPoll();
    const channel = selectedChannel;
    const token = loadToken;
    if (!channel) {
      return;
    }
    // A tick is already awaiting its fetch (the visibility handler can call
    // poll() directly mid-flight). Don't fire a second concurrent request — the
    // in-flight tick reschedules the loop when it settles, so the duplicate buys
    // nothing and only adds load to the unauthenticated localhost surface.
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
      // head is newest-first; the unseen ones are the newest, so prepending them
      // (in their newest-first order) ahead of the existing list keeps the shown
      // timeline newest-first without re-rendering or re-sorting what's shown.
      // Note the bound: if more than HEAD_LIMIT messages land between two ticks,
      // the ones past the head's window (older than the head, newer than what's
      // shown) are never re-fetched — an acceptable gap for Slice 1's poll-based,
      // low-traffic localhost surface, to be closed by keyset back-fill or a
      // push channel later (OQ4).
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

  // loadChannels fetches the channel list and is re-runnable (mount + Retry).
  // The channelsToken guard drops a superseded resolution (a slow load that
  // settles after a Retry, or after unmount), mirroring Chat's loadAgents.
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
        // Honour a cross-panel hand-off (§F): if the chat panel asked to open a
        // specific DM, select it. The request is one-shot, scoped to the mount
        // it triggered, so consume it on this successful load whether or not the
        // channel turned up — leaving a stale intent would surface an unexpected
        // jump on a later, unrelated mount/Refresh. (A failed load never reaches
        // here, so the intent still survives to a Retry.) Otherwise default to
        // the first channel.
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

  // loadHistory replaces the timeline with the selected channel's history and
  // (re)starts polling from a clean de-dupe set. Bumping loadToken invalidates
  // any in-flight fetch/poll from the previous channel.
  function loadHistory(channel) {
    const token = ++loadToken;
    clearPoll();
    historyError = "";
    pollError = "";
    historyLoaded = false;
    messages = [];
    seenIds = new Set();
    backoffMs = POLL_INTERVAL_MS;
    // Clear the in-flight flag too: bumping loadToken just stranded any prior
    // channel's poll (it bails on the token mismatch without rescheduling), so
    // without this reset that stale tick's flag could make the new channel's
    // first poll bail on the guard and leave the loop unarmed.
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
  // failure. The poll loop only arms on a successful load, and re-selecting the
  // same channel fires no onchange, so without this a failed first history fetch
  // is a dead end (a single-channel console would be stuck until reload). Mirrors
  // the channel-list Retry; loadHistory's loadToken bump keeps it race-safe.
  function retryHistory() {
    if (selectedChannel) {
      loadHistory(selectedChannel);
    }
  }

  // Load the agent list once for sender-name decoration. Best-effort and
  // independent of the channel/poll lifecycle: a failure is swallowed (the
  // timeline still renders raw ids), so this never gates the panel.
  $effect(() => {
    let cancelled = false;
    listAgents()
      .then((list) => {
        if (cancelled) return;
        agentsById = Object.fromEntries(list.map((agent) => [agent.id, agent]));
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

  // React to the selected channel: each change reloads history and restarts the
  // poll loop; the cleanup invalidates the prior channel's in-flight work so a
  // slow fetch can't write to the newly-selected channel.
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
  // the load-protection contract the RFC calls out for the unauthenticated
  // surface. Hiding clears the pending timer; revealing polls immediately (then
  // resumes the cadence) so a returning operator sees fresh messages at once.
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
    // Capture the active channel's load token before the await. The channel
    // <select> stays enabled during a publish, so the operator can switch
    // channels (or the panel can unmount) while this POST is in flight. The
    // publish targets the channel it was issued against (publishMessage reads
    // selectedChannel now, before the await); if the channel has since changed,
    // the resolution must not echo the stored message into — nor seed its id
    // against — the now-current channel. Mirrors the loadToken guard poll() /
    // loadHistory() apply to their own resolutions.
    const token = loadToken;
    publishError = "";
    publishing = true;
    try {
      const stored = await publishMessage(selectedChannel, {
        senderId: userId,
        content,
      });
      if (token !== loadToken) {
        // Superseded by a channel switch (or unmount): drop the echo. The
        // message persisted in its own channel and surfaces there on that
        // channel's own load/poll — it must not appear under the new selection.
        return;
      }
      // Echo the stored message immediately rather than waiting a poll interval;
      // the de-dupe set keeps the upcoming poll from re-adding it. The agent
      // mention fan-out (RFC 0011) still surfaces on a later poll.
      if (stored && stored.id && !seenIds.has(stored.id)) {
        seenIds.add(stored.id);
        messages = [stored, ...messages];
      }
      publishContent = "";
    } catch (err) {
      if (token !== loadToken) {
        // A failure for a channel the operator already left is not actionable
        // here — surfacing it over the new selection (which onChannelChange just
        // cleared) would read as though the new channel is broken.
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

  // onChannelChange drops a stale publish error when the operator switches
  // channel — it refers to the prior channel's attempt, so leaving it over the
  // new selection reads as if the new channel is already broken.
  function onChannelChange() {
    publishError = "";
  }

  // The wire/internal order is newest-first (poll prepends, publish echoes to the
  // front); the panel RENDERS oldest-top, newest-bottom — a conversation read
  // top-down, with the newest message and the publish box co-located at the
  // bottom (RFC 0048 amendment §D). Reverse a shallow copy for display; the
  // internal newest-first model and its de-dupe are untouched.
  const displayMessages = $derived(messages.slice().reverse());

  // Pinned-scroll autoscroll: a new message scrolls the timeline to the bottom
  // ONLY when the operator is already there. If they have scrolled up to read
  // history, autoscroll must not yank them back every poll tick (§D caveat).
  let timelineEl = $state(null);
  let pinnedToBottom = true;
  const PIN_EPSILON_PX = 40;

  function onTimelineScroll() {
    if (!timelineEl) return;
    const distance =
      timelineEl.scrollHeight - timelineEl.scrollTop - timelineEl.clientHeight;
    pinnedToBottom = distance < PIN_EPSILON_PX;
  }

  // After the message list changes, stick to the bottom if we were pinned. The
  // effect reads displayMessages so it re-runs on every append; the pinned check
  // reflects the scroll position as of the last user scroll, so a reader who
  // scrolled up is left in place.
  $effect(() => {
    // Touch the reactive length so this effect tracks message changes.
    void displayMessages.length;
    if (timelineEl && pinnedToBottom) {
      timelineEl.scrollTop = timelineEl.scrollHeight;
    }
  });

  // onPublishKeydown mirrors the chat composer (§D): Enter posts, Shift+Enter
  // inserts a newline, so the two write surfaces behave the same.
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
      <!-- Refresh the channel list without a full reload — a new channel (or DM)
           created since mount appears on demand (§D). -->
      <button type="button" class="refresh" onclick={loadChannels}>Refresh</button>
    </div>

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
