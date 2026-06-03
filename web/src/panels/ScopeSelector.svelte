<script>
  // Optional isolation-scope selector (RFC 0048 amendment §C) — extracted from
  // the Chat panel so the panel stays under the review-size cap. Surfaces the
  // chat API's optional session_id / epoch_id overrides (RFC 0031 / ISSUE-0085):
  // session is a dropdown over the labeled sessions API (with an inline "create
  // session" affordance), and epoch stays free-text (no labeled-epoch list
  // exists). The §F identity rule constrains user_id only (never typed here);
  // session and epoch are operator-namespace ids.
  import { listSessions, createSession, ApiError } from "../lib/api.js";

  // sessionId / epochId are bindable so the parent composer reads the scope each
  // turn is sent under; sending locks the controls while a turn is in flight.
  let {
    sessionId = $bindable(""),
    epochId = $bindable(""),
    sending = false,
  } = $props();

  // sessions is the labeled list from /api/v1/sessions; sessionsAvailable flips
  // false when the registry is unwired (503), and the control degrades to the
  // free-text input bound to sessionId so the isolation override stays reachable.
  let sessions = $state([]);
  let sessionsAvailable = $state(false);
  let newSessionLabel = $state("");
  let creatingSession = $state(false);
  let sessionCreateError = $state("");

  // loadSessions populates the session dropdown. A failure (notably 503 when the
  // session registry is unwired) leaves sessionsAvailable false, so the control
  // degrades to the free-text input rather than disappearing.
  function loadSessions() {
    return listSessions()
      .then((result) => {
        sessions = (result.sessions ?? []).filter((s) => !s.archived);
        sessionsAvailable = true;
      })
      .catch(() => {
        sessionsAvailable = false;
      });
  }

  $effect(() => {
    loadSessions();
  });

  // createNewSession mints a labeled session and selects it, so a tester can
  // scope a conversation without leaving the browser for the CLI. The label is
  // required server-side; an empty one is a no-op here.
  async function createNewSession() {
    const label = newSessionLabel.trim();
    if (!label || creatingSession) {
      return;
    }
    sessionCreateError = "";
    creatingSession = true;
    try {
      const created = await createSession(label);
      sessions = [created, ...sessions];
      sessionId = created.id;
      newSessionLabel = "";
    } catch (err) {
      sessionCreateError =
        err instanceof ApiError
          ? err.message
          : `Could not create session: ${err.message}`;
    } finally {
      creatingSession = false;
    }
  }

  // sessionOptionLabel shows the human label, falling back to the id for a
  // not-yet-named (auto-minted) session.
  function sessionOptionLabel(session) {
    return session.label ? session.label : session.id;
  }
</script>

<details class="overrides">
  <summary>Scope (optional)</summary>
  {#if sessionsAvailable}
    <label>
      Session
      <select bind:value={sessionId} disabled={sending}>
        <!-- The empty value rides the orchestrator's boot-default session
             (the override is omitted on the wire when blank). -->
        <option value="">(default session)</option>
        {#each sessions as session (session.id)}
          <option value={session.id}>{sessionOptionLabel(session)}</option>
        {/each}
      </select>
    </label>
    <div class="new-session">
      <label>
        New session
        <input
          type="text"
          bind:value={newSessionLabel}
          placeholder="label…"
          autocomplete="off"
          disabled={sending || creatingSession}
        />
      </label>
      <button
        type="button"
        onclick={createNewSession}
        disabled={sending || creatingSession || !newSessionLabel.trim()}
      >
        {creatingSession ? "Creating…" : "Create"}
      </button>
    </div>
    {#if sessionCreateError}
      <p class="poll-error" role="status">{sessionCreateError}</p>
    {/if}
  {:else}
    <!-- Session registry unwired (503) — degrade to free-text id entry so
         the override stays reachable (§C). -->
    <label>
      Session ID
      <input
        type="text"
        bind:value={sessionId}
        autocomplete="off"
        disabled={sending}
      />
    </label>
  {/if}
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
