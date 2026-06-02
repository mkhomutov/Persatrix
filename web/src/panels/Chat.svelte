<script>
  // Chat panel (RFC 0048 Phase 1 PR 4) — the "feel the taste" moment: pick a
  // persona and talk to it over today's synchronous chat API. No backend change;
  // pure render-over-existing-API. The shell threads in the /ui/context-derived
  // userId (RFC §F single identity source), so the panel never prompts for or
  // hard-codes a user.
  import { listAgents, sendChat, ApiError } from "../lib/api.js";

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
  // The transcript is the panel's local view of the conversation: each turn is
  // the human prompt and the agent's reply. Slice 1 keeps it in-memory (no
  // history fetch) — a reload starts a fresh transcript, which is the expected
  // shape for a synchronous chat panel.
  let transcript = $state([]);

  const canSend = $derived(
    Boolean(selectedAgent) && message.trim().length > 0 && !sending,
  );

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
    return agent.status && agent.status !== "healthy"
      ? `${name} (${agent.status})`
      : name;
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
      transcript = [
        ...transcript,
        {
          prompt: text,
          reply: response.reply,
          agent: response.agent_display_name || selectedAgent,
          status: response.reply_status,
          // Record the scope this turn was actually sent under. The overrides
          // stay editable between turns, so turns under different session/epoch
          // scopes can interleave in one transcript; pinning the scope per turn
          // keeps the isolation story (RFC 0031 / ISSUE-0085) visible rather
          // than silent. Empty when the turn rode the orchestrator's defaults.
          session: usedSession,
          epoch: usedEpoch,
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
    <ol class="transcript" aria-label="Conversation">
      {#each transcript as turn, i (i)}
        <li class="turn">
          <p class="from-user"><strong>You:</strong> {turn.prompt}</p>
          <p
            class="from-agent"
            class:reply-error={turn.status === "error"}
          >
            <strong>{turn.agent}:</strong>
            {#if turn.status === "empty"}
              <!-- reply_status:"empty" is a valid turn (the agent had nothing to
                   say, chat_handler.go) — show a placeholder so it doesn't read
                   as a blank/broken line. -->
              <em class="empty-reply">(no reply)</em>
            {:else}
              {turn.reply}
            {/if}
          </p>
          {#if turn.session || turn.epoch}
            <!-- Annotate the isolation scope this turn rode (RFC 0031 session /
                 ISSUE-0085 epoch). Only shown when an override was set; a
                 default-scope turn carries no annotation. -->
            <p class="turn-scope">
              {#if turn.session}<span>session: {turn.session}</span>{/if}
              {#if turn.epoch}<span>epoch: {turn.epoch}</span>{/if}
            </p>
          {/if}
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
