<script>
  import { loadBootstrap } from "./lib/api.js";
  import { selectPanels, deriveUserId } from "./lib/bootstrap.js";
  import ChannelTimeline from "./panels/ChannelTimeline.svelte";

  // Known panel name → its Svelte component. selectPanels already filters to
  // panels the client knows and the server reports enabled && available, so an
  // entry here exists for every panel that can reach this map. Slice 1 ships only
  // channel_timeline (the consolidated conversation panel, RFC 0048
  // chat-panel-retirement amendment); memory_strip / cost have no component yet
  // and can never be `available`, so they are filtered out upstream and
  // intentionally absent here.
  const COMPONENTS = {
    channel_timeline: ChannelTimeline,
  };

  // Boot state machine: loading → (ready | error). The shell cannot render any
  // panel without both /ui/config and /ui/context, so a failure in either is a
  // single boot-error state rather than a half-configured console.
  let status = $state("loading");
  let panels = $state([]);
  let errorMessage = $state("");
  // Identity (RFC 0048 §F + amendment §E). `principal` is the real /ui/context
  // identity, shown verbatim and never overwritten. `authenticated` gates the
  // §E carve-out. `actingAs` is the local tester override — it DEFAULTS to the
  // principal and is layered on top of it (a single identity source, offset by
  // an operator-visible value), so a tester can demonstrate per-user
  // persistence ("greet me; now not-recognise a different user") without
  // leaving the browser. The effective `userId` the panels act as is the
  // override in local mode and the real principal once authenticated — at which
  // point the override control disappears and can never mask a real principal.
  let principal = $state(null);
  let authenticated = $state(false);
  // `actingAs` is the *committed* override that drives the effective identity;
  // `actingAsDraft` is what's in the box. The draft commits to `actingAs` only
  // on `change` (blur/Enter), never per-keystroke — persistence is keyed on
  // (user, agent) and the panels reseed history when the identity changes, so a
  // per-keystroke commit would blank+refetch the transcript for every
  // intermediate value ("b", "bo", "bob"). Deferring the commit reseeds once,
  // on the value the tester actually meant.
  let actingAs = $state("");
  let actingAsDraft = $state("");
  const userId = $derived(
    principal == null
      ? null
      : authenticated
        ? principal
        : actingAs.trim() || principal,
  );
  // Build version from /ui/config (build.version) — surfaced in the topbar so an
  // operator can tell at a glance which orchestrator build they're driving (RFC
  // 0048 amendment §D). Empty when the payload omits it; the topbar then shows
  // no version chip rather than a placeholder.
  let version = $state("");
  let activeName = $state(hashPanelName());

  // The active panel is chosen by the hash route (e.g. #/channels) so a deep
  // link / reload lands on the right panel; falls back to the first rendered
  // panel. Hash-mode keeps the static file server a plain http.FileServer with
  // no SPA-fallback shim (PR plan D1).
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
  // clean load isn't forced to the first panel's route.
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
  // The structural-write (create) affordance renders only when the server reports
  // the active panel's create capability both enabled (operator opt-in) and
  // available (subsystem wired) — the same enabled && available gate panels use
  // (RFC 0048 channel-creation amendment §A). Panels without a create capability
  // (e.g. a future memory_strip) get false and ignore the prop.
  const canCreate = $derived(
    Boolean(activePanel?.create?.enabled && activePanel?.create?.available),
  );
  // The channel settings affordance renders only when the server reports the
  // active panel's config_edit capability both enabled (operator opt-in, the
  // default-off config_edit_enabled toggle) and available (store + router wired)
  // — the same enabled && available gate as `create` (RFC 0050 Phase 2). Panels
  // without a config_edit capability get false and ignore the prop.
  const canConfigEdit = $derived(
    Boolean(
      activePanel?.config_edit?.enabled && activePanel?.config_edit?.available,
    ),
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
        principal = id;
        authenticated = context?.authenticated === true;
        actingAs = id; // the override defaults to the real principal
        actingAsDraft = id; // and the box shows that default
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
    <!-- Three-node mark: a nod to the multi-persona orchestration the console
         fronts. Decorative only (aria-hidden); the brand text carries the name. -->
    <svg class="logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="5.5" r="2.6" fill="currentColor" />
      <circle cx="5.5" cy="17.5" r="2.6" fill="currentColor" />
      <circle cx="18.5" cy="17.5" r="2.6" fill="currentColor" />
      <path
        d="M12 8.1 6.7 15.3M12 8.1l5.3 7.2M8.1 17.5h7.8"
        stroke="currentColor"
        stroke-width="1.4"
        stroke-linecap="round"
      />
    </svg>
    Persatrix <span class="console-word">console</span>
    {#if version}<span class="version" title="Orchestrator build">v{version}</span>{/if}
  </span>
  {#if status === "ready" && panels.length > 0}
    <!-- The panel tabs live in the topbar as a segmented nav — same ARIA tabs
         contract as before (roving tabindex, arrow-key movement), restyled. -->
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
  {/if}
  {#if principal}
    <!-- `identity-block` (not `identity`): the bare `.identity` class is the
         conversation panel's identity line, a global rule that would otherwise
         also match this wrapper and bleed that styling into the topbar. This
         wrapper is a *group* (principal + override), so it gets its own name. -->
    <span class="identity-block">
      <!-- The real /ui/context principal, shown verbatim — never the override.
           Titled so its source is unambiguous (RFC 0048 §F rule 1). -->
      <span class="principal" title="Identity from /api/v1/ui/context">
        {principal}
      </span>
      {#if !authenticated}
        <!-- §E tester identity override: a clearly-labelled LOCAL TESTING
             control, not identity. Defaults to the principal; lets a tester act
             as another user to make per-user persistence visible. Absent once
             authenticated (RFC 0039) so a real principal can never be masked
             from the browser. -->
        <label
          class="acting-as"
          title="Local testing only — defaults to the principal and is ignored once authenticated"
        >
          acting as
          <!-- Commit on `change` (blur/Enter), not per-keystroke, so the panels
               reseed once on the intended value. The placeholder echoes the
               principal so a cleared box reads as "acting as the principal"
               (the userId derivation falls back to it) rather than no identity. -->
          <input
            name="acting_as"
            bind:value={actingAsDraft}
            onchange={() => (actingAs = actingAsDraft)}
            placeholder={principal}
            autocomplete="off"
          />
        </label>
      {/if}
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
        <ActiveComponent {userId} {canCreate} {canConfigEdit} />
      {:else}
        <p class="boot">This panel isn’t available in this build.</p>
      {/if}
    </div>
  </main>
{/if}
