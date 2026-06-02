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
  // authority.
  const MAX_MESSAGE_LENGTH = 4000;

  let agents = $state([]);
  let agentsError = $state("");
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

  $effect(() => {
    let cancelled = false;
    listAgents()
      .then((list) => {
        if (cancelled) return;
        agents = list;
        // Default the picker to the first persona so a newcomer can send
        // immediately without first opening the dropdown.
        if (list.length > 0) {
          selectedAgent = list[0].id;
        }
      })
      .catch((err) => {
        if (cancelled) return;
        agentsError = `Could not load personas: ${err.message}`;
      });
    return () => {
      cancelled = true;
    };
  });

  // agentLabel is the picker's display text: the persona's name, falling back to
  // its id when unnamed — matching the server's own display-name fallback in
  // chat_handler.go.
  function agentLabel(agent) {
    return agent.name ? agent.name : agent.id;
  }

  async function send() {
    sendError = "";
    const text = message.trim();
    if (!selectedAgent || text.length === 0) {
      return;
    }
    if (text.length > MAX_MESSAGE_LENGTH) {
      sendError = `Message exceeds the maximum length of ${MAX_MESSAGE_LENGTH} characters.`;
      return;
    }

    sending = true;
    try {
      const payload = { message: text, userId };
      // Pass the optional isolation overrides through only when set, so an empty
      // selector leaves the orchestrator's boot defaults intact (the client
      // omits absent keys; see api.js sendChat).
      if (sessionId.trim()) {
        payload.sessionId = sessionId.trim();
      }
      if (epochId.trim()) {
        payload.epochId = epochId.trim();
      }
      const response = await sendChat(selectedAgent, payload);
      transcript = [
        ...transcript,
        {
          prompt: text,
          reply: response.reply,
          agent: response.agent_display_name || selectedAgent,
          status: response.reply_status,
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
</script>

<section class="panel chat" aria-label="Chat">
  <h2>Chat</h2>
  <p class="identity">Acting as <code>{userId}</code></p>

  {#if agentsError}
    <p class="boot error" role="alert">{agentsError}</p>
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
            {turn.reply}
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
      <label>
        Persona
        <select bind:value={selectedAgent}>
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
        ></textarea>
      </label>

      <!-- Optional isolation overrides (RFC 0031 session / ISSUE-0085 epoch).
           Free-text by design: these are operator-namespace ids, not identity —
           the §F identity rule constrains user_id only, which is never typed. -->
      <details class="overrides">
        <summary>Scope (optional)</summary>
        <label>
          Session ID
          <input type="text" bind:value={sessionId} autocomplete="off" />
        </label>
        <label>
          Epoch ID
          <input type="text" bind:value={epochId} autocomplete="off" />
        </label>
      </details>

      <button type="submit" disabled={!canSend}>
        {sending ? "Sending…" : "Send"}
      </button>
    </form>
  {/if}
</section>
