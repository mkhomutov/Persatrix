<script>
  import { loadBootstrap } from "./lib/api.js";
  import { selectPanels, deriveUserId } from "./lib/bootstrap.js";
  import Chat from "./panels/Chat.svelte";
  import ChannelTimeline from "./panels/ChannelTimeline.svelte";

  // Known panel name → its Svelte component. selectPanels already filters to
  // panels the client knows and the server reports enabled && available, so an
  // entry here exists for every panel that can reach this map. memory_strip /
  // cost have no component in Slice 1 and can never be `available`, so they are
  // filtered out upstream and intentionally absent here.
  const COMPONENTS = {
    chat: Chat,
    channel_timeline: ChannelTimeline,
  };

  // Boot state machine: loading → (ready | error). The shell cannot render any
  // panel without both /ui/config and /ui/context, so a failure in either is a
  // single boot-error state rather than a half-configured console.
  let status = $state("loading");
  let panels = $state([]);
  let userId = $state(null);
  let principal = $state(null);
  let errorMessage = $state("");
  let activeName = $state(hashPanelName());

  // The active panel is chosen by the hash route (#/chat, #/channels) so a
  // deep link / reload lands on the right panel; falls back to the first
  // rendered panel. Hash-mode keeps the static file server a plain
  // http.FileServer with no SPA-fallback shim (PR plan D1).
  function hashPanelName() {
    const route = window.location.hash;
    const match = panels.find((p) => p.route === route);
    return match ? match.name : panels[0]?.name ?? null;
  }

  const activePanel = $derived(panels.find((p) => p.name === activeName));
  const ActiveComponent = $derived(
    activePanel ? COMPONENTS[activePanel.name] : null,
  );

  $effect(() => {
    let cancelled = false;
    loadBootstrap()
      .then(({ config, context }) => {
        if (cancelled) return;
        panels = selectPanels(config);
        userId = deriveUserId(context);
        principal = context?.principal ?? null;
        if (!userId) {
          status = "error";
          errorMessage =
            "The console could not determine an identity (no principal in /ui/context).";
          return;
        }
        activeName = hashPanelName();
        status = "ready";
      })
      .catch((err) => {
        if (cancelled) return;
        status = "error";
        errorMessage = `The console could not reach its backend: ${err.message}`;
      });
    return () => {
      cancelled = true;
    };
  });

  $effect(() => {
    const onHashChange = () => {
      activeName = hashPanelName();
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  });

  function selectTab(panel) {
    window.location.hash = panel.route;
    activeName = panel.name;
  }
</script>

<header class="topbar">
  <span class="brand">Persatrix console</span>
  {#if principal}
    <span class="principal" title="Identity from /api/v1/ui/context">
      {principal}
    </span>
  {/if}
</header>

{#if status === "loading"}
  <p class="boot">Loading the console…</p>
{:else if status === "error"}
  <p class="boot error" role="alert">{errorMessage}</p>
{:else}
  <div class="tabs" role="tablist" aria-label="Console panels">
    {#each panels as panel (panel.name)}
      <button
        type="button"
        role="tab"
        aria-selected={panel.name === activeName}
        onclick={() => selectTab(panel)}
      >
        {panel.title}
      </button>
    {/each}
  </div>

  <main class="content">
    {#if ActiveComponent}
      <ActiveComponent {userId} />
    {:else}
      <p class="boot">No panels are enabled for this deployment.</p>
    {/if}
  </main>
{/if}
