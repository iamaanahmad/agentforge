# Security model

This application trusts its single owner and the host operator. It does not trust model output, fetched content, repository files, or generated artifacts as executable instructions.

Server-enforced controls include authentication, CSRF/origin checks, strict tool schemas, fixed credential scope, exact action approvals, branch restrictions, durable tool receipts, and public-network checks for website reads. Website connections pin validated public DNS addresses and retain TLS hostname verification. Redirects are refused.

Prompt instructions are defense in depth, not a security boundary. Models can still produce bad recommendations or propose harmful content. Review the full action, not just its name. A GitHub PR can contain unsafe code even when it writes to a restricted branch.

Known boundaries:

- One shared owner password; no SSO, MFA, roles, tenant isolation, or password-reset service.
- SQLite and backups are plaintext. Use disk encryption and restrict host access.
- API keys live in server configuration. Do not commit `.env` or put secrets in tasks. Common credential file paths are blocked, but arbitrary source files may still contain secrets.
- Known configured secret strings are redacted from final results and tool results. This does not detect every secret or private datum.
- An external request already in flight may complete after Stop. Ambiguous failures require provider-side inspection.
- No automatic HTTP retries for external writes. Receipt-based replay prevention is not a distributed exactly-once guarantee.
- Audit events are owner-readable operational records, not tamper-proof compliance logs.
- Workload limits are not spending caps. Configure provider-side financial limits.
- No security audit, penetration test, or regulatory certification is claimed.

Report suspected vulnerabilities privately to the repository owner through an agreed private channel. Do not post credentials or customer data in public GitHub issues.
