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
  let errorMessage = $state("");
  // Build version from /ui/config (build.version) — surfaced in the topbar so an
  // operator can tell at a glance which orchestrator build they're driving (RFC
  // 0048 amendment §D). Empty when the payload omits it; the topbar then shows
  // no version chip rather than a placeholder.
  let version = $state("");
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

  // Canonicalise the URL after a deep link that fell back. A non-empty hash that
  // doesn't name a rendered panel (a stale link, or a known panel that isn't
  // available in this deployment, e.g. #/memory) resolves to the first panel via
  // hashPanelName; rewrite the hash to that panel's route so the address bar
  // matches the tab actually shown. replaceState (not push) keeps it a silent
  // correction, and the guard leaves a bare /ui/ (empty hash) untouched so a
  // clean load isn't forced to #/chat.
  function canonicalizeHash(name) {
    const panel = panels.find((p) => p.name === name);
    if (panel && window.location.hash && window.location.hash !== panel.route) {
      window.history.replaceState(null, "", panel.route);
    }
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
        // Resolve identity first: an empty principal is a boot-error, so bail
        // before mutating shell state (panels/activeName) that the error branch
        // doesn't render — no half-applied state in the failure path.
        const id = deriveUserId(context);
        if (!id) {
          status = "error";
          errorMessage =
            "The console could not determine an identity (no principal in /ui/context).";
          return;
        }
        userId = id;
        version = config?.build?.version ?? "";
        panels = selectPanels(config);
        activeName = hashPanelName();
        canonicalizeHash(activeName);
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

  // Back/forward and any external hash navigation re-resolve the active tab.
  // A hash that doesn't name a rendered panel (manual address-bar edit, or a
  // known-but-unavailable panel like #/memory) falls back to the first panel
  // via hashPanelName — so canonicalise here too, mirroring the initial-load
  // correction, rather than leaving the URL dangling a route that resolves to a
  // different tab than the one shown. replaceState fires no hashchange, so this
  // can't re-enter; a valid route is a no-op (the guard in canonicalizeHash).
  $effect(() => {
    const onHashChange = () => {
      activeName = hashPanelName();
      canonicalizeHash(activeName);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  });

  // Click navigation pushes a hash history entry — a deliberate, deep-linkable
  // move. Keyboard navigation under automatic activation fires on every arrow
  // keystroke, so it *replaces* instead: otherwise arrowing across the tabs
  // would bury the previous page under one history entry per tab, and Back would
  // walk the tabs rather than leave the console.
  function selectTab(panel, { replace = false } = {}) {
    if (replace) {
      window.history.replaceState(null, "", panel.route);
    } else {
      window.location.hash = panel.route;
    }
    activeName = panel.name;
  }

  // ARIA APG tabs keyboard interaction. The role=tab markup advertises a
  // keyboard contract, so the tablist must honour it: Left/Right move between
  // tabs (wrapping), Home/End jump to the ends, and — with automatic activation
  // (cheap here, panels are local) — moving focus also selects. Focus is moved
  // imperatively to the target tab so it pairs with the roving tabindex in the
  // markup (only the active tab is in the Tab sequence).
  function onTabKeydown(event) {
    const last = panels.length - 1;
    const current = panels.findIndex((p) => p.name === activeName);
    let next;
    switch (event.key) {
      case "ArrowRight":
        next = current >= last ? 0 : current + 1;
        break;
      case "ArrowLeft":
        next = current <= 0 ? last : current - 1;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = last;
        break;
      default:
        return;
    }
    event.preventDefault();
    selectTab(panels[next], { replace: true });
    const tablist = event.currentTarget.closest('[role="tablist"]');
    tablist?.querySelectorAll('[role="tab"]')[next]?.focus();
  }
</script>

<header class="topbar">
  <span class="brand">
    Persatrix console
    {#if version}<span class="version" title="Orchestrator build">v{version}</span>{/if}
  </span>
  {#if userId}
    <span class="principal" title="Identity from /api/v1/ui/context">
      {userId}
    </span>
  {/if}
</header>

<!-- Boot states wrap their copy in <main> for the same reason the empty-state
     and ready branches below do: every shell branch keeps its content inside a
     landmark region, so nothing renders orphaned outside one. The error keeps
     role=alert so it is still announced. -->
{#if status === "loading"}
  <main class="content">
    <p class="boot">Loading the console…</p>
  </main>
{:else if status === "error"}
  <main class="content">
    <p class="boot error" role="alert">{errorMessage}</p>
  </main>
{:else if panels.length === 0}
  <!-- Reachable backend with a valid principal but no enabled && available
       panel. Render the empty-state copy on its own — an empty role=tablist (a
       tablist with no tabs) and a tabpanel labelled by a tab that doesn't exist
       are both invalid ARIA, so the tab scaffolding is omitted entirely. -->
  <main class="content">
    <p class="boot">No panels are enabled for this deployment.</p>
  </main>
{:else}
  <div class="tabs" role="tablist" aria-label="Console panels">
    {#each panels as panel (panel.name)}
      <button
        type="button"
        role="tab"
        id="tab-{panel.name}"
        aria-controls={panel.name === activeName
          ? `panel-${panel.name}`
          : undefined}
        aria-selected={panel.name === activeName}
        tabindex={panel.name === activeName ? 0 : -1}
        onclick={() => selectTab(panel)}
        onkeydown={onTabKeydown}
      >
        {panel.title}
      </button>
    {/each}
  </div>

  <!-- The content region is the tabpanel for whichever tab is active;
       id/aria-labelledby track activeName so the tab↔panel relationship is
       complete for assistive tech. Only the active panel is mounted (its panel
       may poll, so mounting inactive panels would start background work for tabs
       the operator isn't viewing); aria-controls is therefore set only on the
       active tab — the one whose panel is actually in the DOM — so inactive tabs
       don't dangle a reference to a missing element.
       The role lives on a generic <div> rather than <main> so a non-interactive
       landmark isn't given an interactive role; tabindex makes the panel
       keyboard-reachable even when its content has no focusable element (ARIA
       APG tabs pattern). -->
  <main class="content">
    <div
      role="tabpanel"
      id="panel-{activeName}"
      aria-labelledby="tab-{activeName}"
      tabindex="0"
    >
      {#if ActiveComponent}
        <ActiveComponent {userId} />
      {:else}
        <p class="boot">This panel isn’t available in this build.</p>
      {/if}
    </div>
  </main>
{/if}
