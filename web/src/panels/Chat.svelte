<script>
  // Chat panel (RFC 0048 Phase 1 PR 4) — the "feel the taste" moment: pick a
  // persona and talk to it over today's synchronous chat API. No backend change;
  // pure render-over-existing-API. The shell threads in the /ui/context-derived
  // userId (RFC §F single identity source), so the panel never prompts for or
  // hard-codes a user.
  import {
    listAgents,
    sendChat,
    getChatHistory,
    ApiError,
  } from "../lib/api.js";

  let { userId } = $props();

  // chatMaxMessageLength mirrors the server's constant (chat_handler.go) so an
  // over-length message is rejected with immediate feedback instead of burning a
  // round-trip; the server still enforces it — this is a courtesy guard, not the
  // authority. The server measures in runes (utf8.RuneCountInString), so the
  // guard counts code points too (`[...text].length`) — a UTF-16 `.length` would
  // over-count astral characters and falsely block a message the server accepts.
  const MAX_MESSAGE_LENGTH = 4000;

  let agents = $state([]);
  let agentsError = $state("");
  // agentsLoaded flips true once the first (or a retried) load settles, so the
  // "no personas" empty state only shows after a confirmed-empty list — never as
  // a flash of the blank picker while the load is still in flight.
  let agentsLoaded = $state(false);
  let selectedAgent = $state("");
  let message = $state("");
  let sessionId = $state("");
  let epochId = $state("");
  let sending = $state(false);
  let sendError = $state("");
  // The transcript is a flat, conversational (oldest-top) message list — the
  // shape RFC 0048 amendment §B migrates to. Each entry is one message:
  //   { id, fromUser, who, content, status?, timestamp, session?, epoch? }
  // On persona-select it is SEEDED from the persisted DM history (so a reload
  // resumes the conversation rather than presenting as stateless), then live
  // turns append to the bottom. A flat list (vs {prompt,reply} pairs) handles
  // the persisted ordering and any non-paired messages naturally.
  let transcript = $state([]);
  // Live (this-session) messages get a locally-minted id; seeded history uses
  // the server message id. The `local-` prefix can never collide with a server
  // id, keeping the {#each} key stable across a seed-then-send sequence.
  let nextLocalId = 0;
  // History-seed state, separate from the agent-list load: a persona switch
  // reloads THAT persona's conversation. historyError is non-fatal (you can
  // still send into a fresh transcript); historyToken guards superseded loads.
  let historyLoading = $state(false);
  let historyError = $state("");
  let historyToken = 0;
  // The resolved DM channel id, captured from seeded history when present, for
  // the §F "view this conversation in the timeline" deep-link (PR F). Empty
  // until a conversation exists (a fresh persona has no DM yet). NOTE for §F: a
  // live send into a fresh persona creates the DM server-side but does NOT
  // populate this — chatResponse carries no channel id — so the deep-link stays
  // empty until the next reload reseeds from history. §F should surface the id
  // on the chat response (or re-resolve) rather than rely on a reload.
  let dmChannelId = $state("");

  const canSend = $derived(
    Boolean(selectedAgent) && message.trim().length > 0 && !sending,
  );

  // selectedAgentInfo is the full persona record behind the picker selection, so
  // the panel can render a persona header (name — role, capabilities) above the
  // transcript rather than leaving the persona a bare dropdown entry. The DTO
  // already carries role (RFC 0048 amendment §A) and capabilities/address
  // (always served) — "faceless" was the client throwing those away, not the API
  // withholding them.
  const selectedAgentInfo = $derived(
    agents.find((agent) => agent.id === selectedAgent) ?? null,
  );

  // personaName is the label shown for the agent's own messages — its name (or
  // id), mirroring agentLabel's fallback.
  const personaName = $derived(
    selectedAgentInfo
      ? selectedAgentInfo.name || selectedAgentInfo.id
      : selectedAgent,
  );

  // historyToEntry maps a persisted DM message into a flat transcript entry. The
  // operator's own messages (sender_id === userId) render as "You"; everything
  // else in a DM is the persona. Seeded history carries no per-turn scope
  // annotation (a persisted message does not record the session/epoch override),
  // so by amendment §B caveat 2 the scope line is shown only on live turns.
  function historyToEntry(m) {
    const fromUser = m.sender_id === userId;
    return {
      id: m.id,
      fromUser,
      who: fromUser ? "You" : personaName,
      content: m.content,
      status: m.metadata?.reply_status,
      timestamp: m.timestamp,
    };
  }

  // loadHistory seeds the transcript from the selected persona's persisted DM,
  // making a reload resume the conversation (the point of §B). The wire order is
  // newest-first; the panel renders oldest-top (conversational), so the seed is
  // reversed. A fresh persona returns an empty list (200, not 404) — a clean
  // empty transcript, not an error. historyError is non-fatal: a failed seed
  // still leaves a usable (empty) composer rather than blocking the panel.
  //
  // Scope note: the seed fetches only the server's default-limit most-recent
  // page (channelDefaultHistoryLimit, 50) and the panel has no "load earlier"
  // affordance yet, so a conversation longer than that resumes from its tail
  // with the oldest turns omitted. getChatHistory already plumbs limit/before
  // for the paginating back-fill a later slice (§F) adds; until then the cap is
  // intentional, not full resume.
  function loadHistory(agentID) {
    const token = ++historyToken;
    historyError = "";
    historyLoading = true;
    transcript = [];
    dmChannelId = "";
    return getChatHistory(agentID, { userId })
      .then((result) => {
        if (token !== historyToken) return;
        const messages = result.messages ?? [];
        transcript = messages
          .slice()
          .reverse()
          .map(historyToEntry);
        // The history messages carry the canonical DM channel id; capture it for
        // the §F timeline deep-link. Empty history leaves it unset (no DM yet).
        if (messages.length > 0) {
          dmChannelId = messages[0].channel_id ?? "";
        }
      })
      .catch((err) => {
        if (token !== historyToken) return;
        historyError = `Could not load conversation history: ${err.message}`;
      })
      .finally(() => {
        if (token !== historyToken) return;
        historyLoading = false;
      });
  }

  // Reseed the transcript whenever the selected persona changes (including the
  // initial default selection): each persona is its own conversation, so the
  // transcript must follow the picker. The cleanup invalidates an in-flight seed
  // so a slow load can't write into a persona the operator already switched off.
  $effect(() => {
    const agent = selectedAgent;
    if (!agent) return;
    loadHistory(agent);
    return () => {
      historyToken++;
    };
  });

  // formatTimestamp renders the wire timestamp (RFC-3339 UTC) as a readable
  // local time, mirroring the channel timeline. An unparseable value falls back
  // to the raw string rather than rendering "Invalid Date".
  function formatTimestamp(ts) {
    const date = new Date(ts);
    return Number.isNaN(date.getTime()) ? ts : date.toLocaleString();
  }

  // loadToken disambiguates concurrent/superseded loads: each call stamps a
  // token and only the latest may write state. This both guards against a
  // resolve-after-unmount (the effect cleanup bumps the token) and makes Retry
  // safe — a slow first load can never clobber the result of a later retry.
  let loadToken = 0;

  // loadAgents fetches the persona list and is re-runnable: the mount effect
  // calls it once, and the Retry control calls it again after a load failure.
  function loadAgents() {
    const token = ++loadToken;
    agentsError = "";
    agentsLoaded = false;
    return listAgents()
      .then((list) => {
        if (token !== loadToken) return;
        agents = list;
        // Default the picker to the first *healthy* persona so a newcomer can
        // send immediately and the default landing isn't a guaranteed 503 (the
        // chat route only answers for a healthy persona; an offline one is a dead
        // end). Fall back to the first entry when none are healthy — there's
        // nothing more sendable to offer, and the status annotation already warns
        // why a send may fail.
        if (list.length > 0) {
          const healthy = list.find((agent) => agent.status === "healthy");
          selectedAgent = (healthy ?? list[0]).id;
        }
      })
      .catch((err) => {
        if (token !== loadToken) return;
        agentsError = `Could not load personas: ${err.message}`;
      })
      .finally(() => {
        if (token !== loadToken) return;
        agentsLoaded = true;
      });
  }

  $effect(() => {
    loadAgents();
    return () => {
      // Invalidate any in-flight load so its resolution can't write to an
      // unmounted component.
      loadToken++;
    };
  });

  // agentLabel is the picker's display text: the persona's name, falling back to
  // its id when unnamed (matching the server's own display-name fallback in
  // chat_handler.go). A non-healthy persona is annotated with its status, since
  // only a healthy one can actually reply (the chat route 503s otherwise) — the
  // operator sees that before spending a send, not after.
  function agentLabel(agent) {
    const name = agent.name ? agent.name : agent.id;
    // Fold the role into the option so the picker reads as a cast of personas
    // ("Ada — Researcher") rather than a list of bare names (RFC 0048 §A). Role
    // is optional; omit the separator when unset.
    const named = agent.role ? `${name} — ${agent.role}` : name;
    return agent.status && agent.status !== "healthy"
      ? `${named} (${agent.status})`
      : named;
  }

  async function send() {
    // Guard re-entrancy: the Send button is disabled while a reply is in flight,
    // but pressing Enter in a single-line override input still submits the form
    // (canSend gates only the button, not send()). A second concurrent turn
    // collides on the server's replyWaiter (409) and wastes a round-trip, so
    // drop it here.
    if (sending) {
      return;
    }
    sendError = "";
    const text = message.trim();
    if (!selectedAgent || text.length === 0) {
      return;
    }
    if ([...text].length > MAX_MESSAGE_LENGTH) {
      sendError = `Message exceeds the maximum length of ${MAX_MESSAGE_LENGTH} characters.`;
      return;
    }

    sending = true;
    try {
      const payload = { message: text, userId };
      // Pass the optional isolation overrides through only when set, so an empty
      // selector leaves the orchestrator's boot defaults intact (the client
      // omits absent keys; see api.js sendChat).
      const usedSession = sessionId.trim();
      const usedEpoch = epochId.trim();
      if (usedSession) {
        payload.sessionId = usedSession;
      }
      if (usedEpoch) {
        payload.epochId = usedEpoch;
      }
      const response = await sendChat(selectedAgent, payload);
      // A live turn appends two flat messages — the operator's prompt then the
      // persona's reply — keeping the conversational oldest-top order. The user
      // message carries the scope it was actually sent under (the overrides stay
      // editable between turns, so pinning the scope per message keeps the
      // isolation story visible — RFC 0031 / ISSUE-0085). The reply timestamp
      // comes from the server when present, else the local clock.
      const now = new Date().toISOString();
      transcript = [
        ...transcript,
        {
          id: `local-${nextLocalId++}`,
          fromUser: true,
          who: "You",
          content: text,
          timestamp: now,
          session: usedSession,
          epoch: usedEpoch,
        },
        {
          id: `local-${nextLocalId++}`,
          fromUser: false,
          who: response.agent_display_name || personaName,
          content: response.reply,
          status: response.reply_status,
          timestamp: response.timestamp
            ? new Date(response.timestamp * 1000).toISOString()
            : now,
        },
      ];
      message = "";
    } catch (err) {
      // The client surfaces the server's `{error, code}` envelope as the
      // ApiError message (api.js), so showing err.message gives the operator the
      // backend's own wording (e.g. the over-length rejection). A non-ApiError
      // still degrades to its message rather than crashing the panel.
      sendError =
        err instanceof ApiError
          ? err.message
          : `The message could not be sent: ${err.message}`;
    } finally {
      sending = false;
    }
  }

  function onSubmit(event) {
    event.preventDefault();
    send();
  }

  // onPersonaChange clears a lingering send error when the operator switches
  // persona: the error refers to the attempt that just failed, and a new
  // selection is a fresh intent, so leaving the stale alert over the new picker
  // value reads as if the new persona is already broken. The transcript and the
  // in-progress message are left intact — only the transient error is dropped.
  // Scoped to the user-driven change event so the programmatic default-selection
  // during load doesn't clear an unrelated error.
  function onPersonaChange() {
    sendError = "";
  }
</script>

<section class="panel chat" aria-label="Chat">
  <h2>Chat</h2>
  <p class="identity">Acting as <code>{userId}</code></p>

  {#if agentsError}
    <p class="boot error" role="alert">{agentsError}</p>
    <button type="button" class="retry" onclick={loadAgents}>Retry</button>
  {:else if !agentsLoaded}
    <!-- Until the first load settles, show a loading line rather than falling
         through to the composer beside an empty picker (a flash of a blank
         dropdown). agentsLoaded gates both this and the empty state below. -->
    <p class="loading" role="status">Loading personas…</p>
  {:else if agents.length === 0}
    <p class="empty">No personas are registered yet.</p>
  {:else}
    {#if selectedAgentInfo}
      <!-- Persona header: gives the conversation a face. Name + role identify
           the persona; the capability chips say what it's for — all from fields
           the agent DTO already carries (RFC 0048 amendment §A). -->
      <header class="persona">
        <span class="persona-name"
          >{selectedAgentInfo.name || selectedAgentInfo.id}</span
        >
        {#if selectedAgentInfo.role}
          <span class="persona-role">{selectedAgentInfo.role}</span>
        {/if}
        {#if selectedAgentInfo.capabilities && selectedAgentInfo.capabilities.length > 0}
          <ul class="persona-caps" aria-label="Capabilities">
            <!-- Unkeyed: capabilities are display-only and the registry doesn't
                 dedupe them, so a value key would throw each_key_duplicate. The
                 list is re-derived wholesale per selection, so there's no identity
                 to preserve across mutations anyway. -->
            {#each selectedAgentInfo.capabilities as capability}
              <li>{capability}</li>
            {/each}
          </ul>
        {/if}
      </header>
    {/if}

    {#if historyLoading}
      <p class="loading" role="status">Loading conversation history…</p>
    {/if}
    {#if historyError}
      <!-- Non-fatal: a failed history seed still leaves a usable (empty)
           transcript, so the operator can start a fresh conversation rather than
           being blocked. Muted, not an alarming alert. -->
      <p class="poll-error" role="status">{historyError}</p>
    {/if}

    <ol class="transcript" aria-label="Conversation">
      {#each transcript as msg (msg.id)}
        <li class="msg" class:from-self={msg.fromUser}>
          <p
            class:from-user={msg.fromUser}
            class:from-agent={!msg.fromUser}
            class:reply-error={msg.status === "error"}
          >
            <strong>{msg.who}:</strong>
            {#if !msg.fromUser && msg.status === "empty"}
              <!-- reply_status:"empty" is a valid message (the agent had nothing
                   to say, chat_handler.go) — show a placeholder so it doesn't
                   read as a blank/broken line. -->
              <em class="empty-reply">(no reply)</em>
            {:else}
              {msg.content}
            {/if}
          </p>
          <p class="msg-meta">
            {#if msg.timestamp}
              <time datetime={msg.timestamp}>{formatTimestamp(msg.timestamp)}</time>
            {/if}
            <!-- Per-message isolation scope (RFC 0031 session / ISSUE-0085
                 epoch). Only live turns carry it; seeded history shows no scope
                 line (amendment §B caveat 2). -->
            {#if msg.session}<span>session: {msg.session}</span>{/if}
            {#if msg.epoch}<span>epoch: {msg.epoch}</span>{/if}
          </p>
        </li>
      {/each}
    </ol>

    {#if sending}
      <p class="thinking" role="status">Waiting for a reply…</p>
    {/if}

    {#if sendError}
      <p class="boot error" role="alert">{sendError}</p>
    {/if}

    <form class="composer" onsubmit={onSubmit}>
      <!-- The whole composer locks while a turn is in flight (`sending`): the
           chat call is synchronous, so leaving it editable lets the operator
           keep typing into a message that the post-send reset then wipes, or
           switch persona out from under the pending reply. Disabling for the
           round-trip keeps the in-flight text and pins the turn to its persona;
           the "Waiting for a reply…" status says why. -->
      <label>
        Persona
        <select
          bind:value={selectedAgent}
          onchange={onPersonaChange}
          disabled={sending}
        >
          {#each agents as agent (agent.id)}
            <option value={agent.id}>{agentLabel(agent)}</option>
          {/each}
        </select>
      </label>

      <label>
        Message
        <textarea
          bind:value={message}
          rows="3"
          placeholder="Say something to the persona…"
          disabled={sending}
        ></textarea>
      </label>

      <!-- Optional isolation overrides (RFC 0031 session / ISSUE-0085 epoch).
           Free-text by design: these are operator-namespace ids, not identity —
           the §F identity rule constrains user_id only, which is never typed. -->
      <details class="overrides">
        <summary>Scope (optional)</summary>
        <label>
          Session ID
          <input
            type="text"
            bind:value={sessionId}
            autocomplete="off"
            disabled={sending}
          />
        </label>
        <label>
          Epoch ID
          <input
            type="text"
            bind:value={epochId}
            autocomplete="off"
            disabled={sending}
          />
        </label>
      </details>

      <button type="submit" disabled={!canSend}>
        {sending ? "Sending…" : "Send"}
      </button>
    </form>
  {/if}
</section>
