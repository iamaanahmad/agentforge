# Deployment and operations

## Deployment boundary

This release targets one owner, one Linux host, one web service, and one worker. It is not a multi-tenant service. Provide a persistent local volume. Do not expose development ports to the internet.

## Local acceptance

Run the commands in the README. Confirm `/healthz` returns 200, sign in, save a draft, and check the worker indicator. Without an OpenAI key, no model run starts. Set provider budgets before adding a key.

## HTTPS deployment with Docker Compose

1. Point your domain's DNS to your host. Allow inbound TCP 80/443 and optionally UDP 443 for Caddy.
2. Copy `.env.example` to `.env`, set permissions to `600`, and fill fresh strong secrets. Do not use the local bootstrap settings publicly.
3. Set `A4G_PUBLIC_ORIGIN=https://your-domain`, `A4G_SECURE_COOKIES=true`, and exact `A4G_ALLOWED_HOSTS`, including `localhost` for health checks.
4. Add your own model key. Add optional service keys only if needed. Use a fine-grained GitHub token for one repository.
5. Run `docker compose --profile tls up --build -d --wait`.
6. Check `docker compose ps` and `docker compose exec -T worker python -m agent4good.healthcheck`.
7. Visit the HTTPS URL, sign in, and verify a small internal artifact task. Then test one approved disposable GitHub issue if that integration is enabled.
8. Configure host monitoring, encrypted backups, a provider budget, and your update routine before relying on unattended work.

Caddy terminates TLS. Uvicorn deliberately ignores forwarded client IP headers, so login throttling is shared per proxy IP. This is conservative for one owner. Do not enable arbitrary forwarded-header trust to bypass it. Restrict origin ports and deploy behind an access gateway if your risk profile requires it.

## Backups

Run an online consistent backup inside the deployment:

```sh
docker compose exec -T web python scripts/backup.py /app/data/backup-YYYYMMDD.sqlite3
```

Copy the backup to encrypted off-host storage using your host tooling. A file in the same volume is not disaster recovery. It contains private data and session hashes. Verify restore on a separate disposable deployment periodically.

Restore procedure:

1. Stop web and worker. Keep the previous database and volume as a recovery copy.
2. Validate the backup with SQLite `PRAGMA integrity_check`.
3. Replace `agent4good.sqlite3` with the backup. Remove old `-wal` and `-shm` files only while both services are stopped.
4. Preserve ownership for UID/GID 10001. Rotate `A4G_SESSION_SECRET` to invalidate restored sessions.
5. Start both services. Inspect interrupted tasks and external provider receipts before creating replacement work.

## Recovery and updates

- `failed` after restart: inspect the activity and service receipts. An email or GitHub change may already exist. Never assume it did not happen.
- `waiting_approval`: review the exact action in Decisions. Approval resumes the saved run.
- `queued` with worker online: the UTC daily run budget may be exhausted. It includes approval resumes. Check the server configuration and activity.
- `queued` with worker offline: restart the worker and inspect container health.
- Connection configured but HTTP error: check provider permissions, billing, rate limits, and resource scope. Configuration is not proof of access.
- Invalid host or sign-in failure: verify exact allowed hosts, HTTPS origin, secure cookies, and reverse-proxy routing.
- Duplicate worker: the second process exits because the file lock is held. Do not scale the worker service.

Before upgrades, back up the database and retain the prior image. Schema version 3 preserves owner data and binds the database to its configured tenant and environment. Existing sessions require fresh login. Read [vault migration notes](credential-security.md) before changing configuration.

## Go-live evidence

The repository's tests and container CI prove local code paths, not your live provider access or host reliability. Record a successful authenticated HTTPS session, worker heartbeat, artifact run, backup/restore, and any enabled external integration's bounded acceptance test before declaring your deployment production-ready.

## Repeatable recovery acceptance

Run `uv run pytest -q tests/test_release_acceptance.py` before releasing changes.
This invokes the backup command against tagged test data and restores into a separate temporary directory.
It checks owner authentication, exact saved approval, artifact retrieval, and completed receipts after restart.
It also checks that interrupted work stays failed and existing backup files cannot be overwritten.
Pytest uses disposable data; no external message is sent and no provider key is inherited by the backup process.

This is application integration evidence with scripted model responses, not a live deployment test.
A restored backup can predate external writes. Inspect provider receipts before resuming restored approvals or queued tasks.
Do not run the original and restored workers together. A database backup cannot roll back a provider action.
