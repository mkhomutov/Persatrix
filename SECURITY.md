# Security Policy

## Reporting a Vulnerability

If you believe you have found a security vulnerability in this project, **please do not open a public issue**.

Instead, please report it through [GitHub's Private Vulnerability Reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability). This ensures the issue is triaged privately before any public disclosure.

When reporting, please include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce or a proof of concept (if possible).
- Any relevant environment details (OS, Go/Python/Rust version, etc.).

### What to Expect

- We will acknowledge receipt of your report within a reasonable timeframe.
- We will investigate and work toward a fix. We may reach out for additional information.
- Once a fix is available, we will coordinate disclosure with you before making details public.

We appreciate responsible disclosure and will credit reporters (unless anonymity is preferred).

## Supported Versions

Security fixes are applied to the latest release on the `main` branch. Older versions are not actively maintained.

## Security Design

Persatrix uses deny-by-default security for agent permissions. For details on the security model, see:

- [Agent Configuration](config/agents.yaml) — Agent permissions and capabilities
- [Security Gates](internal/security/) — Go orchestrator security enforcement

## Responsible Use

Persatrix is experimental, pre-1.0 software distributed under
[BUSL 1.1](LICENSE). It drives commercial LLM APIs and runs persona agents
with autonomous tick loops, so misuse or undiscovered bugs can incur real
monetary cost. Users running Persatrix are responsible for:

- **Configuring spending limits at their LLM provider.** This is the
  authoritative safeguard. Persatrix's internal budget controls
  (`max_llm_calls` per agent, `max_daily_usd` in
  [config/optimization.yaml](config/optimization.yaml), per-workflow token
  budgets, response cache) are best-effort and have known limitations —
  see the cost warning in [README.md](README.md#-cost-warning--read-before-running).
- **Not running Persatrix in production environments** without significant
  additional safeguards beyond what the project provides. The roadmap
  through v0.3 explicitly treats current releases as experimental.
- **Reviewing agent actions before allowing them to affect real systems.**
  Persona behaviour is non-deterministic by design and tool permissions
  default to deny — keep them that way unless you have audited the
  alternative.
- **Stopping persona agents explicitly when done.** Kill the `make run-agent`
  process or the `agent-*` Docker Compose service. Do not rely on
  `idle_after_ticks`, the empty-context tick short-circuit, or any other
  automatic timeout alone.
- **Reporting cost or security bugs responsibly** via the private
  vulnerability reporting channel above (for security-sensitive issues) or
  via [GitHub Issues](https://github.com/mkhomutov/Persatrix/issues) (for
  unexpected-cost or budget-bypass bugs that are not directly exploitable).

Persatrix authors and contributors are not liable for API costs, damages,
or losses arising from use of this software, as stated in
[LICENSE](LICENSE).
