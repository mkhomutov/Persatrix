import { mount } from "svelte";
import App from "./App.svelte";
import { applyTheme, loadTheme } from "./lib/prefs.js";
// One design system, one sheet per surface, in cascade order: the tokens, the
// shared controls, the shell, the conversation workspace, the management rail
// and its settings form, dialogs, and the narrow layout last so it overrides.
import "./app.css";
import "./styles/controls.css";
import "./styles/shell.css";
import "./styles/sidebar.css";
import "./styles/conversation.css";
import "./styles/composer.css";
import "./styles/management.css";
import "./styles/settings.css";
import "./styles/dialogs.css";
import "./styles/responsive.css";

// Pin a remembered colour theme before the first paint, so a dark-theme
// operator never sees a flash of light.
applyTheme(loadTheme());

// Entry point: mount the console shell into the page. Plain client-side Svelte
// (no SSR/hydration) — the orchestrator serves the static bundle and the SPA
// boots in the browser off /api/v1/ui/config + /api/v1/ui/context.
const app = mount(App, { target: document.getElementById("app") });

export default app;
