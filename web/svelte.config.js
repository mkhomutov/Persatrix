import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// Plain Svelte (no SvelteKit) per RFC 0048 OQ1 — the contract is "static files
// behind WithUI", so no Node runtime ships in the binary. vitePreprocess only
// enables standard preprocessing (e.g. <style> handling); no compiler options
// beyond the defaults are needed for Slice 1.
export default {
  preprocess: vitePreprocess(),
};
