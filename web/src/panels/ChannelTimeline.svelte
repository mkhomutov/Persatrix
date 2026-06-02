<script>
  // Channel-timeline panel (RFC 0048 Phase 1 PR 5) — watch personas interact:
  // pick a channel, see its history newest-first, and keep it live by polling
  // (no channel-push API exists today, OQ4). An optional human publish posts
  // into the channel and the mention fan-out (RFC 0011) surfaces on the next
  // poll. No backend change; pure render-over-existing-API. The shell threads in
  // the /ui/context-derived userId (RFC §F single identity source), so the panel
  // never prompts for or hard-codes a sender.
  import {
    listChannels,
    getChannelHistory,
    publishMessage,
    ApiError,
  } from "../lib/api.js";

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

  // seenIds de-dupes the head poll against messages already shown (and against a
  // just-published echo). Not reactive — it's bookkeeping the render reads
  // through `messages`, reset whenever the active channel changes.
  let seenIds = new Set();
  // pollTimer holds the pending setTimeout; backoffMs is the current poll delay,
  // reset to the base interval on every success.
  let pollTimer = null;
  let backoffMs = POLL_INTERVAL_MS;
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
        if (channels.length > 0 && !selectedChannel) {
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

  // channelLabel mirrors Chat's persona label: the channel's name, falling back
  // to its id (DMs/threads have no name — channel_types.go).
  function channelLabel(channel) {
    return channel.name ? channel.name : channel.id;
  }

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
    <p class="empty">No channels exist yet.</p>
  {:else}
    <label>
      Channel
      <select bind:value={selectedChannel} onchange={onChannelChange}>
        {#each channels as channel (channel.id)}
          <option value={channel.id}>{channelLabel(channel)}</option>
        {/each}
      </select>
    </label>

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
      <ol class="timeline" aria-label="Channel messages">
        {#each messages as message (message.id)}
          <li class="message">
            <span class="sender">{message.sender_id}</span>
            <span class="content">{message.content}</span>
            <time class="ts" datetime={message.timestamp}>{message.timestamp}</time>
          </li>
        {/each}
      </ol>
    {/if}

    {#if publishError}
      <p class="boot error" role="alert">{publishError}</p>
    {/if}

    <!-- The optional human publish: a clearly-labelled write action so it reads
         as deliberate, not a search box. The sender is the /ui/context principal
         (userId) — never a free-text field (RFC §F rule 1). -->
    <form class="publish" onsubmit={onPublishSubmit}>
      <label>
        Message
        <textarea
          bind:value={publishContent}
          rows="2"
          placeholder="Post a message to this channel…"
          disabled={publishing}
        ></textarea>
      </label>
      <button type="submit" disabled={!canPublish}>
        {publishing ? "Posting…" : "Post"}
      </button>
    </form>
  {/if}
</section>
