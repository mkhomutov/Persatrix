<script>
  // Persona switcher + Exit (extracted to keep the conversation panel under the
  // review-size cap, mirroring PublishComposer). Pinned at the top of the panel
  // (like the channel timeline's selector-at-top layout) so it stays reachable
  // above a growing transcript rather than buried in the composer.
  //
  // selectedAgent — bound to the panel's selection ("" = no conversation open).
  // agents — the persona list to offer.
  // sending — true while a turn is in flight; locks the controls so a switch or
  //   exit can't strand the pending reply.
  // onChange — user-driven persona change (records the sticky selection upstream).
  // onExit — leave the conversation (clears the selection back to the empty state).
  import { isChattable, agentLabel } from "../lib/agents.js";

  let {
    selectedAgent = $bindable(),
    agents,
    sending,
    onChange,
    onExit,
  } = $props();
</script>

<div class="persona-picker">
  <label>
    Persona
    <select bind:value={selectedAgent} onchange={onChange} disabled={sending}>
      {#if !selectedAgent}
        <!-- Lobby placeholder: no conversation is open (a fresh start or after
             Exit). Disabled so it isn't a re-pickable value; it just labels the
             empty selection until the operator chooses a persona. -->
        <option value="" disabled>Select a persona…</option>
      {/if}
      {#each agents as agent (agent.id)}
        <option value={agent.id} disabled={!isChattable(agent)}
          >{agentLabel(agent)}</option
        >
      {/each}
    </select>
  </label>
  {#if selectedAgent}
    <!-- Exit leaves the conversation for the persona lobby — the web analogue of
         quitting the CLI chat REPL. Locked during a turn so it can't strand an
         in-flight reply. -->
    <button type="button" class="exit-chat" onclick={onExit} disabled={sending}
      >Exit</button
    >
  {/if}
</div>
