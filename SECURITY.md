# Security model

This application trusts its single owner and the host operator. It does not trust model output, fetched content, repository files, or generated artifacts as executable instructions.

Server-enforced controls include authentication, CSRF/origin checks, strict tool schemas, fixed credential scope, exact action approvals, branch restrictions, durable tool receipts, and public-network checks for website reads. Website connections pin validated public DNS addresses and retain TLS hostname verification. Redirects are refused.

Prompt instructions are defense in depth, not a security boundary. Models can still produce bad recommendations or propose harmful content. Review the full action, not just its name. A GitHub PR can contain unsafe code even when it writes to a restricted branch.

Known boundaries:

- One owner per database, tenant, and environment. No multi-user login, SSO, MFA, or password-reset service.
- Vault credential values use authenticated encryption with separately stored keys. Other SQLite data remains plaintext; encrypt disks and backups.
- Prefer the scoped encrypted vault. Legacy environment-only configuration remains supported and is labeled explicitly. Never commit secrets.
- Known secrets are rejected from request/tool content and redacted from model inputs, outputs, and saved state. Unknown or transformed secrets remain a risk.
- An external request already in flight may complete after Stop. Ambiguous failures require provider-side inspection.
- No automatic HTTP retries for external writes. Receipt-based replay prevention is not a distributed exactly-once guarantee.
- Audit events are owner-readable operational records, not tamper-proof compliance logs.
- Workload limits are not spending caps. Configure provider-side financial limits.
- No security audit, penetration test, or regulatory certification is claimed.

Private vulnerability reporting is not yet enabled on this repository. The maintainer must enable it under Settings > Code security before GitHub can receive private reports. Until then, arrange a private channel with the repository owner before sharing vulnerability details. Do not post credentials or customer data in public GitHub issues.

See [credential boundaries, migration, rotation, and webhook authentication](docs/credential-security.md) for the threat model and operational limits.
