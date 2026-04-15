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
