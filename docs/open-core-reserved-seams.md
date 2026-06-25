# Open-Core Reserved Seams

**What this is:** a short, standing reminder for anyone building new BUSL
features. [RFC 0045](rfcs/0045-open-core-extraction-policy.md) splits the
codebase into three license tiers — permissive **MIT** primitives below, the
self-hostable **BUSL-1.1** product in the middle, and a never-published
**Private** tier above. This note records the handful of interfaces that a
future Private (hosted/commercial) tier will plug into, and one rule that keeps
the open product honest. Shaping those interfaces deliberately *now* — while
they are cheap to keep clean — is far easier than retrofitting them later.

This note **moves no code and stands up no Private tier.** It only reserves
seams. The full reasoning lives in
[RFC 0045 §C](rfcs/0045-open-core-extraction-policy.md#c-the-private-tier-reserved-seams-and-the-no-retraction-rule).

## The four reserved seams

When you touch one of these areas, keep the boundary a clean, stable interface
so a managed implementation could drop in behind it later:

| Seam | Keep this stable | Where it already shows up |
|------|------------------|---------------------------|
| **Memory backend** | A `MemoryBackend` boundary behind the frozen memory facade, so a managed/scaled society store is a drop-in. | [RFC 0029](rfcs/0029-personal-society-storage-split.md) `society_facade`; the memory facade ahead of the Postgres split. |
| **Budget policy & pricing/metering** | A pluggable budget-policy and pricing/metering source, so real billing can replace the in-app wallet *simulation*. | The wallet's pluggable cost/budget interface ([RFC 0023](rfcs/0023-llm-call-leasing.md)). |
| **Identity & authz** | An `Identity`/authz boundary for SSO, RBAC, and org administration. | [RFC 0039](rfcs/0039-user-accounts-authentication.md); [RFC 0012](rfcs/0012-protocols-organizations.md); `config/organizations.yaml`. |
| **Control plane & tenancy** | A control-plane/tenancy boundary for a managed, multi-tenant orchestrator. | The single-node orchestrator topology. |

## The no-retraction rule

**Nothing currently shipped under BUSL is ever clawed back into the Private
tier.** The line between BUSL and Private is drawn *before* code is published,
never after.

- The memory tiers as they exist today — including relationship/trust — stay
  BUSL and self-hostable.
- The Private differentiation is the **managed, scaled** society backend
  (operated, not shipped) plus **future** capabilities that ship straight to
  Private and were never public.
- **Ambiguous new capability defaults to BUSL.** When it is genuinely unclear
  whether something belongs in BUSL or Private, it ships to BUSL — the default
  tier — and the line is drawn explicitly in that capability's own RFC. This is
  safe because the moat is *operational* (running the managed backend), not
  source secrecy: a capability shipped to BUSL can always stay or open further,
  but one published can never be pulled back.

## The mechanical half

The downward (MIT) boundary is enforced, not just documented: the
`MIT ← BUSL ← Private` import-direction invariant is a hard CI gate
([RFC 0045 §B](rfcs/0045-open-core-extraction-policy.md#b-the-dependency-direction-invariant)).
A leaf MIT-candidate primitive that imports orchestrator-internal (BUSL) code
fails the build —

- **Python:** the `[tool.importlinter]` forbidden contract in
  [`agents/pyproject.toml`](../agents/pyproject.toml), run by `make imports-check`.
- **Go:** the `internal/archpolicy` package, checked in the existing
  `go test ./internal/...` lane.

The upward (BUSL ↛ Private) boundary has no live check yet — there is no Private
code in this repo to import. It is enforced by *keeping intended-private
capability out of the public tree in the first place*, which is exactly what the
no-retraction rule above is for.

## Related documentation

- [RFC 0045 — Open-Core Library Extraction Policy](rfcs/0045-open-core-extraction-policy.md) — the full policy this note summarizes.
- [RFC 0046 — Budget-Lease Library Extraction](rfcs/0046-budget-lease-extraction.md) / [RFC 0047 — Low-Coupling Batch Extraction](rfcs/0047-low-coupling-batch-extraction.md) — the first per-extraction RFCs that inherit the policy.
- [CONTRIBUTING.md](../CONTRIBUTING.md) — the sign-off (DCO) requirement extracted repos will carry.
