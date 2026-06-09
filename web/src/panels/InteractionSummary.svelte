<script>
  // The interaction-summary surface (v0.3.8, RFC 0020 §C/§D): when an
  // interaction in the active conversation closes — by end-vote, by the RFC 0030
  // Layer 1 cost ceiling, or by going idle — the persona persists a synthesised
  // summary, and this affordance surfaces it at the foot of the conversation
  // view so a converged brainstorm hands back a readable outcome, not just a
  // stop. Additive: with no closed interaction it renders nothing, so an open
  // conversation's live feed is untouched.
  //
  // The read API is per-agent (each participating persona persists its own
  // summary row), so the surface queries the channel's candidate agents for the
  // active scope and shows the single newest closed interaction. The
  // "[interaction summary unavailable]" sentinel is rendered as an explicit
  // unavailable state, never blanked (SS3).
  import { getClosedInteractions } from "../lib/api.js";
  import {
    pickLatestClosed,
    closeTriggerLabel,
    isSummaryUnavailable,
  } from "../lib/interactions.js";

  // scope — the RFC 0020 scope to filter on (the active channel id; for a group
  //   channel the scope IS the `group:` id, scopes.py `scope_for_group`).
  // agentIds — the channel's participating personas to query (a DM's peer, or a
  //   group's members), already free of the human principal.
  let { scope = "", agentIds = [] } = $props();

  // Re-poll cadence for a close that lands while the conversation is open. The
  // feed already head-polls messages; this is the parallel summary refresh,
  // paused while the tab is backgrounded.
  const REFRESH_MS = 5000;
  // Single newest closed interaction per agent — the surface only shows one.
  const PER_AGENT_LIMIT = 1;
  // Skip the degenerate single-turn rows (per-event tick/task envelopes) the
  // read API keeps retrievable; the conversation view wants real interactions.
  const MIN_TURNS = 2;

  let record = $state(null);
  let loadToken = 0;

  // refresh fetches each candidate agent's latest closed interaction for the
  // scope and keeps the newest. A token guards against a stale resolution
  // writing after scope/agentIds changed.
  //
  // `reset` distinguishes the two callers, which want opposite failure
  // behaviour:
  //   - reset (a scope / agent change): clear the record up front so the
  //     previous conversation's summary never lingers — no flash while the new
  //     reads are in flight, and nothing to wrongly hold if they fail. `next`
  //     (possibly null) is authoritative.
  //   - poll (the interval, same scope): an INCOMPLETE read (some agent failed)
  //     that yields no record must not flap the affordance away — the failing
  //     agent may be the very one holding the latest summary while a peer simply
  //     has none. Hold the current record in that case only.
  async function refresh({ reset = false } = {}) {
    const ids = (agentIds ?? []).filter(Boolean);
    if (!scope || ids.length === 0) {
      record = null;
      return;
    }
    if (reset) {
      record = null;
    }
    const token = ++loadToken;
    const settled = await Promise.allSettled(
      ids.map((id) =>
        getClosedInteractions(id, {
          scope,
          limit: PER_AGENT_LIMIT,
          minTurns: MIN_TURNS,
        }),
      ),
    );
    if (token !== loadToken) {
      return;
    }
    const fulfilled = settled.filter((r) => r.status === "fulfilled");
    const records = fulfilled.flatMap((r) => r.value?.interactions ?? []);
    const next = pickLatestClosed(records);
    // Same-scope poll with a partial failure and nothing to show: hold rather
    // than flap. A complete read (all agents answered) is always authoritative,
    // and a reset already cleared above.
    const incomplete = fulfilled.length < settled.length;
    if (!reset && next === null && incomplete) {
      return;
    }
    record = next;
  }

  // Re-fetch whenever the scope or the candidate agent set changes (a channel
  // switch). Reading both props here registers them as effect dependencies; the
  // async fetch itself is launched fire-and-forget.
  $effect(() => {
    void scope;
    void agentIds;
    refresh({ reset: true });
  });

  // Catch a close that happens while the conversation stays open. One interval
  // for the component lifetime; it reads the current props at fire time.
  $effect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const timer = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) {
        return;
      }
      refresh();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  });

  const unavailable = $derived(
    record ? isSummaryUnavailable(record.summary) : false,
  );
  const triggerLabel = $derived(
    record ? closeTriggerLabel(record.close_reason) : "",
  );
</script>

{#if record}
  <aside
    class="interaction-summary"
    role="status"
    aria-label="Interaction summary"
  >
    <header class="meta">
      <span class="badge">Conversation {triggerLabel}</span>
      {#if record.turn_count}
        <span class="turns">{record.turn_count} turns</span>
      {/if}
    </header>
    {#if unavailable}
      <p class="unavailable">Summary unavailable for this interaction.</p>
    {:else}
      <p class="summary">{record.summary}</p>
    {/if}
  </aside>
{/if}

<style>
  .interaction-summary {
    margin: 0.5rem 0 0;
    padding: 0.6rem 0.8rem;
    border: 1px solid var(--border, #d4d4d8);
    border-left: 3px solid var(--accent, #2563eb);
    border-radius: 6px;
    background: var(--surface-muted, #f4f4f5);
  }
  .meta {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    margin-bottom: 0.35rem;
  }
  .badge {
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--accent, #2563eb);
  }
  .turns {
    font-size: 0.75rem;
    color: var(--text-muted, #71717a);
  }
  .summary {
    margin: 0;
    white-space: pre-wrap;
  }
  .unavailable {
    margin: 0;
    font-style: italic;
    color: var(--text-muted, #71717a);
  }
</style>
