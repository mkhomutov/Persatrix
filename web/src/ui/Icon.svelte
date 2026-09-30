<script>
  // The console's icon set: small stroke icons on a 24-unit grid, drawn in
  // currentColor so they follow the text colour and theme. Always decorative
  // (aria-hidden) — the control or text beside an icon carries its meaning.
  //
  // name — a key of PATHS; size — rendered size in px.
  let { name, size = 16 } = $props();

  // Each entry is a list of SVG elements as [tag, attributes].
  const PATHS = {
    hash: [["path", { d: "M5 9h14M4 15h14M10 3 8 21M16 3l-2 18" }]],
    plus: [["path", { d: "M12 5v14M5 12h14" }]],
    refresh: [
      ["path", { d: "M20 11a8 8 0 1 0-2.3 5.7" }],
      ["path", { d: "M20 4v7h-7" }],
    ],
    search: [
      ["circle", { cx: 11, cy: 11, r: 6.5 }],
      ["path", { d: "m20 20-4.2-4.2" }],
    ],
    x: [["path", { d: "M18 6 6 18M6 6l12 12" }]],
    "chevron-down": [["path", { d: "m6 9 6 6 6-6" }]],
    "chevron-right": [["path", { d: "m9 6 6 6-6 6" }]],
    "arrow-down": [["path", { d: "M12 5v14M18 13l-6 6-6-6" }]],
    "arrow-up": [["path", { d: "M12 19V5M6 11l6-6 6 6" }]],
    send: [["path", { d: "M4 12 20 4l-6 16-3-7-7-1Z" }], ["path", { d: "m11 13 9-9" }]],
    sun: [
      ["circle", { cx: 12, cy: 12, r: 4 }],
      [
        "path",
        {
          d: "M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4",
        },
      ],
    ],
    moon: [["path", { d: "M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z" }]],
    monitor: [
      ["rect", { x: 3, y: 4, width: 18, height: 12, rx: 2 }],
      ["path", { d: "M9 20h6M12 16v4" }],
    ],
    menu: [["path", { d: "M4 7h16M4 12h16M4 17h16" }]],
    "panel-right": [
      ["rect", { x: 3, y: 4, width: 18, height: 16, rx: 2 }],
      ["path", { d: "M15 4v16" }],
    ],
    users: [
      ["circle", { cx: 9, cy: 8, r: 3.5 }],
      ["path", { d: "M2.5 20a6.5 6.5 0 0 1 13 0" }],
      ["path", { d: "M16 4.5a3.5 3.5 0 0 1 0 7M18 14.5a6.5 6.5 0 0 1 3.5 5.5" }],
    ],
    sliders: [
      ["path", { d: "M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1" }],
      ["circle", { cx: 15, cy: 6, r: 2 }],
      ["circle", { cx: 9, cy: 12, r: 2 }],
      ["circle", { cx: 17, cy: 18, r: 2 }],
    ],
    sparkles: [
      ["path", { d: "M11 3.5 12.8 8.2 17.5 10l-4.7 1.8L11 16.5l-1.8-4.7L4.5 10l4.7-1.8Z" }],
      ["path", { d: "M18.5 15v4M16.5 17h4" }],
    ],
    alert: [
      ["path", { d: "M10.3 4.2 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0Z" }],
      ["path", { d: "M12 9.5v4M12 17h.01" }],
    ],
    check: [["path", { d: "m5 12.5 4.5 4.5L19 7.5" }]],
    pencil: [["path", { d: "M15.5 5.5 18.5 8.5M4 20l1-4.5L16 4.5a2.1 2.1 0 0 1 3 3L8 18.5Z" }]],
    trash: [["path", { d: "M4 7h16M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13" }]],
    keyboard: [
      ["rect", { x: 2.5, y: 6, width: 19, height: 12, rx: 2 }],
      ["path", { d: "M6.5 10h.01M10 10h.01M14 10h.01M17.5 10h.01M8 14h8" }],
    ],
    message: [["path", { d: "M20 15a2 2 0 0 1-2 2H8l-4 3.5V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2Z" }]],
    lock: [
      ["rect", { x: 4.5, y: 10.5, width: 15, height: 10, rx: 2 }],
      ["path", { d: "M8 10.5V7.5a4 4 0 0 1 8 0v3" }],
    ],
    zap: [["path", { d: "M13 2.5 4.5 13.5H12l-1 8 8.5-11H12Z" }]],
    play: [["path", { d: "M7 4.5v15l12-7.5Z" }]],
    "user-plus": [
      ["circle", { cx: 9.5, cy: 8, r: 3.5 }],
      ["path", { d: "M3 20a6.5 6.5 0 0 1 13 0M19 8v6M16 11h6" }],
    ],
    help: [
      ["circle", { cx: 12, cy: 12, r: 9 }],
      ["path", { d: "M9.5 9.5a2.6 2.6 0 0 1 5 1c0 1.8-2.5 2.2-2.5 3.8M12 17h.01" }],
    ],
    info: [
      ["circle", { cx: 12, cy: 12, r: 9 }],
      ["path", { d: "M12 11v5M12 8h.01" }],
    ],
    inbox: [
      ["path", { d: "M3.5 13.5 6 5.5h12l2.5 8V18a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18Z" }],
      ["path", { d: "M3.5 13.5h5l1.5 2.5h4l1.5-2.5h5" }],
    ],
    logout: [["path", { d: "M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4M10 8l-4 4 4 4M6 12h9" }]],
  };

  const shapes = $derived(PATHS[name] ?? []);
</script>

<svg
  class="icon"
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.8"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
  focusable="false"
>
  {#each shapes as [tag, attrs]}
    <svelte:element this={tag} xmlns="http://www.w3.org/2000/svg" {...attrs} />
  {/each}
</svg>

<style>
  .icon {
    flex: none;
    display: block;
  }
</style>
