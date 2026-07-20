import { mount } from "svelte";
import App from "./App.svelte";
// One design system, three sheets (split to hold the file-size cap): tokens +
// shell chrome, then the conversation workspace, then management + overlays.
import "./app.css";
import "./styles/conversation.css";
import "./styles/management.css";

// Entry point: mount the console shell into the page. Plain client-side Svelte
// (no SSR/hydration) — the orchestrator serves the static bundle and the SPA
// boots in the browser off /api/v1/ui/config + /api/v1/ui/context.
const app = mount(App, { target: document.getElementById("app") });

export default app;
