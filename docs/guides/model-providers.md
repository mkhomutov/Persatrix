# Model Providers & Aliases

Persatrix is provider-agnostic: the same agents run on Anthropic, OpenAI,
Gemini, watsonx.ai, a local model via Ollama, or a zero-cost mock — and
switching between them is a one-line config edit, not a code change. This guide
explains how that works and how to pick (or swap) a provider.

It implements [RFC 0033 — Provider-Agnostic Model Alias Layer](../rfcs/0033-model-alias-layer.md).
For per-agent config in general, see the [persona agents guide](persona-agents.md).

---

## The idea: agents name a role, not a vendor

An agent's `model:` field is a **logical alias** — a role like `quality`,
`fast`, or `summarizer` — not a vendor model ID:

```yaml
# config/agents.yaml
- id: ember-owl
  type: persona
  model: "quality"        # a role, resolved at call time — not "claude-sonnet-4-6"
```

The alias is defined once, in [`config/optimization.yaml`](../../config/optimization.yaml),
where it resolves to a concrete `(provider, model, pricing)` record:

```yaml
# config/optimization.yaml
models:
  aliases:
    quality:
      provider: anthropic          # ← you choose this
      model: claude-sonnet-4-6
      input_per_1m_tokens: 3.00
      output_per_1m_tokens: 15.00
```

Because every agent, the routing defaults, and the summarisation path all
reference the alias **name**, a vendor retirement or a provider swap is a
single edit to that one entry — not a sweep across `agents.yaml`, the routing
defaults, the pricing table, and the docs.

> **No default provider.** The shipped `config/optimization.yaml` ships these
> role aliases **unconfigured** (`provider: unconfigured`), so provider choice
> is always explicit. A plain `docker compose up` fails loud at agent startup
> with an actionable message until you pick one — run a [demo](#zero-config-demos)
> (which mounts a configured alias config) or set `provider`/`model`/pricing on
> the three role aliases (`quality` / `fast` / `summarizer`) yourself. Nothing
> privileges a vendor or can spend money by default.

---

## The six providers are peers

Provider selection is **pure data**: the alias entry's `provider` field
chooses the concrete provider, and all six are selected the exact same way.
There are no per-provider force-knobs.

| Provider | `provider:` | Needs | Cost | Notes |
|----------|-------------|-------|------|-------|
| **Anthropic** | `anthropic` | `ANTHROPIC_API_KEY` | per-token | Claude. A peer, not a default — no provider is configured out of the box. |
| **OpenAI** | `openai` | `OPENAI_API_KEY` | per-token | Also any OpenAI-compatible API (vLLM, Together, Groq, LM Studio) via `provider_config.base_url`. |
| **Gemini** | `gemini` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) + the `google-genai` extra | per-token | Google Gemini on the native `google-genai` SDK ([`agents/llm_gemini.py`](../../agents/llm_gemini.py)) — a first-class `gemini` identity for cost/telemetry, not the OpenAI-compat endpoint (RFC 0053). Optional Vertex routing via `provider_config.project`/`location`. Install the SDK: `pip install 'google-genai>=1.0.0'` (or the extra: `pip install 'persatrix-agents[gemini]'`). |
| **watsonx.ai** | `watsonx` | `WATSONX_API_KEY` (secret) **+** a `project_id`/`space_id` (non-secret — `provider_config` **or** `WATSONX_PROJECT_ID`/`WATSONX_SPACE_ID` env) **+** optional `url` (`provider_config.url` or `WATSONX_URL`; defaults us-south) + the `ibm-watsonx-ai` extra | per-token | IBM watsonx.ai (Llama / Granite / Mistral hosts) on the native `ibm-watsonx-ai` SDK ([`agents/llm_watsonx.py`](../../agents/llm_watsonx.py)) — no broad OpenAI-compatible endpoint exists, so a native class is required (RFC 0053 §C). The **secret** key rides env; the non-secret `project_id`/`url` resolve from `provider_config` **or** a `WATSONX_*` env fallback, and the factory **fails closed at startup** if no id is set in either (see below). Install: `pip install 'ibm-watsonx-ai>=1.1.0'` (or the extra: `pip install 'persatrix-agents[watsonx]'`). |
| **Ollama** | `ollama` | a local `ollama serve` | **$0** (local) | A real model on your machine; a thin OpenAI-compatible subclass ([`agents/llm_ollama.py`](../../agents/llm_ollama.py)). `provider_config.base_url` defaults to `http://localhost:11434/v1`. |
| **Mock (offline)** | `mock` | nothing | **$0** | Scripted persona replies, no network, no key ([`agents/llm_offline.py`](../../agents/llm_offline.py)). For demos, CI smoke, and risk-free exploration. |

Each entry needs `provider`, `model`, and pricing (see
[Pricing](#pricing-and-the-missing-price-guard) below). Adding a brand-new
provider is the recipe in [RFC 0033 §H](../rfcs/0033-model-alias-layer.md):
a class implementing the `LLMProvider` protocol plus one branch in
[`agents/llm_factory.py`](../../agents/llm_factory.py) — Ollama is that RFC's
first worked example, **Gemini the second and watsonx.ai the third**
([RFC 0053](../rfcs/0053-gemini-watsonx-providers.md)).

> **watsonx: the secret-vs-config split.** watsonx is the one provider that
> needs a non-secret setting the client cannot be built without — a `project_id`
> (or `space_id`); plus a regional `url` (defaults to us-south). These are
> **config, not credentials**. Their source of truth is the alias
> `provider_config` (the same channel OpenAI's `base_url` uses), but — because
> they are non-secret — each also accepts a `WATSONX_*` env fallback
> (`WATSONX_PROJECT_ID` / `WATSONX_SPACE_ID` / `WATSONX_URL`), resolved by
> [`resolve_watsonx_config`](../../agents/llm_watsonx.py) with the same
> precedence as Ollama's `base_url` (provider_config → env → default). That env
> channel lets the shipped demo config stay generic and keeps your project id out
> of VCS — it is *not* a second secret path (only `WATSONX_API_KEY` is a secret).
> If a `project_id` **and** `space_id` are absent from *both* channels, the
> factory **fails closed with a loud `SystemExit` at startup** — deliberately
> louder than the soft missing-*key* warning the other cloud providers use,
> because config the client literally cannot construct without should fail at
> boot, not defer to the first request (`url` defaults, so it never blocks). See
> [MT-PROVIDER-WATSONX-001](../manual-tests/MT-PROVIDER-WATSONX-001.md).

> **Persatrix meters inference — and nothing else.** The `Cost` column above is
> the **inference** price. It is the only cost the alias table meters, and the
> only one a cost cap — the RFC 0023 per-agent wallet, or an autonomous
> channel's mandatory cap — can enforce. Any charge your provider raises that is
> *not* per-token (a subscription, a reserved-capacity or provisioned-throughput
> commitment, a per-instance platform fee) sits outside that accounting
> entirely: Persatrix never sees it, so it cannot reach cost telemetry and no
> cap can bound it. **Your provider's own bill is the authority on your spend;
> Persatrix's cost table is not.**
>
> This is worth stating beside watsonx because watsonx is the provider in this
> table whose platform layer — watsonx.ai Runtime — is provisioned as its own
> billable service on a plan you pick, separately from the tokens you spend. So
> the plan choice, not just the model choice, decides your floor. **As of August
> 2026** IBM's published plan documentation describes three tiers; check the
> [current plan page](https://www.ibm.com/docs/en/watsonx/saas?topic=cloud-watsonxai-runtime-plans)
> before you provision, because plans, limits, and prices are IBM's to change
> and this guide is not the authority on them:
>
> - **Lite** — free, and where to evaluate. Published limits are 20 CUH and
>   300 000 tokens a month at 2 requests/second, which is enough to exercise
>   this provider. Mind the rate limit: the demo society is **six agents** that
>   can call concurrently, so a busy channel can meet it — throttling on Lite is
>   the plan working as documented, not a broken integration.
> - **Essentials** — pay as you go, no fixed fee, billed per Resource Unit
>   (1 RU = 1 000 tokens). That is the same per-token shape the alias table
>   already meters, so a cost cap does track it.
> - **Standard** (and the enterprise tiers) — carries a **flat monthly instance
>   fee** for the provisioned instance, independent of what you run through it.
>   That fee is the kind of charge the paragraph above describes: real spend,
>   invisible to every cap Persatrix has.
>
> One operator's data point rather than a general claim: on a short evaluation
> of this integration, the instance fee on the bill was three orders of
> magnitude larger than the inference charged over the same period. Whether that
> shape applies to you depends on the plan you provision.
>
> If you do provision a fee-bearing plan:
>
> - **The instance meters, not the stack.** The fee accrues while the Runtime
>   instance exists, whether or not any agent ever calls it — `docker compose
>   down` does not stop it, deleting the instance does. Moving back down a tier
>   is not an in-place edit either: you delete the instance and provision a new
>   one.
> - **Set a spending notification first.** IBM Cloud's spending controls are
>   notification-only — they alert, they do not stop usage — and they are not on
>   by default (Billing and usage → Spending notifications).
> - **A trial credit can absorb the early fees.** If your account carries one,
>   the running total stays near $0 until it is consumed, so the first invoice —
>   not the console — is the first complete signal.
>
> None of this applies to `make demo-offline` or `make demo-ollama`, which cost
> nothing by construction.

---

## Swapping providers is one line per role

Switching the model behind a role is a single-entry edit — no code change, no
sweep across `agents.yaml`, the pricing table, and the docs. To move the
`quality` role (the task and persona agents) from Anthropic to OpenAI:

```diff
 quality:
-  provider: anthropic
-  model: claude-sonnet-4-6
-  input_per_1m_tokens: 3.00
-  output_per_1m_tokens: 15.00
+  provider: openai
+  model: gpt-4o
+  input_per_1m_tokens: 2.50
+  output_per_1m_tokens: 10.00
```

The quality-routed agents, unchanged, now run on OpenAI, and cost re-keys to
the new physical model because the cost table is derived from the alias map
(below). This is verified end-to-end by the manual test
[MT-ALIAS-002](../manual-tests/MT-ALIAS-002.md): a single alias edit re-routed
a live agent to a different provider, with cost re-keyed to the new model and
the config reverted clean.

> **The society spans three role aliases** — `quality` (the task and persona
> agents), `fast` (evaluators), and `summarizer` (the summarisation-on-close
> path, [RFC 0020](../rfcs/0020-interaction-lifecycle.md)). Moving the **whole**
> society to a provider means pointing all three at it — each a one-line edit; a
> [demo config](#zero-config-demos) does that in one file. Configure at least the
> role aliases your run exercises: leaving `summarizer` `unconfigured` doesn't
> break chat, but summarisation-on-close silently degrades to its fallback.

> The alias entry is **authoritative**. If an agent sets its own `provider:`
> field that *disagrees* with the alias it resolves to, the factory fails loud
> at startup (RFC 0033 §D) rather than silently picking one — drop the
> redundant field.

---

## Zero-config demos

Each demo selects its provider the same config-driven way — by mounting a
per-provider alias config ([`config/demo/<provider>/optimization.yaml`](../../config/demo/))
over the stack's `optimization.yaml`. There is no env force-knob and no default;
a demo is just an alias config that points `quality` / `fast` / `summarizer` at
one provider. (The base config ships them `unconfigured` — see above.)

```bash
make demo-offline   # mock provider: scripted replies, $0, no key, no network
make demo-ollama    # a REAL local model via Ollama: no key, no cloud spend
make demo-anthropic # the Anthropic (Claude) cloud peer (needs ANTHROPIC_API_KEY; spends real money)
make demo-openai    # the OpenAI cloud peer (needs OPENAI_API_KEY; spends real money)
make demo-gemini    # the Google Gemini cloud peer (needs GEMINI_API_KEY / GOOGLE_API_KEY; spends real money)
make demo-watsonx   # the IBM watsonx.ai cloud peer (needs WATSONX_API_KEY + WATSONX_PROJECT_ID in .env; spends real money)
```

`make demo-ollama` bundles an `ollama` container and pulls the model (default
`llama3.2`; override with `PERSATRIX_OLLAMA_MODEL=qwen2.5 make demo-ollama`,
which swaps the pull and every ollama-routed agent in lock-step). The base
Compose file plumbs every provider key into each agent optionally, so
`make demo-openai` authenticates with just `OPENAI_API_KEY` in your `.env` —
no per-deployment override. `make demo-gemini` additionally installs the
optional `google-genai` SDK into the agent image (via the `AGENT_EXTRAS` build
arg, so `--build` is required) and plumbs `GEMINI_API_KEY` / `GOOGLE_API_KEY`,
which the base compose does not carry. `make demo-watsonx` likewise installs the
`ibm-watsonx-ai` extra and plumbs the secret `WATSONX_API_KEY` plus the
non-secret `WATSONX_PROJECT_ID` / `WATSONX_SPACE_ID` / `WATSONX_URL` env
fallbacks — so you set your (non-secret) project id in `.env`, and the shipped
alias config stays generic. Set one of `WATSONX_PROJECT_ID` / `WATSONX_SPACE_ID`
before the demo boots or the factory fails closed (`WATSONX_URL` is optional —
it defaults to us-south); `make demo-watsonx` preflights for the id and refuses
to start with a clear message if it is missing.

> **Installing the optional provider SDKs (extras).** Only Gemini and watsonx.ai
> ship their SDK as an optional [pyproject extra](../../agents/pyproject.toml):
> `anthropic` / `openai` are always-installed base dependencies, Ollama reuses
> the base `openai` SDK, and the mock needs nothing — so those four carry no
> extra. Install one cloud SDK with `pip install 'persatrix-agents[gemini]'` (or
> `[watsonx]`), or pull **both optional SDKs at once** with the combined bundle
> `pip install 'persatrix-agents[providers]'`. The `make demo-gemini` /
> `demo-watsonx` targets already install the matching extra into the agent image
> for you (via the `AGENT_EXTRAS` build arg); reach for `[providers]` when you
> want every optional cloud SDK in one step — e.g. standing up the **four-vendor
> human-free brainstorm** (Anthropic + OpenAI + Gemini + watsonx.ai discussing
> one topic in one channel, no human), the flagship
> [RFC 0052](../rfcs/0052-autonomous-agent-channels.md) autonomous-channel demo
> this four-cloud-vendor roster exists to enable. The cross-vendor roster is
> [`blueprints/autonomous-multivendor/blueprint.yaml`](../../blueprints/autonomous-multivendor/blueprint.yaml)
> (four seats, each pinned by an RFC 0033 alias to a different cloud vendor), and
> its live headline manual test is
> [MT-AUTONOMOUS-MULTIPROVIDER-001](../manual-tests/MT-AUTONOMOUS-MULTIPROVIDER-001.md)
> (RFC 0052 [PR 9](../rfcs/0052-pr-plan.md#pr-9-featurev0311-rfc0052-demo-multivendor--phase-4b-four-vendor-headline--closeout-cuttable)).

To opt a **single** agent onto a different provider instead of the whole
society, give it its own alias: add an entry to `models.aliases` that declares
the provider, model, and price (e.g. `local-fast: {provider: ollama, model:
llama3.2, input_per_1m_tokens: 0, output_per_1m_tokens: 0}`), then point that
agent's `agents.yaml` `model:` field at the new alias (`model: local-fast`).
Since [RFC 0033 Phase 3](../rfcs/0033-model-alias-layer.md) retired the
raw-vendor-ID pass-through, an agent `model:` **must** name a declared alias — a
raw model tag (`model: llama3.2`) is rejected with a loud `SystemExit`, mock
agents included ([ISSUE-0074](../issues/ISSUE-0074-mock-provider-raw-id-deprecation-gate.md)).
The agent's own `provider:` field, if set, must agree with the alias (§D).

---

## Pricing and the missing-price guard

Every alias entry carries an inline per-token price. Two things consume it:

1. **The derived cost table.** `cost.pricing.models` in `optimization.yaml`
   (keyed by physical model ID, read by the Go cost pipeline) is a checked-in
   *projection* of the alias map. When an alias's model or price changes,
   regenerate that block — a lock-step test
   (`TestShippedCostPricingDerivedFromAliases`) rejects drift.
2. **The RFC 0023 budget/lease gate.** Every call leases against a simulated
   per-agent wallet priced at these rates, so cost is a structural gate, not a
   post-hoc accountant.

A non-local provider with **no** price would make cost estimation return `$0`
and silently disable that gate. The **missing-price guard**
([`agents/model_aliases.py`](../../agents/model_aliases.py)) fails closed on an
unpriced non-local alias at resolve time, with a loud message naming the alias.

**Local models are the exception.** Ollama and mock run at genuinely $0, so
their entries carry an explicit `0` (documented `$0-real`, not absent), and the
guard exempts them — by provider name (`ollama` / `mock`) or by a *loopback*
`provider_config.base_url`. So an explicit-`0` simulation price is
distinguishable from a forgotten cloud price.

> **$0-local vs. the wallet cap.** A genuinely-$0 local alias never trips the
> simulated wallet, so the [README's](../../README.md#-cost-warning) "an agent
> pauses itself at the cap" behaviour only shows on a **priced** (cloud) alias.
> The offline / Ollama demos run at $0 by design; use `make demo-openai` (or an
> Anthropic alias) to watch the cap actually trip.

---

## Telemetry: alias rolls up alongside the physical model

When a call's model came in via an alias, the `agent.llm.call` span carries
`persatrix.llm.model_alias` (e.g. `quality`) **alongside** the physical
`gen_ai.request.model` (e.g. `claude-sonnet-4-6`) — never instead of it. So a
dashboard can group spend by logical role while the vendor ID stays visible.
The alias is telemetry-only — it is never forwarded to the provider API. See
[observability.md § 10.5](../observability.md#105-persatrix-specific-attribute-namespace).

As of RFC 0033 **Phase 3** a `model:` field must name a declared alias. A raw
vendor ID (or a typo'd alias name) is no longer a silent fall-through — it
fails loud with a `SystemExit` at resolve, naming the string and pointing at
`models.aliases`. Provider is data, not inferred: the prefix-routing heuristic
(`_infer_provider`) and the `persatrix.llm.alias.raw_id_usage` dogfood gate
counter that authorised this cutover are retired.

---

## Related

- [RFC 0033 — Provider-Agnostic Model Alias Layer](../rfcs/0033-model-alias-layer.md) — the design.
- [Persona agents guide](persona-agents.md) — `model:` and USD budgets in context.
- [observability.md](../observability.md) — the `persatrix.llm.model_alias` span attribute.
- Manual tests: [MT-ALIAS-001](../manual-tests/MT-ALIAS-001.md) (alias-routed cost), [MT-ALIAS-002](../manual-tests/MT-ALIAS-002.md) (one-line swap), [MT-OFFLINE-001](../manual-tests/MT-OFFLINE-001.md), [MT-OLLAMA-001](../manual-tests/MT-OLLAMA-001.md), [MT-PROVIDER-GEMINI-001](../manual-tests/MT-PROVIDER-GEMINI-001.md), [MT-PROVIDER-WATSONX-001](../manual-tests/MT-PROVIDER-WATSONX-001.md).
