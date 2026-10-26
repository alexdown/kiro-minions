# ADR-001: Developer authentication to the IDE ingress

**Status:** PROPOSED (needs your decision)
**Context doc:** [../ide-ingress.md](../ide-ingress.md) §8

## Context

The Jira ingress authenticates with a single shared `WEBHOOK_SECRET` (HMAC or
header). That is fine for one trusted sender (Jira), but the IDE ingress is
called **by individual developers**. We need to know *who* dispatched a task so
we can:

- scope repo access to that developer's permissions (not just the global
  `REPO_ALLOWLIST`),
- attribute the task in the audit trail (`actor` field),
- apply per-developer rate/quota limits (ADR-007),
- revoke a single developer without rotating a shared secret.

The extension runs on the developer's laptop, so any long-lived shared secret
baked into it is effectively public.

## Options

### Option A — OIDC via the customer's existing IdP / SSO (recommended)
The extension obtains a short-lived OIDC token from the customer's identity
provider (the one their SSO already uses); API Gateway validates it (JWT
authorizer) and the ingress Lambda reads the verified `sub`/email as `actor`.

- **Pros:** real per-user identity; no shared secret on laptops; revocation and
  MFA handled by the IdP; natural fit if the customer already has SSO; tokens are
  short-lived.
- **Cons:** depends on the customer's IdP being OIDC-capable and on us wiring a
  JWT authorizer; more moving parts than a shared secret.

### Option B — Amazon Cognito user pool
We run a Cognito user pool; developers authenticate against it; API Gateway uses
a Cognito authorizer.

- **Pros:** fully managed by us; works even if the customer has no usable IdP;
  built-in token issuance.
- **Cons:** we now own an identity store (user lifecycle, provisioning, offboarding);
  duplicates identity the customer likely already has; onboarding friction.

### Option C — Reuse a token the extension already holds
If the Kiro IDE extension already carries a per-user credential (e.g. a Kiro/GitHub
token), the ingress validates that.

- **Pros:** least new infrastructure; leverages existing trust.
- **Cons:** couples us to the extension's token semantics and scopes; may not
  carry the identity/claims we need; harder to reason about blast radius.

### Option D — Per-developer static API keys
Issue each developer a long-lived key.

- **Pros:** trivial to implement.
- **Cons:** long-lived secret on laptops; manual rotation/revocation; weakest
  option; not recommended.

## Decision

_To be recorded by the maintainer._ Recommendation: **Option A (OIDC via the
customer's IdP)** if they have OIDC SSO; fall back to **Option B (Cognito)** only
if they don't. Avoid D.

**Questions that decide this:**
1. Does the customer have an OIDC-capable IdP/SSO their extension can get a token from?
2. Do they want us to own identity (Cognito) or federate to theirs?
3. Must repo access be scoped to the *individual's* GitHub permissions, or is a
   global allowlist + bot token acceptable for v1?

## Consequences

- Chosen mechanism determines the API Gateway authorizer type and how `actor` is
  populated in the `TaskPayload` (ADR-005) and the audit log.
- Per-user identity is a prerequisite for per-actor quotas (ADR-007).
