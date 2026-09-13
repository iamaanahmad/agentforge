# Credentials and execution boundaries

This release protects one owner per database, tenant, and environment. It does not enable multi-user service.

## Threat model

The host operator and application processes are trusted. Models, tool arguments, fetched pages, and webhook callers are untrusted.

| Boundary | Enforced control | Remaining limit |
|---|---|---|
| Stolen database or backup | AES-256-GCM encrypts vault values and authenticates scope metadata | Task data remains plaintext; use disk and backup encryption |
| Credential misuse | Server selects credential, active task, owner, role, and fixed adapter purpose | Trusted Python processes can access keys; this is not process isolation |
| Cross-tenant or environment startup | Persistent database binding refuses a different configured domain | Separate hosts/containers, volumes, and keys are still required |
| Cross-user task access | API hides foreign task records; workers and brokers refuse foreign owners | No second-user login, roles, or multi-user operation |
| Prompt or log leakage | Known secrets are rejected from API/tool inputs and redacted from model context, output, and saved state | Encoded, fragmented, unknown, and historical secrets need separate review |
| Forged or repeated webhook | HMAC, timestamp, domain binding, atomic delivery receipt, payload and rate limits | A valid sender can create drafts; no provider-specific webhook protocol is implied |
| External writes | Existing exact approvals, policy limits, receipts, and branch restrictions | In-flight requests can finish after cancellation or revocation |
| SSRF and commands | Public DNS validation, pinned HTTPS, no redirects, fixed service endpoints, proxy environment ignored | Shell and sandbox execution remain unavailable |

The cryptographic primitive follows [cryptography's AESGCM contract](https://cryptography.io/en/latest/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM).
Each encryption uses a new random 96-bit nonce. Name, key identifier, and scope metadata are authenticated together.
This does not claim resistance to a compromised host, malicious application code, or universal sandbox escapes.

## Set up the encrypted vault

Stop web and worker before migration. Take a private database backup first.
Choose the tenant and environment before first startup: `A4G_TENANT_ID` defaults to `default`; `A4G_ENVIRONMENT` defaults to `local`.
Existing databases bind to those values during their first upgrade. A later mismatch refuses startup.
Keep the same values during recovery. Never reuse one database for another environment.

1. Create a key directory outside the repository, data volume, and database backup destination. Restrict it to the host operator.
2. Run `uv run python -m agent4good.vault init-key /secure/agent4good/keyring.json`.
3. Set `A4G_CREDENTIAL_KEY_FILE=/secure/agent4good/keyring.json` in private server configuration.
4. Run `uv run python -m agent4good.vault put openai_api_key --agent strategist`.
5. Enter the value at the hidden prompt. Repeat `--agent` for each allowed role.
6. Import needed adapter credentials similarly, then remove their old environment values.
7. Restart both services. Confirm `/api/readiness` reports `credential_storage: encrypted-vault` after login.

Supported names: `openai_api_key`, `github_token`, `search_api_key`, `resend_api_key`, and `webhook_secret`.
Use `--purpose github_read_file --purpose github_list_issues` to restrict a GitHub credential to reads.
Repository, sender, and host configuration remain server-owned. Fine-grained provider scopes remain necessary.
Use `--stdin` only with a private file or secret manager pipe. Never put values in command arguments or shell history.

The key file must be a regular, non-symlink file owned by the process user, with no group or other permissions.
The application rejects keys beneath its data directory. The operator must also separate key storage and backup access permissions.
A path check cannot prove that two directories use different physical storage or access policies.

Vault mode never falls back to environment credentials. Missing, corrupted, or inaccessible key material fails closed.
Without a configured key file, the existing environment-only owner deployment remains available and is labeled `legacy-environment`.
Legacy environment values are not automatically copied into SQLite. Owner password and session secret remain private bootstrap configuration.
`Settings` representations and serialization exclude those secret fields.

## Rotate, revoke, and restore

Stop both services before key rotation. Run `uv run python -m agent4good.vault rotate`, then restart them.
The command atomically replaces the keyring and rewraps all credential rows in one database transaction.
Old keys remain available for older backups. Back up the keyring separately with tighter access than database backups.
If the process stops between keyring update and database commit, run `uv run python -m agent4good.vault rewrap`.
Both old and new keys remain valid during recovery. Do not remove old keys until dependent backups expire.

To rotate a provider value, use `put` again. Revoke the old value at the provider after confirming migration.
To deny future access, run `uv run python -m agent4good.vault revoke <name>`.
Already-dispatched requests may finish. Restart services when changing legacy environment credentials.

Restore the database and its separately protected keyring, using the original tenant and environment identifiers.
Rotate the session secret after restoration. The upgrade changes session hashing, so existing sessions require a fresh login.
Owner password, tasks, approvals, memory, and artifacts remain intact. Never run original and restored workers together.
A restored backup can predate an external action; inspect provider receipts before resuming work.

## Docker vault overlay

Use `compose.vault.yaml` together with the base Compose file.
Set `A4G_KEYRING_HOST_PATH` to a host directory containing `keyring.json` outside the data volume.
Make that directory and file accessible only to UID 10001: directory mode 0700, file mode 0600.
The overlay mounts the directory read-only at `/run/a4g-keys` in both application containers.
Manage and rotate its contents through a private maintenance process, not a model tool or public API.

```sh
docker compose -f compose.yaml -f compose.vault.yaml up --build -d --wait
```

The base Compose configuration retains the legacy path. It does not claim encrypted credentials until the overlay is configured.

## Signed draft webhook

Create `webhook_secret` with a random value of at least 32 characters using the vault CLI.
Webhook access is unavailable until this vault credential exists. It is never a model tool.

Send JSON containing `title`, `prompt`, and optionally `agent` to `POST /api/webhooks/tasks`.
Unknown fields, including `start`, `owner_id`, tenant, environment, and approvals, are rejected.
Every accepted request creates one draft. It cannot start paid model work, approve actions, or change policies.

Headers:

- `X-A4G-Timestamp`: current Unix timestamp in seconds, within five minutes.
- `X-A4G-Delivery`: unique identifier of 16–128 ASCII letters, digits, underscores, or hyphens.
- `X-A4G-Signature`: `v1=` followed by the lowercase HMAC-SHA256 hex digest.
- `Content-Type`: `application/json`.

Sign these UTF-8 bytes, followed immediately by the exact raw request body:

```text
v1\nPOST\n/api/webhooks/tasks\n<tenant>\n<environment>\n<timestamp>\n<delivery>\n
```

Limits: 32 KB payload; 60 attempts per minute per observed client IP. Forwarded IP headers remain untrusted.
A duplicate delivery returns 409, including concurrent retries. Receipts persist so reused identifiers remain rejected.
Invalid signatures and stale timestamps return 401. Oversized requests return 413. Rate limits return 429.
Do not reuse a delivery identifier after receiving any successful response.

## Evidence and release limits

`tests/test_security_boundaries.py` exercises encrypted backup restoration, key rotation, tampering, revocation, and domain refusal.
It also exercises real ASGI routes, the model adapter with a simulated transport, concurrency, and secret redaction.
Existing policy, SSRF, approval, and release restoration tests remain required.
No test sends customer email or uses a paid model. These checks do not establish production deployment or independent security certification.
